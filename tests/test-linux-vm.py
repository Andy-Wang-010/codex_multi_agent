#!/usr/bin/env python3
"""Offline VM-launcher regressions; never starts Lima or opens external sockets."""

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


sys.dont_write_bytecode = True
SPEC = importlib.util.spec_from_file_location("linux_vm", Path(__file__).parents[1] / "scripts/test-linux-vm.py")
VM = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VM)


def write_archive(path, entry="bin/runtime", machine=183, extra=()):
    header = bytearray(20)
    header[:6] = b"\x7fELF\x02\x01"
    header[18:20] = machine.to_bytes(2, "little")
    with tarfile.open(path, "w:gz") as archive:
        member = tarfile.TarInfo(entry)
        member.size = len(header)
        member.mode = 0o755
        archive.addfile(member, io.BytesIO(header))
        for member in extra:
            archive.addfile(member)


class FakeRunner:
    def __init__(self, source, version="2.0.0"):
        self.source, self.version = source, version
        self.calls = []
        self.instance = None
        self.fail_stage = None
        self.leave_instance = False
        self.evidence = {
            "provenance.json": {"passed": True, "architecture": "aarch64"},
            "suites.json": {"passed": True, "checks": [
                {"Suite": s, "ExitCode": 0} for s in sorted(VM.SUITES)]},
            "client-preflight.json": {"passed": True},
            "client-live.json": {"passed": True},
        }

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        stdout, stderr, code = b"", b"private diagnostic must not be persisted", 0
        if argv[0] == "git":
            if "rev-parse" in argv:
                stdout = b"a" * 40 + b"\n"
            else:
                stdout = b"\x00".join(str(p.relative_to(self.source)).encode()
                                       for p in self.source.rglob("*") if p.is_file()) + b"\x00"
        else:
            # Every Lima operation must inherit the same external-network deny.
            assert argv[:3] == ["/usr/bin/sandbox-exec", "-p", VM.NETWORK_POLICY]
            command = argv[4:]
            operation = command[0]
            if operation == "--version":
                stdout = ("limactl version " + self.version + "\n").encode()
            elif operation == "create":
                self.instance = command[command.index("--name") + 1]
            elif operation == "delete":
                if not self.leave_instance:
                    self.instance = None
            elif operation == "list":
                stdout = ((json.dumps({"name": self.instance}) + "\n").encode()
                          if self.instance else b"")
            elif operation == "copy" and ":/tmp/" in command[1]:
                filename = command[1].rsplit("/", 1)[1]
                if filename not in self.evidence:
                    code = 1
                else:
                    Path(command[2]).write_text(json.dumps(self.evidence[filename]))
            if operation == self.fail_stage:
                code = 17
        return subprocess.CompletedProcess(argv, code, stdout, stderr)


class FakeGuard:
    port = 12345

    def __init__(self):
        self.closed = False

    def summary(self):
        return {"route": "approved proxy", "connections": []}

    def close(self):
        self.closed = True


class VMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="vm-launcher-test-")
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        (self.source / "scripts").mkdir(parents=True)
        for name in ("test.ps1", "test-client-acceptance.py", "install-user.ps1"):
            (self.source / "scripts" / name).write_text("fixture\n")
        (self.source / "VERSION").write_text("0.10.2\n")
        image = self.root / "ubuntu.qcow2"
        image.write_bytes(b"QFI\xfbtest image")
        self.manifest = {"schemaVersion": 1, "limaVersion": "2.0.0", "image": {
            "path": image.name, "sha256": VM.digest(image), "version": "24.04.5-20261001",
            "architecture": "aarch64"}, "runtimes": {}, "packages": []}
        for name in VM.RUNTIME_NAMES:
            path = self.root / (name + ".tar.gz")
            write_archive(path)
            self.manifest["runtimes"][name] = {"path": path.name, "sha256": VM.digest(path),
                "version": "1.2.3", "architecture": "aarch64", "entrypoint": "bin/runtime"}
        (self.source / "config").mkdir()
        with tarfile.open(self.root / 'code_mode_host.tar.gz') as archive:
            helper_binary_sha = hashlib.sha256(archive.extractfile('bin/runtime').read()).hexdigest()
        (self.source / "config/client-acceptance-lock.json").write_text(json.dumps({
            "schemaVersion": 1, "clientVersion": "1.2.3", "platforms": {"linux-arm64": {
                "sha256": self.manifest["runtimes"]["client"]["sha256"],
                "codeModeHost": {"version": "1.2.3", "sha256": self.manifest["runtimes"]["code_mode_host"]["sha256"],
                                 "binarySha256": helper_binary_sha}}}}))
        self.manifest_path = self.root / "assets.json"
        self.save_manifest()
        self.args = argparse.Namespace(lima=self.root / "limactl", assets_manifest=self.manifest_path,
            source_root=self.source, result=self.root / "result.json", mode="preflight",
            proxy_url_env=None, allow_direct=False, allow_installation_id=False)
        self.runner = FakeRunner(self.source)
        self.run = VM.VMRun(self.args, self.runner)
        self.run.check_host = lambda: None
        self.run.check_network_confinement = lambda: None

    def tearDown(self):
        # A fake cleanup failure deliberately retains its temporary Lima state.
        if self.run.root and self.run.root.exists():
            shutil.rmtree(self.run.root)
        self.temp.cleanup()

    def save_manifest(self):
        self.manifest_path.write_text(json.dumps(self.manifest))

    def commands(self):
        return [argv[4:] for argv, _ in self.runner.calls if argv[0] != "git"]

    def assert_no_vm(self):
        self.assertFalse(any(cmd[0] in ("create", "start") for cmd in self.commands()))

    def test_success_retains_native_evidence_and_deletes_only_owned_vm(self):
        result = self.run.execute()
        self.assertTrue(result["passed"], result)
        self.assertEqual(len(result["suites"]["checks"]), 14)
        self.assertEqual(result["source"]["fileCount"], 5)
        self.assertTrue(result["cleanup"]["vmDeleted"])
        self.assertFalse(self.run.root.exists())
        delete = [cmd for cmd in self.commands() if cmd[0] == "delete"]
        self.assertEqual(delete, [["delete", "--force", self.run.name]])
        self.assertNotIn("private diagnostic", json.dumps(result))
        for argv, kwargs in self.runner.calls:
            if argv[0] != "git":
                self.assertEqual(Path(kwargs["env"]["LIMA_HOME"]).parent, self.run.root)

    def test_missing_asset_fails_before_any_vm_creation(self):
        (self.root / "node.tar.gz").unlink()
        result = self.run.execute()
        self.assertEqual(result["status"], "assets-missing")
        self.assert_no_vm()

    def test_wrong_checksum_fails_before_any_vm_creation(self):
        self.manifest["image"]["sha256"] = "0" * 64
        self.save_manifest()
        result = self.run.execute()
        self.assertEqual(result["status"], "assets-mismatch")
        self.assert_no_vm()

    def test_actual_non_arm_runtime_is_refused(self):
        path = self.root / "client.tar.gz"
        write_archive(path, machine=62)
        self.manifest["runtimes"]["client"]["sha256"] = VM.digest(path)
        self.save_manifest()
        result = self.run.execute()
        self.assertEqual(result["status"], "assets-incompatible")
        self.assert_no_vm()

    def test_nonexecutable_helper_fails_before_vm_creation(self):
        path = self.root / 'code_mode_host.tar.gz'
        with tarfile.open(path, 'r:gz') as archive:
            content = archive.extractfile('bin/runtime').read()
        with tarfile.open(path, 'w:gz') as archive:
            member = tarfile.TarInfo('bin/runtime')
            member.size, member.mode = len(content), 0o644
            archive.addfile(member, io.BytesIO(content))
        self.manifest['runtimes']['code_mode_host']['sha256'] = VM.digest(path)
        self.save_manifest()
        self.assertEqual(self.run.execute()['status'], 'assets-incompatible')
        self.assert_no_vm()

    def test_manifest_cannot_substitute_a_different_common_client(self):
        self.manifest["runtimes"]["client"]["version"] = "9.9.9"
        self.save_manifest()
        self.assertEqual(self.run.execute()["status"], "assets-mismatch")
        self.assert_no_vm()

    def test_helper_must_be_supplied_and_match_client_release_and_binary_pin(self):
        original = copy.deepcopy(self.manifest)
        for mutation in ('missing', 'version', 'binary'):
            self.manifest = copy.deepcopy(original)
            with self.subTest(mutation=mutation):
                if mutation == 'missing':
                    del self.manifest['runtimes']['code_mode_host']
                elif mutation == 'version':
                    self.manifest['runtimes']['code_mode_host']['version'] = '9.9.9'
                else:
                    lock_path = self.source / 'config/client-acceptance-lock.json'
                    lock = json.loads(lock_path.read_text())
                    lock['platforms']['linux-arm64']['codeModeHost']['binarySha256'] = '0' * 64
                    lock_path.write_text(json.dumps(lock))
                self.save_manifest()
                self.assertIn(self.run.execute()['status'], ('assets-invalid', 'assets-mismatch'))
                self.assert_no_vm()

    def test_archive_path_traversal_is_refused(self):
        path = self.root / "client.tar.gz"
        write_archive(path, extra=[tarfile.TarInfo("../../escaped")])
        self.manifest["runtimes"]["client"]["sha256"] = VM.digest(path)
        self.save_manifest()
        self.assertEqual(self.run.execute()["status"], "assets-invalid")
        self.assertFalse((self.root / "escaped").exists())
        self.assert_no_vm()

    def test_archive_file_under_symlink_is_refused(self):
        link = tarfile.TarInfo("nested")
        link.type, link.linkname = tarfile.SYMTYPE, "other"
        path = self.root / "client.tar.gz"
        write_archive(path, extra=[link, tarfile.TarInfo("nested/file")])
        self.manifest["runtimes"]["client"]["sha256"] = VM.digest(path)
        self.save_manifest()
        self.assertEqual(self.run.execute()["status"], "assets-invalid")
        self.assert_no_vm()

    def test_official_node_style_relative_symlink_stays_inside_archive(self):
        directory = tarfile.TarInfo("node-v24-linux-arm64/lib/node_modules/npm/bin")
        directory.type = tarfile.DIRTYPE
        cli = tarfile.TarInfo("node-v24-linux-arm64/lib/node_modules/npm/bin/npm-cli.js")
        link = tarfile.TarInfo("node-v24-linux-arm64/bin/npm")
        link.type, link.linkname = tarfile.SYMTYPE, "../lib/node_modules/npm/bin/npm-cli.js"
        path = self.root / "node.tar.gz"
        write_archive(path, entry="node-v24-linux-arm64/bin/node", extra=[directory, cli, link])
        record = {"path": path, "entrypoint": "node-v24-linux-arm64/bin/node"}
        VM.inspect_runtime(record, "node")
        self.assertEqual(record["entrypoint"], "node-v24-linux-arm64/bin/node")

    def test_relative_symlink_cannot_escape_archive_root(self):
        link = tarfile.TarInfo("node/bin/npm")
        link.type, link.linkname = tarfile.SYMTYPE, "../../../outside"
        path = self.root / "node.tar.gz"
        write_archive(path, extra=[link])
        with self.assertRaises(VM.AcceptanceError) as failure:
            VM.inspect_runtime({"path": path, "entrypoint": "bin/runtime"}, "node")
        self.assertEqual(failure.exception.code, "assets-invalid")

    def test_relative_link_cannot_traverse_another_link_before_dotdot(self):
        first = tarfile.TarInfo("sub/shortcut")
        first.type, first.linkname = tarfile.SYMTYPE, "../shallow"
        second = tarfile.TarInfo("escape")
        second.type, second.linkname = tarfile.SYMTYPE, "sub/shortcut/../../outside"
        path = self.root / "node.tar.gz"
        write_archive(path, extra=[first, second])
        with self.assertRaises(VM.AcceptanceError):
            VM.inspect_runtime({"path": path, "entrypoint": "bin/runtime"}, "node")

    def test_hardlink_target_is_relative_to_archive_root(self):
        link = tarfile.TarInfo("nested/link")
        link.type, link.linkname = tarfile.LNKTYPE, "../outside"
        path = self.root / "node.tar.gz"
        write_archive(path, extra=[link])
        with self.assertRaises(VM.AcceptanceError):
            VM.inspect_runtime({"path": path, "entrypoint": "bin/runtime"}, "node")

    def test_exact_lima_version_is_required(self):
        self.runner.version = "2.0.1"
        self.assertEqual(self.run.execute()["status"], "assets-incompatible")
        self.assert_no_vm()

    def test_config_has_no_download_dependencies_or_shared_mounts(self):
        config = VM.lima_config(VM.load_manifest(self.manifest_path))
        self.assertEqual((config["vmType"], config["cpus"], config["memory"], config["disk"]),
                         ("vz", 4, "4GiB", "20GiB"))
        self.assertEqual(config["mounts"], [])
        self.assertEqual(config["networks"], [])
        self.assertTrue(config["plain"])
        self.assertFalse(config["hostResolver"]["enabled"])
        self.assertFalse(config["propagateProxyEnv"])
        self.assertTrue(config["provision"][0]["skipDefaultDependencyResolution"])
        self.assertNotIn("https://", json.dumps(config))
        self.assertNotIn("vzNAT", config)

    def test_uncertain_host_confinement_blocks_before_vm_creation(self):
        def reject():
            raise VM.AcceptanceError("compatibility-blocked", "Sandbox unavailable.")
        self.run.check_network_confinement = reject
        self.assertEqual(self.run.execute()["status"], "compatibility-blocked")
        self.assert_no_vm()

    def test_source_snapshot_includes_working_changes_and_refuses_unknown_personal_files(self):
        (self.source / "personal-auth.json").write_text("secret")
        result = self.run.execute()
        self.assertEqual(result["status"], "source-invalid")
        self.assertNotIn("secret", json.dumps(result))
        self.assert_no_vm()

    def test_source_links_are_refused(self):
        (self.source / "scripts" / "linked").symlink_to(self.source / "VERSION")
        self.assertEqual(self.run.execute()["status"], "source-invalid")
        self.assert_no_vm()

    def test_partial_start_failure_still_deletes_owned_vm(self):
        self.runner.fail_stage = "start"
        result = self.run.execute()
        self.assertEqual(result["status"], "command-failed")
        self.assertTrue(result["cleanup"]["vmDeleted"])
        self.assertFalse(self.run.root.exists())

    def test_cleanup_failure_is_not_a_pass_and_retains_recovery_state(self):
        self.runner.leave_instance = True
        result = self.run.execute()
        self.assertEqual(result["status"], "cleanup-failed")
        self.assertFalse(result["passed"])
        self.assertTrue(self.run.root.exists())
        self.assertTrue(result["cleanup"]["recoveryRequired"])
        self.assertEqual(result["cleanup"]["recovery"]["vmName"], self.run.name)
        self.assertEqual(result["cleanup"]["recovery"]["limaHome"], str(self.run.root / 'lima'))

    def test_stop_timeout_still_attempts_delete_and_verifies_absence(self):
        original = self.run.command

        def command(operation, *args, **kwargs):
            if operation == "stop":
                raise VM.AcceptanceError("timeout", "Synthetic stop timeout")
            return original(operation, *args, **kwargs)

        self.run.command = command
        result = self.run.execute()
        self.assertTrue(result["passed"], result)
        self.assertTrue(result["cleanup"]["vmDeleted"])
        self.assertTrue(result["cleanup"]["temporaryStateRemoved"])
        self.assertEqual(result["cleanup"]["commandFailures"], ["stop"])
        self.assertEqual([cmd[0] for cmd in self.commands() if cmd[0] in ('delete', 'list')], ['delete', 'list'])

    def test_stop_delete_and_list_failures_retain_actionable_owned_recovery(self):
        original = self.run.command
        attempted = []

        def command(operation, *args, **kwargs):
            if operation in ("stop", "delete", "list"):
                attempted.append(operation)
                raise VM.AcceptanceError("timeout", "Synthetic cleanup timeout")
            return original(operation, *args, **kwargs)

        self.run.command = command
        result = self.run.execute()
        self.assertEqual(attempted, ['stop', 'delete', 'list'])
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "cleanup-failed")
        self.assertFalse(result["cleanup"]["vmDeleted"])
        self.assertTrue(self.run.root.is_dir())
        self.assertEqual(result["cleanup"]["recovery"]["vmName"], self.run.name)
        self.assertEqual(result["cleanup"]["recovery"]["stateDirectory"], str(self.run.root))

    def test_missing_suite_or_duplicate_suite_is_not_a_pass(self):
        checks = self.runner.evidence["suites.json"]["checks"]
        checks[-1] = copy.deepcopy(checks[0])
        result = self.run.execute()
        self.assertEqual(result["status"], "evidence-incomplete")
        self.assertTrue(result["cleanup"]["vmDeleted"])

    def test_absent_client_evidence_is_not_a_pass(self):
        self.runner.evidence.pop("client-preflight.json")
        self.assertEqual(self.run.execute()["status"], "evidence-incomplete")

    def test_failed_offline_preflight_never_opens_guard(self):
        self.args.mode, self.args.allow_direct, self.args.allow_installation_id = "live", True, True
        self.runner.evidence["client-preflight.json"]["passed"] = False
        with mock.patch.object(VM, "load_guard") as guard:
            self.assertFalse(self.run.execute()["passed"])
            guard.assert_not_called()

    def test_live_requires_both_new_route_and_identifier_acknowledgment(self):
        self.args.mode = "live"
        result = self.run.execute()
        self.assertEqual(result["status"], "approval-required")
        self.assert_no_vm()
        self.args.allow_installation_id = True
        self.assertEqual(self.run.execute()["status"], "approval-required")
        self.assert_no_vm()

    def test_live_proxy_credentials_remain_only_in_host_guard_memory(self):
        self.args.mode, self.args.proxy_url_env, self.args.allow_installation_id = "live", "VM_TEST_ROUTE", True
        proxy = "http://secret-user:secret-pass@proxy.invalid:123"
        guard = FakeGuard()
        with mock.patch.dict(os.environ, {"VM_TEST_ROUTE": proxy}):
            with mock.patch.object(VM, "load_guard", return_value=guard) as factory:
                result = self.run.execute()
                factory.assert_called_once_with(self.source.resolve(), proxy_url=proxy, allow_direct=False)
        self.assertTrue(result["passed"], result)
        self.assertTrue(guard.closed)
        self.assertNotIn(proxy, repr(self.runner.calls))
        self.assertNotIn("secret-pass", json.dumps(result))
        interactive = [kw for _, kw in self.runner.calls if kw.get("interactive")]
        self.assertEqual(len(interactive), 1)
        self.assertIn(b"http://192.168.5.2:12345", interactive[0]["input"])
        self.assertIn(b"--allow-installation-id", interactive[0]["input"])
        self.assertIn(b"--linux-root-netns", interactive[0]["input"])
        self.assertIn(b"--code-mode-host", interactive[0]["input"])

    def test_interrupted_live_preserves_observed_guard_destinations_after_cleanup(self):
        self.args.mode, self.args.allow_direct, self.args.allow_installation_id = "live", True, True
        guard = FakeGuard()
        original = self.runner.__call__

        def interrupt(argv, **kwargs):
            if kwargs.get('interactive'):
                raise VM.AcceptanceError('interrupted', 'Synthetic live interruption')
            return original(argv, **kwargs)

        self.run.runner = interrupt
        guard.summary = lambda: {'route': 'approved direct', 'connections': [{'host': 'auth.openai.com', 'count': 1}]}
        with mock.patch.object(VM, 'load_guard', return_value=guard):
            result = self.run.execute()
        self.assertEqual(result['status'], 'interrupted')
        self.assertEqual(result['route']['connections'], [{'host': 'auth.openai.com', 'count': 1}])
        self.assertTrue(guard.closed)
        self.assertTrue(result['cleanup']['vmDeleted'])
        self.assertTrue(result['cleanup']['temporaryStateRemoved'])

    def test_owned_guest_preflight_uses_explicit_privileged_namespace_opt_in(self):
        driver = VM.guest_driver()
        self.assertIn("'--linux-root-netns'", driver)
        self.assertNotIn("policy.write", driver)
        self.assertNotIn("sysctl -w", driver)

    def test_bubblewrap_extraction_avoids_maintainer_scripts_and_sysctl_files(self):
        manifest = {"packages": [{"installMode": "extract-bubblewrap", "version": "0.9.0-1ubuntu0.3"}]}
        script = VM.guest_bootstrap("/owned/fixture", manifest)
        self.assertIn('Package)" = bubblewrap', script)
        self.assertIn('Version)" = 0.9.0-1ubuntu0.3', script)
        self.assertIn('Architecture)" = arm64', script)
        self.assertIn("/usr/bin/dpkg-deb --extract", script)
        self.assertNotIn("dpkg -i", script)
        self.assertNotIn("sysctl", script)
        self.assertNotIn("postinst", script)
        self.assertIn("test ! -L /owned/fixture/bubblewrap-extract-0/usr/bin/bwrap", script)
        self.assertIn("test -f /owned/fixture/bubblewrap-extract-0/usr/bin/bwrap", script)
        self.assertIn("/usr/bin/install -o root -g root -m 0755", script)
        self.assertIn("/usr/bin/cmp -s", script)
        self.assertIn("0:0:755", script)

    def test_unknown_package_installation_mode_fails_before_creating_vm(self):
        package = self.root / "fixture.deb"
        package.write_bytes(b"!<arch>\n")
        self.manifest["packages"] = [{"path": str(package), "sha256": VM.digest(package),
            "version": "1.0", "architecture": "arm64", "installMode": "unknown"}]
        self.save_manifest()
        self.assertEqual(self.run.execute()["status"], "assets-invalid")
        self.assert_no_vm()

    def test_standard_profile_extraction_validates_pin_and_loads_only_target(self):
        manifest = {"packages": [{"installMode": "extract-bubblewrap-profile", "version": "4.0.1-1", "profileSha256": "a" * 64}]}
        script = VM.guest_bootstrap("/owned/fixture", manifest)
        self.assertIn('Package)" = apparmor-profiles', script)
        self.assertIn('Architecture)" = all', script)
        self.assertIn("/usr/bin/dpkg-deb --extract", script)
        self.assertIn('"${profile_digest%% *}" = ' + "a" * 64, script)
        self.assertIn("test ! -L /owned/fixture/bubblewrap-profile-extract-0/usr/share/apparmor/extra-profiles/bwrap-userns-restrict", script)
        self.assertIn("/usr/bin/install -o root -g root -m 0644", script)
        self.assertIn("sudo -n /usr/sbin/apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict", script)
        self.assertIn('[ "$aa_before" = "$aa_after" ]', script)
        self.assertIn('[ "$clone_before" = "$clone_after" ]', script)
        self.assertNotIn("dpkg -i", script)
        self.assertNotIn("sysctl -", script)
        self.assertNotIn("postinst", script)

    def test_profile_requires_separate_digest_before_vm_creation(self):
        package = self.root / "fixture.deb"
        package.write_bytes(b"!<arch>\n")
        self.manifest["packages"] = [{"path": str(package), "sha256": VM.digest(package),
            "version": "1.0", "architecture": "all", "installMode": "extract-bubblewrap-profile"}]
        self.save_manifest()
        self.assertEqual(self.run.execute()["status"], "assets-invalid")
        self.assert_no_vm()

    def test_failed_live_evidence_cannot_be_reported_as_passed(self):
        self.args.mode, self.args.allow_direct, self.args.allow_installation_id = "live", True, True
        self.runner.evidence["client-live.json"]["passed"] = False
        with mock.patch.object(VM, "load_guard", return_value=FakeGuard()):
            self.assertEqual(self.run.execute()["status"], "evidence-incomplete")

    def test_results_never_overwrite_prior_attempt(self):
        self.args.result.write_text("existing evidence")
        argv = ["--lima", str(self.args.lima), "--assets-manifest", str(self.manifest_path),
                "--source-root", str(self.source), "--result", str(self.args.result), "--mode", "preflight"]
        self.assertEqual(VM.main(argv), 2)
        self.assertEqual(self.args.result.read_text(), "existing evidence")


class RunnerInterruptionTests(unittest.TestCase):
    def test_interrupt_drains_closed_stdin_and_preserves_explicit_status(self):
        process = mock.Mock(pid=12345)
        first = True

        def communicate(**kwargs):
            nonlocal first
            if first:
                first = False
                raise KeyboardInterrupt()
            if process.stdin is not None:
                raise ValueError("flush of closed file")
            return b"", b""

        process.communicate.side_effect = communicate
        with mock.patch.object(VM.subprocess, "Popen", return_value=process), \
                mock.patch.object(VM.os, "killpg") as signal_group:
            with self.assertRaises(VM.AcceptanceError) as failure:
                VM.Runner()(["fixture-command"])
        self.assertEqual(failure.exception.code, "interrupted")
        self.assertIsNone(process.stdin)
        signal_group.assert_called_once_with(12345, VM.signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
