# Verification: Windows, macOS, and Linux portability

Status: all 14 automated checks passed on native Windows x64, Ubuntu x64, and macOS arm64 hosted runners. Local macOS verification adds system-alias and case-sensitive APFS coverage. Live-client acceptance remains pending.

## Scope and baseline

Version 0.10.1 extends the existing implementation with platform-aware filesystem handling, a portable test runner, Windows/Unix link fixtures, and cross-platform setup documentation. Receipt schema 2 and manager protocol 1 remain the compatibility baseline. The deployment manager stays self-contained for recovery after source removal or payload downgrade.

The development checkout began at `ca62fce` on branch `zhiheng`. Initial local verification covered the uncommitted implementation; hosted verification covers commit `1ef8f5d195e568568d0a8686e00cf7a4572e17df`. Existing Windows 0.10.0 results remain historical. No PowerShell or Linux runtime was initially available on the local macOS arm64 host. Tests were authored before executable verification became available; this record does not claim an earlier executed test-first Red.

## Ownership and review

- Deployment owner: top-level install, update, deploy, and validator scripts.
- Runtime owner: installed team-core runtime scripts and their local filesystem helper.
- Test owner: test fixtures, portability regressions, and the common test runner.
- Integration owner: shared behavior decisions, platform documentation, paired release files, review, and verification reconciliation.

Production and test edits had disjoint file ownership. Deployment and runtime owners independently reviewed each other's changes; integration review covered tests, the runner, CI, and documentation. Review corrections aligned macOS alias validation, retained conservative case-insensitive protection for governance/legacy namespaces, and made payload validation fail on incomplete directory inspection.

Execution found two further issues. PowerShell returned an empty parent for `Split-Path -Parent /var`, so relative system-alias targets now use `.NET GetDirectoryName`. Configured fallback instruction names again reject case-only duplicates on every OS, preserving the previous contract and preventing ambiguity on default APFS. Actual project-file identity maps remain ordinal.

## Acceptance cases

The initial local macOS results below do not establish native Windows or Linux behavior. The hosted-run section records the later native evidence separately.

| Case | Expected result | Evidence | Status |
| --- | --- | --- | --- |
| PS-01 | The runner launches the current PowerShell executable and reports child results correctly. | Full/follow-up runs, comma-separated suite selection, JSON summaries. | Passed on macOS |
| PS-02 | Install, update, preview, conflicts, downgrade, standalone restore, and crash recovery preserve existing behavior. | Deployment and installer suites in fake homes. | Passed on macOS |
| PS-03 | Verified macOS system aliases work; arbitrary and dangling links fail closed. | Portability, documentation, project-rules, feedback, and deployment fixtures. | Passed on macOS |
| PS-04 | Blueprint traversal into a case-different sibling is rejected without writing there. | Case-sensitive APFS portability run. | Passed on macOS |
| PS-05 | Case-distinct files retain separate inventory/hash identities; package collisions are rejected. | Case-sensitive documentation/OpenSpec and deployment regressions. | Passed on macOS |
| PS-06 | Separate processes respect the deployment lock and retain recoverable state after interruption. | Process fixtures and recovery assertions. | Passed on macOS |
| PS-07 | Space/Unicode paths work; permission denial and cleanup preserve unrelated files. | Both APFS modes; Unix permission probe was enforced. | Passed on macOS; Windows ACL behavior unverified |
| PS-08 | Validators, packaging, stages, blueprints, feedback, and offline OpenSpec tests pass. | All 14 checks reconciled across full and affected-check runs. | Passed on macOS |
| PS-09 | English/Chinese setup and release facts remain equivalent. | Both required PowerShell checks and full semantic review. | Passed; final documentation checks recorded in follow-up result |
| PS-10 | Fresh-client discovery and a small workflow load installed resources. | Explicit live/manual acceptance. | Manual pending |

## Runtime and execution

Execution environment: macOS 26.5.1, OS/process arm64, PowerShell 7.6.6, .NET 10.0.12, Apple Git 2.39.5, and Node.js 20.20.0. The normal temporary filesystem was case-insensitive APFS. A separate owned, case-sensitive APFS sparse image was mounted with ownership enabled for the additional run.

The user explicitly authorized a direct download of the test runtime. The `powershell-7.6.6-osx-arm64.tar.gz` archive matched SHA256 `6df833d094ebac1c1a74340d7b3437f4aaf5e03ce640484a1c4359f3ce8b3db1` published in the [official PowerShell 7.6.6 release](https://github.com/PowerShell/PowerShell/releases/tag/v7.6.6). It was extracted into an owned temporary directory. This permission was limited to runtime acquisition. Test processes disabled PowerShell telemetry/update checks and .NET CLI telemetry and used temporary XDG directories.

The commands below use `pwsh` as shorthand for that temporary executable. JSON artifacts contain the unchanged runner output rather than reconstructed passing results.

| Run | Command/scope | Outcome |
| --- | --- | --- |
| Initial availability | Common runner and required bilingual commands before acquiring PowerShell. | Shell exit 127; no PowerShell execution. |
| First executable run | `pwsh -NoProfile -File scripts/test.ps1` | Both validators passed; twelve suites failed during temporary-path setup, exposing the alias-parent bug. |
| Focused runtime checks | `-Suite deployment,documentation,feedback-runtime,portability` after the alias fix. | All four passed and cleaned up. |
| [Full integrated run](platform-support-macos-full.json) | `pwsh -NoProfile -File scripts/test.ps1 -ResultPath <result-file>` | Thirteen of fourteen passed. Project-rules caught the fallback-name duplicate regression; the artifact retains this failure. |
| [Case-sensitive run](platform-support-macos-case-sensitive.json) | Same runner with `-Suite deployment,documentation,portability` and `TMPDIR` on case-sensitive APFS. | All three passed; filesystem sensitivity was confirmed and no scenario was skipped. |
| [Affected-check rerun](platform-support-macos-followup.json) | `-Suite validate,validate-docs,bilingual-docs,project-rules,portability` after the fallback fix and final documentation updates. | All selected checks passed. |

During report assembly, package validation caught a result link before its artifact existed. The artifact was saved and the final package check was rerun after linking it.

The fallback fix changed configured-name duplicate validation in project-rules only. Unaffected results from the full and case-sensitive runs were retained; the affected project-rules suite and portability smoke were rerun. Default-volume case-distinct skips are covered by the separate case-sensitive run. Every suite reported fixture cleanup. The case-sensitive volume was subsequently unmounted and its image removed.

A separate offline public-command diagnostic confirmed POSIX OpenSpec discovery selects the fixed global package entry outside the project, does not execute the shell shim, rejects project-local/linked automatic entries, and leaves configuration bytes unchanged. It used the repository's fake CLI reporting 1.13.2, not a real OpenSpec installation. Its fixtures were removed.

PowerShell parsed all 28 source/test scripts without syntax errors. For the final scripts, SHA256 of the sorted, newline-terminated `relative-path:sha256(file-bytes)` manifest is `01d18e340cf9455d49f4be37d8b856a4163090d11ecf1a5b1b86aa0cb46a7aa3`; inputs are all `.ps1` files under `scripts`, `skills/team-core/scripts`, and `tests`.

Additional local checks: `git diff --check`; complete semantic review of both README/changelog language pairs; supplemental paired technical-content/link/history checks; and local YAML parsing of the three-target CI workflow. At this stage, hosted execution was still pending. The declared PowerShell 7.0 minimum, additional architectures, real OpenSpec lifecycle, and live-client acceptance remain unverified. Before CI preparation, no real-home kit installation, commit, or push had been performed.

## Follow-up: native runners and client discovery

On 2026-10-10 the user requested the remaining verification and confirmed that no Windows/Linux machines or CI runners were available. The local Mac has no installed container or VM runtime for those targets. A separate GitHub-hosted workflow was prepared, retaining the original self-hosted option. It supports manual dispatch and a push trigger restricted to `zhiheng` for initial branch verification. The user subsequently authorized committing/pushing this branch, running CI, and direct GitHub connectivity for this run, then approved a writable fork when the original repository was read-only. This exception does not authorize unrelated provider traffic.

Before publication, the three standard runner labels were checked against [GitHub's supported runners](https://docs.github.com/en/actions/reference/runners/github-hosted-runners); the corresponding official image inventories list PowerShell, Git, and Node.js. The workflow pins [checkout v4.3.1](https://github.com/actions/checkout/releases/tag/v4.3.1) to `34e114876b0b11c390a56381ad16ebd13914f8d5` and [upload-artifact v4.6.2](https://github.com/actions/upload-artifact/releases/tag/v4.6.2) to `ea165f8d65b6e75b540449e92b4886f43607fa02`. The runtime versions actually supplied to each job still require recorded execution evidence.

A fresh local client-discovery attempt used Codex CLI `0.162.0-alpha.17.2`. The kit preview and installation succeeded into a disposable project's `.codex` and `.agents` directories. A separate stdio app-server process was launched under a macOS sandbox denying all networking and filesystem writes outside the disposable directory. `HOME` and `CODEX_HOME` were left unchanged. The process exited with code 1 before answering `initialize`; stderr included a permission denial. Thus no Skill or agent discovery result was obtained, and no model workflow was attempted. Raw client configuration and stderr were not persisted. This bounded attempt does not count as live-client acceptance; a usable fresh-client environment remains necessary.

The disposable client installation and startup state were cleaned up. Local package/document validators and workflow YAML/embedded-PowerShell parsing passed after CI preparation. These local checks were followed by the native hosted execution below.

## Native hosted execution

[Run 38040447909](https://github.com/Andy-Wang-010/codex_multi_agent/actions/runs/38040447909) passed on 2026-10-10 after pushing `zhiheng` to the authorized fork. Every job recorded expected and observed source SHA `1ef8f5d195e568568d0a8686e00cf7a4572e17df`, a clean checkout, successful preflight, and the same 14 unique checks with exit code 0. The six JSON files below are unchanged downloads of the workflow artifacts. The original repository remains configured as `origin`; the writable fork is `fork`.

| Target | Runtime | Temporary filesystem | Job duration | Preserved evidence |
| --- | --- | --- | --- | --- |
| Windows Server 2022 x64, OS build 10.0.20348 | PowerShell 7.6.6; .NET 10.0.12 | Case-insensitive | 1m35s | [Results](platform-support-hosted/windows-results.json), [provenance](platform-support-hosted/windows-provenance.json) |
| Ubuntu 24.04.5 LTS x64 | PowerShell 7.6.6; .NET 10.0.12 | Case-sensitive | 1m43s | [Results](platform-support-hosted/linux-results.json), [provenance](platform-support-hosted/linux-provenance.json) |
| macOS 15.7.9 arm64 | PowerShell 7.6.5; .NET 10.0.11 | Case-insensitive | 2m02s | [Results](platform-support-hosted/macos-results.json), [provenance](platform-support-hosted/macos-provenance.json) |

Job logs confirmed suite cleanup and these applicability limits:

- Windows skipped case-distinct package/receipt and sibling/document fixtures on its case-insensitive volume. Unix permission-mode and macOS system-alias cases were inapplicable. Junction/recovery coverage passed; a Windows ACL-denial test is still absent.
- Linux exercised case-distinct paths, symbolic links, and Unix permission denial. Only macOS system-alias cases were inapplicable.
- Hosted macOS skipped case-distinct paths and system-alias fixtures because its temporary root did not use `/var` or `/tmp`. Unix permission denial was exercised. The earlier local APFS runs retain the separate case-sensitive and alias evidence for the same script fingerprint.

GitHub emitted a nonblocking warning that the pinned v4 actions target Node.js 20 and were executed using Node.js 24. Checkout, tests, artifact retention, and cleanup all succeeded. This warning concerns the action runtime; actual Node.js versions used by the test fixtures are recorded separately in each provenance file.

The follow-up changes preserve these results and update documentation only; the source/test script fingerprint above remains unchanged. Native success does not establish live-client discovery, model availability, real OpenSpec lifecycle, the PowerShell 7.0 minimum, other Linux distributions, macOS x64, Linux arm64, or Windows ACL-denial behavior. The self-hosted workflow was not dispatched.

## Human verification

1. Review the completed native-target records and run the common entrypoint for any additional runtime, architecture, or filesystem claim before publishing that claim.
2. Review fake-home installation/recovery evidence and any platform-specific cases not exercised.
3. For an explicitly authorized real installation, preview changes, install, and start a fresh client session.
4. Confirm agent/Skill discovery and run one small workflow in a disposable project, recording actual role identity and runtime evidence.
5. Rehearse the installed standalone recovery entrypoint and record the observed outcome.

Final manual status: manual pending.
