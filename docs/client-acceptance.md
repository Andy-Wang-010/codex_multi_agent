# Client acceptance

Version 0.10.2 adds opt-in acceptance tools for macOS arm64 and Ubuntu 24.04 arm64. They test installation, fresh client discovery, standalone recovery, and one bounded review workflow. They do not run as part of `scripts/test.ps1`. Actual runs, including compatibility failures and incomplete checks, belong in the [platform verification record](verification/active/platform-support.md).

## Evidence and pass criteria

The test installs the kit into disposable directories and uses a temporary client home with persisted sessions. It never copies personal authentication or configuration. One Lead reviews a five-line inventory rule against a correct baseline, using exactly one child explicitly selected as `team-reviewer` with `fork_turns: none`.

| Check | Required evidence |
| --- | --- |
| Lead configuration | Client reports `gpt-6-astra` with reasoning effort `high`. |
| Reviewer configuration | Actual spawn selector chooses `team-reviewer`; client reports that role, `gpt-6.1-sol`, and reasoning effort `high`. |
| Review result | Reviewer identifies the equality regression, reads all three scoped inputs through successful recognized commands, and completes the handoff; both final answers contain a valid concrete regression witness. |
| Independent oracle | 143 integer cases compare the baseline and candidate with the fixture contract. |
| Preservation | Protected fixture, installed payload, and staged client/helper bytes remain unchanged; only the designated feedback files may be added or changed. |
| Cleanup | Owned processes, temporary authentication, session state, and fixtures are removed; the VM launcher also deletes its owned VM. |

The reviewer and Lead each retain their normal findings and coverage prose and append exactly one plain line beginning `ACCEPTANCE_FINDING:` followed by a JSON object. Its fields are `path` (repository-relative string), `line` (integer), `stock` and `requested` (integers), and `expected` and `actual` (booleans). The request specifies this neutral schema without supplying a witness. The harness checks the reported location against `inventory.py` line `5` and evaluates the concrete input against the baseline contract and candidate rule; a valid witness demonstrates an accepted positive request that the candidate wrongly rejects. Agent prose or keyword matches alone cannot pass.

The reviewer must also have successful command events for reads of `README.md`, `baseline/inventory.py`, and `inventory.py`. Evidence uses host-parsed read actions and supported shell/Python read syntax with exit code `0`. An unrecognized read form remains unverified and can fail the workflow even if the final finding is correct. This command evidence is separate from model identity and from the independent 143-case oracle.

Child item events identify the actual child even when a child-start notification is missing. The harness requests child metadata while processing events and checks it again before shutdown. Missing or mismatched host metadata fails the strict identity check. Prompt text, source TOMLs, task labels, and agent self-reports do not prove the loaded role or model.

Results distinguish intended settings, client-reported configured settings, and any provider-reported model identity. Client metadata establishes what the client configured; it does not attest which model served every inference request. Provider identity is optional and stays unknown when the interface does not expose it. No model substitution or automatic extra inference run is allowed.

## Client and runtime preparation

Use Python 3.9+, PowerShell 7, Git, and a compatible native Codex client with its same-release `codex-code-mode-host` helper. The full offline developer suite also requires Node.js. Acquire dependencies through the user's approved route before running these tools; the acceptance harness does not download them.

The common client version is pinned to `0.162.0-alpha.17.2` in [the client asset lock](../config/client-acceptance-lock.json), matching the earlier macOS test. OpenAI's [published release metadata](https://github.com/openai/codex/releases/expanded_assets/rust-v0.162.0-alpha.17.2) lists both `codex-aarch64-apple-darwin.tar.gz` and `codex-aarch64-unknown-linux-musl.tar.gz`, including their SHA256 digests. The lock also records each platform's separate same-release `codex-code-mode-host` archive under `codeModeHost`, with its archive and executable digests. Archive digests come from the official release; helper executable digests were calculated from the verified archives. These pins do not certify a completed acceptance run.

Verify both the client and helper archives against their locked digests before extracting them. Pass the helper executable explicitly with `--code-mode-host`; supplying only the client binary is insufficient. The harness requires the expected reported client version, verifies the helper executable against the same-release platform pin, and stages both in a disposable client package layout. An existing bundled client's reported version alone does not prove that its bytes match the locked archive. Capability checks run before authentication and report a compatibility failure rather than switching models or claiming a skipped check passed. The current verification record owns download, compatibility, and execution outcomes.

Run local preflight first, replacing the generic executable paths with verified local paths and keeping the result outside the source checkout:

```sh
python3 scripts/test-client-acceptance.py \
  --client /path/to/codex \
  --code-mode-host /path/to/codex-code-mode-host \
  --pwsh /path/to/pwsh \
  --source-root . \
  --result ../client-preflight.json \
  --mode preflight \
  --expected-client-version 0.162.0-alpha.17.2
```

Preflight checks the installed client interface and the helper through two offline probes: a framed stdio request evaluates `text(6 * 7)` and must return `42`, and a separate unauthenticated app-server opens an ephemeral thread with `code_mode_prewarm=true` without starting a model turn. The second probe must observe an owned child process running the exact staged helper path. A separate unauthenticated `command/exec` reads a dedicated `probe.txt` and must return its fixed contents; this confirms local command execution, without claiming that command ran through the helper. These checks run with external networking denied and do not call `turn/start`. Preflight also installs and discovers all 13 Skills and exercises standalone restore after removing the alternate source. A missing, mismatched, or unusable helper is a blocker before authentication. These probes need no account login or inference. A preflight pass is not a live workflow pass.

Linux additionally executes a harmless command through the client's actual `workspace-write` sandbox before login. The default network isolation uses an unprivileged user/network namespace. On a disposable Linux guest that restricts user namespaces, `--linux-root-netns` explicitly opts into a root-created network namespace: fixed, root-owned system helpers run through noninteractive `sudo -n`, bring loopback up, then drop to the original nonzero UID/GID, clear supplementary groups and capabilities, and set `no-new-privs` before running any client or repository code. Only the cleaned child environment is restored after the drop. The Lima launcher passes this option only to its owned guest; the generic harness never falls back to sudo automatically. Evidence records `namespaceBackend` and the command-sandbox check. This option does not relax Ubuntu's AppArmor policy or bypass the client's nested workspace sandbox; missing `bubblewrap` or incompatible nested namespace policy remains a preflight blocker.

## Approved live run

Authentication and inference must use an explicitly approved network route. An earlier direct-access or installation-ID exception is not permission for a new run. Before a run, state the destinations, route, and purpose, and confirm that any installation-identifier disclosure is authorized. Do not paste credentials into command arguments, result files, logs, or this repository.

The harness accepts the name of an environment variable containing the approved proxy URL; it does not need the proxy value on the command line. After authorization, the live command is:

```sh
python3 scripts/test-client-acceptance.py \
  --client /path/to/codex \
  --code-mode-host /path/to/codex-code-mode-host \
  --pwsh /path/to/pwsh \
  --source-root . \
  --result ../client-live.json \
  --mode live \
  --expected-client-version 0.162.0-alpha.17.2 \
  --proxy-url-env APPROVED_PROXY_URL \
  --allow-installation-id
```

Only when the user explicitly approves direct access for this run may `--allow-direct` replace `--proxy-url-env APPROVED_PROXY_URL`. The route options are mutually exclusive. These flags record an existing approval; typing a flag does not create authorization. The tool must fail before provider traffic if a required route or disclosure approval is missing. It must not silently fall back from the proxy to direct access.

Live mode completes offline checks before fresh ChatGPT device authentication. Complete the displayed sign-in using the host browser; authentication state belongs only to the temporary client home. Existing personal authentication caches are not imported. Login and the live workflow each have a ten-minute limit. Failed authentication, unavailable models, missing identity evidence, protocol errors, and timeouts produce unsuccessful results without silent substitution or repeated inference.

Sanitized JSON retains the source identity, environment, client/runtime versions, evidence categories, check outcomes, and cleanup status. Keep raw authentication output, tokens, proxy credentials, and installation-ID values out of published evidence. A saved result must distinguish unknown evidence from a failed assertion and from a passed check.

The separate `runtimePreservation` result compares the staged client and helper with hashes captured before authentication. It runs after process shutdown and before temporary-state deletion; a missing, linked, unreadable, or changed executable makes the result unsuccessful.

## Disposable Ubuntu arm64 VM

The optional `scripts/test-linux-vm.py` launcher requires an Apple Silicon Mac and an installed Lima runtime. It uses Apple's native virtualization backend, Ubuntu 24.04 arm64, four CPUs, 4 GiB RAM, and a 20 GiB disk. It disables shared host directories, containerd, and unnecessary provisioning. The installed Lima runtime is retained after the run.

Supply a verified local asset manifest for the pinned Ubuntu image and guest runtimes. The launcher copies a fingerprinted source snapshot into the guest's own Linux filesystem; it must not mount the user's source or home directories. Record the source commit and content fingerprint, OS, architecture, filesystem behavior, and runtime versions separately from earlier hosted Ubuntu x64 results.

The [Linux acquisition lock](../config/linux-acceptance-assets.lock.json) records published URLs, SHA256 values, and provenance for Lima `2.2.1`, Ubuntu release `20260926`, PowerShell `7.6.6`, Node.js LTS `24.21.0`, the locked Codex client and its code-mode host helper, and the additional `bubblewrap` and `apparmor-profiles` packages. It is an acquisition record, not the local VM manifest: download through an approved route, verify bytes, inspect executable archive paths, and then supply local paths. Extract Lima's complete archive so its native guest agent and templates remain available.

The manifest uses `schemaVersion: 1` and an exact `limaVersion`. Its `image` object requires a local qcow2 `path`, `sha256`, an exact Ubuntu 24.04 build `version` such as `24.04-20260926`, and `architecture: "aarch64"`. Its `runtimes` object contains `client`, `code_mode_host`, `pwsh`, and `node`; each requires a local archive `path`, `sha256`, exact `version`, relative executable `entrypoint` inside the archive, and `architecture: "aarch64"`. Runtime archives may be `.tar.gz` or `.tar.xz`. Use the lock's Linux client version and archive digest for `runtimes.client`, and the same-version nested `codeModeHost` archive digest for `runtimes.code_mode_host`; both require local paths and actual archive entrypoints. The launcher computes the helper entrypoint's digest from the archive and checks it against the common lock's `binarySha256`, then passes the extracted helper path to the guest harness's `--code-mode-host` option. `packages` is a list of local `.deb` records with `path`, `sha256`, exact `version`, and `architecture` of `arm64` or `all`. Entries may set `installMode` to `install` (the default, using `dpkg -i`) or the package-specific `extract-bubblewrap` and `extract-bubblewrap-profile` modes described below. The profile mode also requires the locked `profileSha256`.

Resolve relative asset paths against the manifest directory. Supply the complete offline package dependency closure for Python 3, Git, PowerShell system libraries, `util-linux`, and the client sandbox helper `bubblewrap` with its standard AppArmor profile; the guest cannot fetch missing packages. Obtain digests from trusted release provenance and compare them with the downloaded bytes. Do not invent version pins or digests to satisfy the manifest. Record the selected official client version on both platforms before claiming matched-client coverage.

For the locked Ubuntu image, its [published package manifest](https://cloud-images.ubuntu.com/releases/noble/release-20260926/ubuntu-24.04-server-cloudimg-arm64.manifest) already includes Python 3, Git, `util-linux`, and the [Ubuntu 24.04 .NET dependencies](https://learn.microsoft.com/en-us/dotnet/core/install/linux-ubuntu-install#ubuntu-2404). The client sandbox additionally needs `bubblewrap`, which the image lacks. Include the locked ARM64 `bubblewrap_0.9.0-1ubuntu0.3_arm64.deb` in the local manifest's `packages` list with `installMode: "extract-bubblewrap"`; its required `libc6`, `libcap2`, and `libselinux1` libraries and recommended `procps` are already in the image. The extra package downloads matched their official Ubuntu index SHA256 values, and each index matched the SHA256 in its `InRelease`, all retrieved over HTTPS. The OpenPGP signature was not independently verified. Actual runtime startup and the sandbox preflight still determine compatibility.

The `extract-bubblewrap` mode verifies the package name, pinned version, and ARM64 architecture with `dpkg-deb`, extracts into unprivileged owned staging, and checks that the helper is a regular file with non-symlink ancestors. It installs only `/usr/bin/bwrap` as `root:root` with mode `0755`. This mode avoids the package's maintainer-script and sysctl setup: it does not execute those scripts or install those policy files. This guarantee applies to the helper extraction mode; other `.deb` entries using `install` retain ordinary package-installation behavior. Ubuntu 24.04 AppArmor restrictions can still prevent the nested client sandbox from running; see the [official sandbox prerequisites](https://learn.chatgpt.com/docs/sandboxing#prerequisites). Installing the helper alone does not establish AppArmor compatibility or a passing acceptance result.

The pinned image also lacks the standard `bwrap-userns-restrict` profile. Add `apparmor-profiles_4.0.1really4.0.1-0ubuntu0.24.04.8_all.deb` with `architecture: "all"`, `installMode: "extract-bubblewrap-profile"`, and the acquisition lock's `profileSha256`. This mode verifies the package name, exact version and architecture, extracts as the ordinary guest user, and rejects a symlink profile or symlink ancestors. It checks the extracted `usr/share/apparmor/extra-profiles/bwrap-userns-restrict` against its pinned digest and refuses an existing installation target.

Only `/etc/apparmor.d/bwrap-userns-restrict` is installed, as `root:root` with mode `0644`; the launcher compares its bytes and ownership before loading that exact file with `apparmor_parser -r`. It records the profile digest and before/after values of `kernel.apparmor_restrict_unprivileged_userns` and `kernel.unprivileged_userns_clone`, requiring both values to remain unchanged. It does not execute package maintainer scripts, install unrelated profiles, or change global sysctl policy. This is the scoped AppArmor prerequisite described in the official guidance; a successful native sandbox preflight is still required before authentication, and actual live acceptance remains recorded separately.

With a completed manifest, run the VM's offline phase:

```sh
python3 scripts/test-linux-vm.py \
  --lima /path/to/limactl \
  --assets-manifest /path/to/acceptance-assets.json \
  --source-root . \
  --result ../linux-preflight.json \
  --mode preflight
```

For an authorized live run, use a separate result path, select `--mode live`, and add `--proxy-url-env APPROVED_PROXY_URL --allow-installation-id`. A newly approved direct-route exception may instead use `--allow-direct --allow-installation-id`. The same authorization and credential-handling rules as the local harness apply. The launcher uses supplied assets; dependency acquisition and Lima installation are separate preparation steps, not implicit network fallbacks.

Before any login, the guest must pass the existing 14-check suite, actual helper protocol smoke test, client discovery, and standalone recovery. Live mode uses a fresh guest client home and the same role/model/workflow requirements as macOS. Export sanitized evidence before deleting the uniquely named VM and its temporary credentials. A VM failure or cleanup failure remains visible in the result; no other Lima instance is eligible for deletion.

The launcher does not establish support for other Linux distributions, Linux x64 live workflows, or untested client versions. Windows remains the accepted baseline for this task. Native Ubuntu arm64 acceptance remains incomplete until the verification record contains an actual successful run.
