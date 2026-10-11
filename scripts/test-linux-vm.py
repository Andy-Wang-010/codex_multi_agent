#!/usr/bin/env python3
"""Run native Ubuntu ARM64 acceptance in an owned, network-confined Lima VM.

All assets are supplied locally and checksum-pinned; this tool never downloads.
The manifest contract and approval boundaries are in docs/client-acceptance.md.
Python 3.9+ is sufficient on the host. No third-party Python packages are used.
"""

import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid


sys.dont_write_bytecode = True
SCHEMA_VERSION = 1
RUNTIME_NAMES = ("client", "code_mode_host", "pwsh", "node")
SUITES = {
    "validate", "validate-docs", "agent-concurrency", "bilingual-docs",
    "deployment", "documentation", "feedback-runtime", "install-user",
    "openspec", "project-blueprint", "project-rules", "stage-verification",
    "validate-package", "portability",
}
# VZ uses Lima's default user-mode network. Inherited confinement prevents its
# host-side forwarding process from opening external sockets, including at boot.
# Never add vzNAT, a named network, or hostResolver to this configuration.
NETWORK_POLICY = '''(version 1)
(allow default)
(deny network*)
(allow network* (local unix-socket) (remote unix-socket))
(allow network-bind (local ip "localhost:*"))
(allow network-inbound (local ip "localhost:*"))
(allow network-outbound (remote ip "localhost:*"))
'''


class AcceptanceError(Exception):
    """Only controlled, credential-free descriptions may be used here."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def digest(path):
    value = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def relative_name(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise AcceptanceError("assets-invalid", "Invalid asset-relative path.")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) in (".", ""):
        raise AcceptanceError("assets-invalid", "An asset path escapes its root.")
    return str(path)


def check_asset(record, base, label):
    if not isinstance(record, dict):
        raise AcceptanceError("assets-invalid", "Asset metadata must be an object.")
    for key in ("path", "sha256", "version", "architecture"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise AcceptanceError("assets-invalid", "Missing pinned asset metadata: " + label)
    if not re.fullmatch(r"[0-9a-fA-F]{64}", record["sha256"]):
        raise AcceptanceError("assets-invalid", "Invalid SHA256 for " + label)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_~:-]{0,100}", record["version"]):
        raise AcceptanceError("assets-invalid", "Invalid version pin for " + label)
    raw = Path(record["path"]).expanduser()
    path = raw if raw.is_absolute() else base / raw
    if not path.is_file() or path.is_symlink():
        raise AcceptanceError("assets-missing", "Missing regular local asset: " + label)
    path = path.resolve()
    if digest(path) != record["sha256"].lower():
        raise AcceptanceError("assets-mismatch", "Checksum mismatch for " + label)
    return {**record, "path": path, "sha256": record["sha256"].lower()}


def inspect_runtime(record, label):
    """Reject escaping archives and check the actual entrypoint's ELF architecture."""
    entry = relative_name(record.get("entrypoint"))
    entries = {}
    try:
        with tarfile.open(record["path"], "r:*") as archive:
            for member in archive.getmembers():
                # A harmless top-level './' directory occurs in official bundles.
                if member.isdir() and member.name in (".", "./"):
                    continue
                name = relative_name(member.name)
                if name in entries:
                    raise AcceptanceError("assets-invalid", "Duplicate archive entry: " + label)
                if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                    raise AcceptanceError("assets-invalid", "Special archive entry: " + label)
                if member.issym() or member.islnk():
                    target = PurePosixPath(member.linkname)
                    if (target.is_absolute() or "\\" in member.linkname
                            or "\x00" in member.linkname or not member.linkname):
                        raise AcceptanceError("assets-invalid", "Unsafe archive link: " + label)
                entries[name] = member
            # No file may be extracted through a link in an earlier member.
            for name in entries:
                for parent in PurePosixPath(name).parents:
                    ancestor = entries.get(str(parent))
                    if ancestor is not None and not ancestor.isdir():
                        raise AcceptanceError("assets-invalid", "Non-directory archive ancestor: " + label)
            for name, member in entries.items():
                if not (member.issym() or member.islnk()):
                    continue
                # Symlinks are relative to their containing directory; tar
                # hardlink names are relative to the archive root. Official
                # Node bundles use bin/npm -> ../lib/node_modules/... .
                resolved = list(PurePosixPath(name).parent.parts) if member.issym() else []
                for part in PurePosixPath(member.linkname).parts:
                    if part == "..":
                        if not resolved:
                            raise AcceptanceError("assets-invalid", "Escaping archive link: " + label)
                        resolved.pop()
                    elif part != ".":
                        resolved.append(part)
                        target_entry = entries.get("/".join(resolved))
                        # Reject link-to-link traversal instead of collapsing
                        # '..' lexically across a symlink; that can resolve
                        # outside the extraction root at filesystem runtime.
                        if target_entry is not None and (target_entry.issym() or target_entry.islnk()):
                            raise AcceptanceError("assets-invalid", "Linked archive target: " + label)
            member = entries.get(entry)
            if member is None or not member.isfile():
                raise AcceptanceError("assets-invalid", "Missing regular runtime entrypoint: " + label)
            if not member.mode & 0o111:
                raise AcceptanceError("assets-incompatible", "Runtime entrypoint is not executable: " + label)
            stream = archive.extractfile(member)
            header = stream.read(20)
            if (len(header) != 20 or header[:4] != b"\x7fELF" or header[4:6] != b"\x02\x01"
                    or int.from_bytes(header[18:20], "little") != 183):
                raise AcceptanceError("assets-incompatible", "Runtime is not Linux ARM64: " + label)
            binary_digest = hashlib.sha256(header)
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                binary_digest.update(chunk)
            record["binarySha256"] = binary_digest.hexdigest()
    except (tarfile.TarError, OSError, EOFError):
        raise AcceptanceError("assets-invalid", "Unreadable runtime archive: " + label) from None
    record["entrypoint"] = entry


def load_manifest(path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AcceptanceError("assets-missing", "The assets manifest is missing.") from None
    except (OSError, ValueError):
        raise AcceptanceError("assets-invalid", "The assets manifest is not readable JSON.") from None
    if not isinstance(raw, dict) or raw.get("schemaVersion") != SCHEMA_VERSION:
        raise AcceptanceError("assets-invalid", "Unsupported assets manifest schema.")
    version = raw.get("limaVersion", "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", version):
        raise AcceptanceError("assets-invalid", "An exact Lima version pin is required.")
    image = check_asset(raw.get("image"), path.parent, "Ubuntu image")
    if image["architecture"] != "aarch64" or not re.match(r"^24\.04(?:[.-]|$)", image["version"]):
        raise AcceptanceError("assets-incompatible", "The image must pin Ubuntu 24.04 ARM64.")
    with image["path"].open("rb") as stream:
        if stream.read(4) != b"QFI\xfb":
            raise AcceptanceError("assets-incompatible", "The supplied image is not qcow2.")
    runtimes = {}
    if not isinstance(raw.get("runtimes"), dict):
        raise AcceptanceError("assets-invalid", "Missing runtime manifest.")
    for name in RUNTIME_NAMES:
        record = check_asset(raw["runtimes"].get(name), path.parent, name)
        if record["architecture"] != "aarch64":
            raise AcceptanceError("assets-incompatible", "Runtime architecture must be aarch64.")
        inspect_runtime(record, name)
        runtimes[name] = record
    if not isinstance(raw.get("packages"), list):
        raise AcceptanceError("assets-invalid", "An explicit offline package list is required.")
    packages = []
    for index, value in enumerate(raw["packages"]):
        record = check_asset(value, path.parent, "package " + str(index))
        record["installMode"] = record.get("installMode", "install")
        if record["installMode"] not in ("install", "extract-bubblewrap", "extract-bubblewrap-profile"):
            raise AcceptanceError("assets-invalid", "Unsupported offline package installation mode.")
        if record["architecture"] not in ("arm64", "all"):
            raise AcceptanceError("assets-incompatible", "Offline packages must be arm64 or all.")
        if record["installMode"] == "extract-bubblewrap" and record["architecture"] != "arm64":
            raise AcceptanceError("assets-incompatible", "The bubblewrap helper must be an arm64 package.")
        if record["installMode"] == "extract-bubblewrap-profile":
            if record["architecture"] != "all":
                raise AcceptanceError("assets-incompatible", "The standard profile must be an architecture-independent package.")
            if not isinstance(record.get("profileSha256"), str) or not re.fullmatch(r"[0-9a-fA-F]{64}", record["profileSha256"]):
                raise AcceptanceError("assets-invalid", "A separate SHA256 pin is required for the standard bubblewrap profile.")
            record["profileSha256"] = record["profileSha256"].lower()
        with record["path"].open("rb") as stream:
            if stream.read(8) != b"!<arch>\n":
                raise AcceptanceError("assets-incompatible", "Offline package is not a Debian archive.")
        packages.append(record)
    return {"limaVersion": version, "image": image, "runtimes": runtimes, "packages": packages}


def verify_common_client_pin(source, manifest):
    try:
        lock = json.loads((source / "config/client-acceptance-lock.json").read_text())
        expected_version = lock["clientVersion"]
        expected_hash = lock["platforms"]["linux-arm64"]["sha256"]
        expected_helper = lock["platforms"]["linux-arm64"]["codeModeHost"]
        helper_version, helper_archive, helper_binary = (expected_helper[key] for key in ("version", "sha256", "binarySha256"))
        if lock["schemaVersion"] != 1:
            raise ValueError("Unknown schema")
    except (OSError, ValueError, KeyError, TypeError):
        raise AcceptanceError("assets-invalid", "The common client asset lock is unavailable or invalid.") from None
    client = manifest["runtimes"]["client"]
    if client["version"] != expected_version or client["sha256"] != expected_hash:
        raise AcceptanceError("assets-mismatch", "Linux client does not match the common official client pin.")
    helper = manifest["runtimes"]["code_mode_host"]
    if (helper_version != expected_version or helper["version"] != expected_version
            or helper["sha256"] != helper_archive or helper["binarySha256"] != helper_binary):
        raise AcceptanceError("assets-mismatch", "Linux code-mode host does not match the same-version official helper pin.")


class Runner:
    """Quiet subprocess execution: no raw command, stdout, or stderr in evidence."""

    def __call__(self, argv, *, env=None, timeout=600, input=None, interactive=False):
        process = subprocess.Popen(argv, stdin=subprocess.PIPE,
                                   stdout=None if interactive else subprocess.PIPE,
                                   stderr=None if interactive else subprocess.PIPE,
                                   env=env, start_new_session=True)
        try:
            deadline = time.monotonic() + timeout
            pending_input = input
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    stdout, stderr = process.communicate(input=pending_input, timeout=min(30, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
                    print("Linux VM acceptance: operation still running within its time limit.", flush=True)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as interruption:
            # A partially completed communicate() can leave stdin closed. Clear
            # it before draining output, so interruption preserves its status
            # rather than becoming "flush of closed file" during cleanup.
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
                process.stdin = None
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
            code = "interrupted" if isinstance(interruption, KeyboardInterrupt) else "timeout"
            raise AcceptanceError(code, "A bounded VM command timed out or was interrupted.") from None
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def checked(result, stage):
    if result.returncode:
        raise AcceptanceError("command-failed", stage + " failed (exit " + str(result.returncode) + ").")
    return result.stdout


def source_snapshot(source, destination, runner):
    """Snapshot working bytes, including new implementation files, without .git."""
    revision = checked(runner(["git", "-C", str(source), "rev-parse", "HEAD"]), "Source revision")
    revision = revision.decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise AcceptanceError("source-invalid", "Source revision is invalid.")
    names = checked(runner(["git", "-C", str(source), "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard"]), "Source inventory")
    allowed_dirs = {".github", "agents", "backlog", "config", "docs", "scripts", "skills", "tests"}
    allowed_root = {"AGENTS.md", "CHANGELOG.md", "CHANGELOG.zh-CN.md", "FOLDER_STRUCTURE.md",
                    "NOTICE.md", "README.md", "README.zh-CN.md", "VERSION", ".gitignore"}
    hashes = {}
    with tarfile.open(destination, "w") as archive:
        for name in sorted(set(os.fsdecode(n) for n in names.split(b"\x00") if n)):
            name = relative_name(name)
            parts = PurePosixPath(name).parts
            if "__pycache__" in parts or name.endswith(".pyc"):
                continue
            if parts[0] not in allowed_dirs and name not in allowed_root:
                raise AcceptanceError("source-invalid", "Unrecognized source path; inspect the source inventory.")
            path = source / name
            if not path.exists():  # An intentional working-tree deletion is represented by absence.
                continue
            if (path.is_symlink() or not path.is_file() or source not in path.resolve().parents
                    or any(parent.is_symlink() for parent in path.parents if parent != source and source in parent.parents)):
                raise AcceptanceError("source-invalid", "Source snapshot contains a linked or non-file entry.")
            data = path.read_bytes()
            hashes[name] = hashlib.sha256(data).hexdigest()
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.mtime = len(data), path.stat().st_mode & 0o777, 0
            archive.addfile(entry, io.BytesIO(data))
    for required in ("scripts/test.ps1", "scripts/test-client-acceptance.py", "scripts/install-user.ps1"):
        if required not in hashes:
            raise AcceptanceError("source-invalid", "Source snapshot is missing a required acceptance entrypoint.")
    manifest = "".join(name + ":" + hashes[name] + "\n" for name in sorted(hashes))
    return {"commit": revision, "contentSha256": hashlib.sha256(manifest.encode()).hexdigest(),
            "fileCount": len(hashes), "archiveSha256": digest(destination)}


def lima_config(manifest):
    return {
        "vmType": "vz", "arch": "aarch64", "cpus": 4, "memory": "4GiB", "disk": "20GiB",
        "images": [{"location": str(manifest["image"]["path"]), "arch": "aarch64",
                    "digest": "sha256:" + manifest["image"]["sha256"]}],
        "mounts": [], "networks": [], "plain": True,
        "containerd": {"system": False, "user": False}, "upgradePackages": False,
        "propagateProxyEnv": False, "hostResolver": {"enabled": False}, "dns": ["127.0.0.1"],
        "ssh": {"loadDotSSHPubKeys": False, "forwardAgent": False, "forwardX11": False},
        "provision": [{"mode": "dependency", "skipDefaultDependencyResolution": True,
                       "script": "#!/bin/sh\nexit 0\n"}],
    }


def load_guard(source, *, proxy_url, allow_direct):
    spec = importlib.util.spec_from_file_location("_vm_client_acceptance", source / "scripts/test-client-acceptance.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.ConnectGuard(proxy_url=proxy_url, allow_direct=allow_direct, enabled=True).start()


def guest_bootstrap(root, manifest):
    packages = " ".join(shlex.quote(root + "/packages/" + str(i) + ".deb")
                        for i, package in enumerate(manifest["packages"])
                        if package.get("installMode", "install") == "install")
    commands = ["sudo -n dpkg -i -- " + packages] if packages else []
    for index, package in enumerate(manifest["packages"]):
        if package.get("installMode") == "extract-bubblewrap-profile":
            archive = shlex.quote(root + "/packages/" + str(index) + ".deb")
            destination = root + "/bubblewrap-profile-extract-" + str(index)
            profile = destination + "/usr/share/apparmor/extra-profiles/bwrap-userns-restrict"
            target = "/etc/apparmor.d/bwrap-userns-restrict"
            commands += [
                '[ "$(/usr/bin/dpkg-deb --field ' + archive + ' Package)" = apparmor-profiles ]',
                '[ "$(/usr/bin/dpkg-deb --field ' + archive + ' Version)" = ' + shlex.quote(package["version"]) + ' ]',
                '[ "$(/usr/bin/dpkg-deb --field ' + archive + ' Architecture)" = all ]',
                'mkdir ' + shlex.quote(destination),
                '/usr/bin/dpkg-deb --extract ' + archive + ' ' + shlex.quote(destination),
            ]
            commands += ['test ! -L ' + shlex.quote(destination + '/' + part)
                         for part in ('usr', 'usr/share', 'usr/share/apparmor', 'usr/share/apparmor/extra-profiles',
                                      'usr/share/apparmor/extra-profiles/bwrap-userns-restrict')]
            commands += [
                'test -f ' + shlex.quote(profile),
                'profile_digest=$(/usr/bin/sha256sum -- ' + shlex.quote(profile) + ')',
                '[ "${profile_digest%% *}" = ' + shlex.quote(package["profileSha256"]) + ' ]',
                'test ! -e ' + target, 'test ! -L ' + target,
                'aa_before=$(/usr/bin/cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)',
                'clone_before=$(/usr/bin/cat /proc/sys/kernel/unprivileged_userns_clone)',
                'sudo -n /usr/bin/install -o root -g root -m 0644 -- ' + shlex.quote(profile) + ' ' + target,
                '/usr/bin/cmp -s -- ' + shlex.quote(profile) + ' ' + target,
                '[ "$(/usr/bin/stat -c %u:%g:%a ' + target + ')" = 0:0:644 ]',
                'sudo -n /usr/sbin/apparmor_parser -r ' + target,
                'aa_after=$(/usr/bin/cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns)',
                'clone_after=$(/usr/bin/cat /proc/sys/kernel/unprivileged_userns_clone)',
                '[ "$aa_before" = "$aa_after" ]', '[ "$clone_before" = "$clone_after" ]',
                '''printf '{"apparmorRestrictionBefore":"%s","apparmorRestrictionAfter":"%s","usernsCloneBefore":"%s","usernsCloneAfter":"%s"}\\n' "$aa_before" "$aa_after" "$clone_before" "$clone_after" > '''
                + shlex.quote(root + '/bubblewrap-profile-policy.json'),
            ]
            continue
        if package.get("installMode") != "extract-bubblewrap":
            continue
        archive = shlex.quote(root + "/packages/" + str(index) + ".deb")
        destination = root + "/bubblewrap-extract-" + str(index)
        binary = destination + "/usr/bin/bwrap"
        # Extract as the ordinary guest user, then install only the verified
        # regular helper. The package's postinst and sysctl file never run/copy.
        commands += [
            '[ "$(/usr/bin/dpkg-deb --field ' + archive + ' Package)" = bubblewrap ]',
            '[ "$(/usr/bin/dpkg-deb --field ' + archive + ' Version)" = ' + shlex.quote(package["version"]) + ' ]',
            '[ "$(/usr/bin/dpkg-deb --field ' + archive + ' Architecture)" = arm64 ]',
            'mkdir ' + shlex.quote(destination),
            '/usr/bin/dpkg-deb --extract ' + archive + ' ' + shlex.quote(destination),
            'test ! -L ' + shlex.quote(destination + '/usr'),
            'test ! -L ' + shlex.quote(destination + '/usr/bin'),
            'test ! -L ' + shlex.quote(binary),
            'test -f ' + shlex.quote(binary),
            'sudo -n /usr/bin/install -o root -g root -m 0755 -- ' + shlex.quote(binary) + ' /usr/bin/bwrap',
            '/usr/bin/cmp -s -- ' + shlex.quote(binary) + ' /usr/bin/bwrap',
            '[ "$(/usr/bin/stat -c %u:%g:%a /usr/bin/bwrap)" = 0:0:755 ]',
        ]
    install = "\n".join(commands) or ":"
    return ("set -eu\n" + install + "\ncommand -v python3 >/dev/null\ncommand -v git >/dev/null\n"
            "command -v unshare >/dev/null\n"
            + "\n".join("mkdir -p " + shlex.quote(root + "/runtime/" + name) + "\n"
                          + "tar -xf " + shlex.quote(root + "/" + name + ".tar") + " -C "
                          + shlex.quote(root + "/runtime/" + name) for name in RUNTIME_NAMES)
            + "\n")


def guest_driver():
    # Fixed code, never credentials. Input contains only controlled runtime paths.
    return r'''import hashlib,json,os,pathlib,platform,subprocess,sys
root=pathlib.Path(sys.argv[1]); settings=json.loads((root/'settings.json').read_text())
source=root/'source'; results=root/'results'; results.mkdir(exist_ok=True)
runtime={k:root/'runtime'/k/v['entrypoint'] for k,v in settings['runtimes'].items()}
env=dict(os.environ)
for k in list(env):
    if k.lower().endswith('_proxy'): env.pop(k)
env.update(PATH=str(runtime['node'].parent)+':'+str(runtime['pwsh'].parent)+':'+env.get('PATH',''),
    POWERSHELL_TELEMETRY_OPTOUT='1',POWERSHELL_UPDATECHECK='Off',DOTNET_CLI_TELEMETRY_OPTOUT='1')
def run(args):
    return subprocess.run([str(x) for x in args],cwd=source,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=900)
os_release={}
for line in pathlib.Path('/etc/os-release').read_text().splitlines():
    if '=' in line:
        key,value=line.split('=',1);os_release[key]=value.strip('"')
probe=root/'case-probe';probe.mkdir();(probe/'item').write_text('lower');(probe/'ITEM').write_text('upper')
case_sensitive=(probe/'item').read_text()=='lower' and (probe/'ITEM').read_text()=='upper'
versions={}
for key,args in {'client':[runtime['client'],'--version'],'pwsh':[runtime['pwsh'],'-NoProfile','-Command','$PSVersionTable.PSVersion.ToString()'],'node':[runtime['node'],'--version'],'git':['git','--version'],'python':['python3','--version']}.items():
    r=run(args)
    if r.returncode: raise SystemExit(20)
    versions[key]=r.stdout.decode().strip()
valid=(os_release.get('ID')=='ubuntu' and os_release.get('VERSION_ID')=='24.04' and platform.machine() in ('aarch64','arm64') and case_sensitive)
for key in ('client','pwsh','node'):
    observed=versions[key].removeprefix('codex-cli ').removeprefix('PowerShell ').removeprefix('v')
    valid=valid and observed==settings['runtimes'][key]['version']
files={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*') if p.is_file()}
fingerprint=hashlib.sha256(''.join(k+':'+files[k]+'\n' for k in sorted(files)).encode()).hexdigest()
valid=valid and fingerprint==settings['source']['contentSha256']
provenance={'os':os_release.get('PRETTY_NAME'),'architecture':platform.machine(),'kernel':platform.release(),'filesystem':{'caseSensitive':case_sensitive},'runtimes':versions,'sourceContentSha256':fingerprint,'passed':valid}
provenance['codeModeHost']={'versionPin':settings['runtimes']['code_mode_host']['version'],
    'binarySha256':hashlib.sha256(runtime['code_mode_host'].read_bytes()).hexdigest()}
valid=valid and provenance['codeModeHost']['binarySha256']==settings['runtimes']['code_mode_host']['binarySha256']
provenance['passed']=valid
policy=pathlib.Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns')
profiles=pathlib.Path('/etc/apparmor.d')
standard_profile=pathlib.Path('/usr/share/apparmor/extra-profiles/bwrap-userns-restrict')
provenance['namespaceSupport']={'bubblewrapPresent':pathlib.Path('/usr/bin/bwrap').is_file(),
    'installedBubblewrapProfiles':sorted(p.name for p in profiles.glob('*bwrap*')) if profiles.is_dir() else [],
    'standardBubblewrapProfileAvailable':standard_profile.is_file(),
    'standardBubblewrapProfileSha256':hashlib.sha256(standard_profile.read_bytes()).hexdigest() if standard_profile.is_file() else None,
    'apparmorRestrictsUnprivilegedUserns':policy.read_text().strip()=='1' if policy.is_file() else None}
installed_profile=pathlib.Path('/etc/apparmor.d/bwrap-userns-restrict')
provenance['namespaceSupport']['installedBubblewrapProfileSha256']=hashlib.sha256(installed_profile.read_bytes()).hexdigest() if installed_profile.is_file() else None
profile_policy=root/'bubblewrap-profile-policy.json'
if profile_policy.is_file(): provenance['namespaceSupport']['profileProvisioning']=json.loads(profile_policy.read_text())
filesystem=run(['stat','-f','-c','%T',source])
if filesystem.returncode: raise SystemExit(23)
provenance['filesystem']['type']=filesystem.stdout.decode().strip()
(results/'provenance.json').write_text(json.dumps(provenance))
if not valid: raise SystemExit(21)
r=run([runtime['pwsh'],'-NoLogo','-NoProfile','-NonInteractive','-File',source/'scripts/test.ps1','-ResultPath',results/'suites.json'])
if r.returncode: raise SystemExit(22)
args=['python3',source/'scripts/test-client-acceptance.py','--client',runtime['client'],'--code-mode-host',runtime['code_mode_host'],'--pwsh',runtime['pwsh'],'--source-root',source,'--result',results/'client-preflight.json','--mode','preflight','--expected-client-version',settings['runtimes']['client']['version'],'--linux-root-netns']
r=run(args)
raise SystemExit(r.returncode)
'''


class VMRun:
    def __init__(self, args, runner=None):
        self.args = args
        self.runner = runner or Runner()
        self.name = "kit-acceptance-" + uuid.uuid4().hex[:12]
        self.created = False
        self.root = None
        self.guard = None
        self.result = {"schemaVersion": SCHEMA_VERSION, "target": "Ubuntu 24.04 ARM64",
                       "mode": args.mode, "passed": False, "status": "not-started",
                       "cleanup": {"vmDeleted": None, "temporaryStateRemoved": None}}

    def command(self, *args, timeout=600, input=None, interactive=False):
        return self.runner(["/usr/bin/sandbox-exec", "-p", NETWORK_POLICY,
                            str(self.args.lima), *map(str, args)], env=self.env,
                           timeout=timeout, input=input, interactive=interactive)

    def shell(self, script, timeout=600, interactive=False):
        return self.command("shell", "--workdir", "/tmp", self.name, "--", "/bin/sh", "-s",
                            input=script.encode(), timeout=timeout, interactive=interactive)

    def check_host(self):
        if platform.system() != "Darwin" or platform.machine() not in ("arm64", "aarch64"):
            raise AcceptanceError("compatibility-blocked", "Lima VZ acceptance requires a macOS ARM64 host.")
        if not self.args.lima.is_file() or not os.access(self.args.lima, os.X_OK):
            raise AcceptanceError("assets-missing", "Install the pinned Lima runtime before running this launcher.")
        if not Path("/usr/bin/sandbox-exec").is_file():
            raise AcceptanceError("compatibility-blocked", "The required host network sandbox is unavailable.")

    def check_network_confinement(self):
        # This probe only touches loopback. 127.0.0.2 is deliberately outside
        # the policy's allowed endpoints; EPERM verifies the outbound rule.
        probe = ("import errno,socket; listener=socket.socket(); "
                 "listener.bind(('127.0.0.1',0)); listener.listen(1); allowed=socket.socket(); "
                 "allowed.connect(listener.getsockname()); accepted,_=listener.accept(); "
                 "accepted.close(); allowed.close(); listener.close(); s=socket.socket(); "
                 "r=s.connect_ex(('127.0.0.2',9)); "
                 "raise SystemExit(0 if r in (errno.EPERM,errno.EACCES) else 1)")
        result = self.runner(["/usr/bin/sandbox-exec", "-p", NETWORK_POLICY,
                              sys.executable, "-c", probe], env=self.env, timeout=15)
        if result.returncode:
            raise AcceptanceError("compatibility-blocked", "Host outbound confinement could not be established.")

    def prepare(self, manifest):
        # macOS' default /var/folders temporary path can make Lima's nested
        # control sockets exceed UNIX_PATH_MAX (104 bytes).
        self.root = Path(tempfile.mkdtemp(prefix="kit-vm-", dir="/private/tmp"))
        self.root.chmod(0o700)
        self.env = {k: v for k, v in os.environ.items()
                    if k in ("PATH", "HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "TERM") or k.startswith("LC_")}
        self.env["LIMA_HOME"] = str(self.root / "lima")
        Path(self.env["LIMA_HOME"]).mkdir(mode=0o700)
        self.env.pop("SSH_AUTH_SOCK", None)
        self.check_network_confinement()
        observed = checked(self.command("--version", timeout=20), "Lima version probe").decode().strip()
        if observed != "limactl version " + manifest["limaVersion"]:
            raise AcceptanceError("assets-incompatible", "The installed Lima version does not match its pin.")
        payload = self.root / "payload"
        payload.mkdir()
        source_tar = payload / "source.tar"
        self.result["source"] = source_snapshot(self.args.source_root.resolve(), source_tar, self.runner)
        for name, record in manifest["runtimes"].items():
            shutil.copyfile(record["path"], payload / (name + ".tar"))
            if digest(payload / (name + ".tar")) != record["sha256"]:
                raise AcceptanceError("assets-mismatch", "Runtime changed while preparing the VM.")
        (payload / "packages").mkdir()
        for index, record in enumerate(manifest["packages"]):
            target = payload / "packages" / (str(index) + ".deb")
            shutil.copyfile(record["path"], target)
            if digest(target) != record["sha256"]:
                raise AcceptanceError("assets-mismatch", "Package changed while preparing the VM.")
        settings = {"source": self.result["source"], "runtimes": {
            name: {key: record[key] for key in ("version", "entrypoint", "binarySha256")}
            for name, record in manifest["runtimes"].items()}}
        (payload / "settings.json").write_text(json.dumps(settings))
        (payload / "guest-driver.py").write_text(guest_driver())
        bundle = self.root / "payload.tar"
        with tarfile.open(bundle, "w") as archive:
            archive.add(payload, arcname=".", recursive=True)
        config = self.root / "lima.yaml"
        # JSON is also YAML; using its encoder avoids path/quote interpolation.
        config.write_text(json.dumps(lima_config(manifest)))
        checked(self.command("validate", config, timeout=30), "Lima configuration validation")
        self.result["assets"] = {"limaVersion": manifest["limaVersion"],
                                 "imageVersion": manifest["image"]["version"],
                                 "imageSha256": manifest["image"]["sha256"],
                                 "runtimes": {k: {f: v[f] for f in ("version", "sha256", "binarySha256")}
                                              for k, v in manifest["runtimes"].items()},
                                 "packageCount": len(manifest["packages"]),
                                 "packages": [{key: package[key] for key in ("version", "sha256", "installMode", "profileSha256") if key in package}
                                              for package in manifest["packages"]]}
        return config, bundle

    def collect(self, guest_root):
        for label, filename in (("provenance", "provenance.json"), ("suites", "suites.json"),
                                ("clientPreflight", "client-preflight.json"), ("clientLive", "client-live.json")):
            local = self.root / filename
            result = self.command("copy", self.name + ":" + guest_root + "/results/" + filename,
                                  local, timeout=30)
            if result.returncode or not local.is_file():
                continue
            if local.stat().st_size > 16 * 1024 * 1024:
                raise AcceptanceError("evidence-invalid", "Guest evidence exceeds its bounded size.")
            try:
                value = json.loads(local.read_text())
                if not isinstance(value, dict):
                    raise ValueError("Expected object")
                self.result[label] = value
            except (ValueError, OSError):
                raise AcceptanceError("evidence-invalid", "Guest evidence is not valid JSON.") from None

    def live(self, manifest, guest_root):
        proxy_url = os.environ.get(self.args.proxy_url_env) if self.args.proxy_url_env else None
        self.guard = load_guard(self.args.source_root.resolve(), proxy_url=proxy_url,
                                allow_direct=self.args.allow_direct)
        # The only URL entering the guest is a credential-free local bridge.
        local_proxy = "http://192.168.5.2:" + str(self.guard.port)
        path = guest_root + "/runtime/"
        common = ["python3", guest_root + "/source/scripts/test-client-acceptance.py",
                  "--client", path + "client/" + manifest["runtimes"]["client"]["entrypoint"],
                  "--code-mode-host", path + "code_mode_host/" + manifest["runtimes"]["code_mode_host"]["entrypoint"],
                  "--pwsh", path + "pwsh/" + manifest["runtimes"]["pwsh"]["entrypoint"],
                  "--source-root", guest_root + "/source", "--result", guest_root + "/results/client-live.json",
                  "--mode", "live", "--expected-client-version", manifest["runtimes"]["client"]["version"],
                  "--proxy-url-env", "VM_ACCEPTANCE_PROXY", "--allow-installation-id", "--linux-root-netns"]
        route = "approved direct access" if self.args.allow_direct else "the approved proxy"
        print("Linux live acceptance: chatgpt.com, api.openai.com, auth.openai.com via " + route
              + " through the local guard, for authentication and one review; installation ID "
              "disclosure is acknowledged for this invocation.", flush=True)
        script = ("export VM_ACCEPTANCE_PROXY=" + shlex.quote(local_proxy)
                  + "\nexport PATH=" + shlex.quote(str(PurePosixPath(path + "node/" + manifest["runtimes"]["node"]["entrypoint"]).parent))
                  + ':"$PATH"\nexec ' + shlex.join(common) + "\n")
        # The harness presents only the device-login challenge to the interactive
        # console; that challenge and other raw client output are never retained.
        result = self.shell(script, timeout=1260, interactive=True)
        self.result["route"] = self.guard.summary()
        self.collect(guest_root)
        checked(result, "Linux live-client workflow")

    def cleanup(self):
        cleanup = self.result["cleanup"]
        failures = []
        if self.created:
            # Each operation is independent: a hung stop must not prevent force
            # deletion or a final inventory proving whether the VM remains.
            for operation in ("stop", "delete"):
                try:
                    response = self.command(operation, "--force", self.name, timeout=60)
                    if response.returncode:
                        failures.append(operation)
                except (Exception, KeyboardInterrupt):
                    failures.append(operation)
            gone = False
            try:
                remaining = self.command("list", "--json", timeout=20)
                if remaining.returncode == 0:
                    instances = [json.loads(line) for line in remaining.stdout.decode().splitlines() if line.strip()]
                    gone = all(isinstance(item, dict) and isinstance(item.get("name"), str)
                               and item["name"] != self.name for item in instances)
                else:
                    failures.append("list")
            except (Exception, KeyboardInterrupt):
                failures.append("list")
            cleanup["vmDeleted"] = gone
        else:
            cleanup["vmDeleted"] = True
        if failures:
            cleanup["commandFailures"] = failures
        if self.guard is not None:
            try:
                self.guard.close()
            except (Exception, KeyboardInterrupt):
                cleanup["guardShutdownFailed"] = True
        if self.root and cleanup["vmDeleted"]:
            try:
                shutil.rmtree(self.root)
                cleanup["temporaryStateRemoved"] = not self.root.exists()
            except OSError:
                cleanup["temporaryStateRemoved"] = False
        elif self.root:
            cleanup["temporaryStateRemoved"] = False
        else:
            cleanup["temporaryStateRemoved"] = True
        if not cleanup["vmDeleted"] or not cleanup["temporaryStateRemoved"]:
            # Keep actionable identifiers even when no final list was available.
            cleanup["recoveryRequired"] = True
            cleanup["recovery"] = {"vmName": self.name, "stateDirectory": str(self.root),
                                   "limaHome": str(self.root / 'lima') if self.root else None}
        if cleanup.get("recoveryRequired") or cleanup.get("guardShutdownFailed"):
            self.result.update(passed=False, status="cleanup-failed")

    def execute(self):
        try:
            self.check_host()
            print("Linux VM preflight: verifying supplied asset pins and ARM64 archives.", flush=True)
            manifest = load_manifest(self.args.assets_manifest.resolve())
            verify_common_client_pin(self.args.source_root.resolve(), manifest)
            if self.args.mode == "live":
                if not self.args.allow_installation_id:
                    raise AcceptanceError("approval-required", "Live mode requires installation-ID approval for this run.")
                if bool(self.args.proxy_url_env) == bool(self.args.allow_direct):
                    raise AcceptanceError("approval-required", "Select exactly one approved route for this run.")
                if self.args.proxy_url_env and not os.environ.get(self.args.proxy_url_env):
                    raise AcceptanceError("route-unavailable", "The approved proxy environment variable is empty.")
            config, bundle = self.prepare(manifest)
            print("Linux VM preflight: creating and starting the isolated Ubuntu guest.", flush=True)
            self.created = True  # Even a partially failed create must be cleaned up.
            checked(self.command("create", "--tty=false", "--name", self.name, config), "VM creation")
            checked(self.command("start", "--tty=false", self.name), "VM startup")
            print("Linux VM preflight: transferring the fingerprinted source and offline runtimes.", flush=True)
            guest_root = "/tmp/" + self.name
            checked(self.shell("set -eu\numask 077\nmkdir " + shlex.quote(guest_root)), "Guest fixture creation")
            checked(self.command("copy", bundle, self.name + ":" + guest_root + "/payload.tar"), "Asset transfer")
            unpack = ("set -eu\ncd " + shlex.quote(guest_root) + "\ntar -xf payload.tar\nmkdir source\n"
                      "tar -xf source.tar -C source\n")
            checked(self.shell(unpack + guest_bootstrap(guest_root, manifest)), "Offline guest provisioning")
            print("Linux VM preflight: running the 14 native checks and fresh client preflight.", flush=True)
            offline = self.shell("exec python3 " + shlex.quote(guest_root + "/guest-driver.py") + " "
                                 + shlex.quote(guest_root), timeout=1800)
            self.collect(guest_root)
            checked(offline, "Linux offline suites and client preflight")
            if (self.result.get("provenance", {}).get("passed") is not True
                    or self.result.get("clientPreflight", {}).get("passed") is not True):
                raise AcceptanceError("evidence-incomplete", "Guest provenance and client preflight must pass.")
            suites = self.result.get("suites", {})
            checks = suites.get("checks", [])
            if (suites.get("passed") is not True or len(checks) != 14
                    or {item.get("Suite") for item in checks} != SUITES
                    or any(item.get("ExitCode") != 0 for item in checks)):
                raise AcceptanceError("evidence-incomplete", "All 14 unique Linux checks must pass.")
            if self.args.mode == "live":
                self.live(manifest, guest_root)
                if self.result.get("clientLive", {}).get("passed") is not True:
                    raise AcceptanceError("evidence-incomplete", "The live-client evidence must pass.")
            self.result.update(status="passed", passed=True)
        except AcceptanceError as error:
            self.result.update(status=error.code, error=str(error), passed=False)
        except KeyboardInterrupt:
            self.result.update(status="interrupted", error="The VM acceptance run was interrupted.", passed=False)
        except (OSError, ValueError, KeyError, TypeError, ImportError):
            self.result.update(status="setup-failed", error="VM setup or evidence processing failed; no raw diagnostics retained.")
        finally:
            try:
                print("Linux VM acceptance: cleaning up owned VM and temporary state.", flush=True)
                self.cleanup()
            except (OSError, AcceptanceError):
                self.result.update(status="cleanup-failed", passed=False)
                self.result["cleanup"]["temporaryStateRemoved"] = False
            finally:
                if self.guard is not None:
                    # An interrupted interactive shell never reaches live()'s
                    # ordinary return. Keep observed destinations in that case.
                    try:
                        self.result["route"] = self.guard.summary()
                    except Exception:
                        self.result["routeEvidenceUnavailable"] = True
                        self.result["passed"] = False
                        if self.result["status"] != "cleanup-failed":
                            self.result["status"] = "evidence-incomplete"
        return self.result


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in ("lima", "assets-manifest", "source-root", "result"):
        parser.add_argument("--" + flag, required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("preflight", "live"))
    route = parser.add_mutually_exclusive_group()
    route.add_argument("--proxy-url-env")
    route.add_argument("--allow-direct", action="store_true")
    parser.add_argument("--allow-installation-id", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    # Never overwrite earlier acceptance attempts, follow output symlinks, or
    # create parent directories whose ownership is not established.
    if args.result.exists() or args.result.is_symlink() or not args.result.parent.is_dir():
        print("Result destination must be a new file in an existing directory.", file=sys.stderr)
        return 2
    result = VMRun(args).execute()
    with args.result.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
    print("Linux VM acceptance: " + result["status"])
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
