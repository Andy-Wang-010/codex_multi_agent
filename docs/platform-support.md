# Platform support and verification

The kit uses one PowerShell implementation for Windows, macOS, and Linux. Agent profiles, Skills, installation receipts, and recovery commands share the same layout and interfaces. Version 0.10.1 adds portability changes and a common test entrypoint; it remains a review preview.

## Runtime and installation

Install PowerShell 7 separately and make `pwsh` available to the shell that runs the kit. The scripts do not install runtimes or download packages. Runtime acquisition, Git traffic, and any later live provider test must follow the user's approved network route. The test suite itself uses local fixtures.

The same commands work from PowerShell, macOS zsh, or Linux shells once `pwsh` is available:

```powershell
pwsh -NoProfile -File ./scripts/validate.ps1
pwsh -NoProfile -File ./scripts/install-user.ps1 -WhatIf
pwsh -NoProfile -File ./scripts/install-user.ps1
pwsh -NoProfile -File ./scripts/update-user.ps1
```

Installation defaults to the user's `.codex/agents` and `.agents/skills` directories. `-CodexHome` and `-AgentsHome` select alternative non-overlapping roots. Global Codex configuration is configured separately. Directory installation can operate without Git; deploying a local `-GitRef` requires Git. Verification and snapshot recovery remain independent of Git and of the source checkout. See [safe deployment](safe-deployment.md) for receipts, backups, restore, recovery, and pruning.

The full developer suite additionally requires Git for local deployment fixtures and Node.js for the optional OpenSpec adapter's offline CLI fixtures. A real OpenSpec installation is not needed for the default suite. The optional real adapter retains its pinned version and Node.js requirement in the [integration contract](../skills/team-core/references/openspec-integration.md).

## Filesystem contract

- Stored package keys and project-relative identities use forward slashes. Paths with spaces and non-ASCII characters are handled as literal paths and argument values.
- On macOS, selected roots and system-temporary anchors may use the verified operating-system aliases `/tmp` to `/private/tmp` and `/var` to `/private/var`. The runtime canonicalizes only these known aliases after checking their actual targets. Other linked roots, linked descendants, and dangling links are rejected.
- Project-relative paths cannot escape their selected root, including through traversal or case-different sibling directories. Non-Windows confinement uses conservative case-sensitive comparison.
- Target-project inventories preserve case-distinct filenames on case-sensitive filesystems. Distributable package paths must remain portable: case-colliding package entries are rejected rather than silently merged.
- Protected documentation metadata and historical-document namespaces retain conservative case-insensitive checks, including on case-insensitive macOS volumes; ordinary document identities remain exact.
- Installation ownership, snapshots, and freshness evidence continue to use exact file bytes. Checkout newline changes can invalidate earlier evidence and require reassessment. Never refresh old fingerprints merely to present prior verification as current.
- Recovery keeps a self-contained manager outside agent/Skill discovery. Its installed copy must not depend on the current checkout or a helper that payload downgrade could remove.

## Automated verification

Run the common entrypoint from the repository:

```powershell
pwsh -NoProfile -File ./scripts/test.ps1
pwsh -NoProfile -File ./scripts/test.ps1 -Suite portability,deployment,documentation
pwsh -NoProfile -File ./scripts/test.ps1 -ResultPath ./platform-results.json
```

Each selected check runs in its own PowerShell process. The runner reports the operating system, process architecture, runtime versions, check outcomes, and a failing exit status when a required check cannot pass. Individual suites own unique temporary fixtures and must clean up those fixtures. Checks never install into real user homes or acquire dependencies. Missing prerequisites are failures, not successful skips.

The default run selects 14 checks: two validators and twelve suites. `-Suite` also accepts a comma-separated string when invoked from zsh or Bash. `-ResultPath` optionally saves a JSON result summary to a chosen file whose parent already exists; keep that generated result outside a clean CI checkout. Platform-specific cases that cannot apply to the current fixture report `NOT APPLICABLE`; an overall suite pass does not establish those cases were exercised. Record case-sensitive-volume and filesystem-permission coverage separately.

Required coverage includes installation/update, unchanged preview, conflicts and backups, exact downgrade, offline restore, interrupted recovery, separate-process locking, unrelated-file preservation, path containment, symlink/dangling-link refusal, case-sensitive inventory, Unicode paths, metadata validation, and bilingual documentation. Optional real OpenSpec lifecycle results are recorded separately from local CLI-double results.

## Platform matrix and evidence

These targets define the initial verification matrix. Source support and recorded execution are separate claims. Record the actual OS release, architecture, PowerShell/.NET version, filesystem, source revision, and result for every run.

| Target | Filesystem coverage | Recorded status for this change |
| --- | --- | --- |
| Windows Server 2022 x64 | Case-insensitive temporary filesystem, junction and recovery regressions | All 14 checks passed on the hosted runner with PowerShell 7.6.6; Windows ACL-denial behavior remains unverified. |
| macOS arm64 | Hosted case-insensitive temporary filesystem; local default/case-sensitive APFS and verified system aliases | All 14 checks passed on macOS 15.7.9 with PowerShell 7.6.5. Local macOS 26.5.1 with PowerShell 7.6.6 supplies additional alias and case-sensitive evidence. |
| Ubuntu 24.04.5 LTS x64 | Case-sensitive temporary filesystem, symbolic links and Unix permission denial | All 14 checks passed on the hosted runner with PowerShell 7.6.6. |
| macOS x64 / Linux arm64 | Native architecture-specific run | Unverified until separately exercised. |

The historical [Windows verification](verification/active/project-rules.md) is retained as historical evidence. The [current platform verification record](verification/active/platform-support.md) owns results for this change. The declared PowerShell 7 minimum and any additional tested runtime versions require explicit execution evidence; a version declaration alone is not a compatibility result.

## CI and live workflow acceptance

The manual platform CI workflow uses preprovisioned self-hosted Windows, macOS, and Linux runners. Each runner needs PowerShell, Git, Node.js, and a clean checkout of the exact requested commit at `KIT_SOURCE_ROOT`. Provisioning and runner connectivity must use the approved route. The workflow verifies the checkout and runs local tests; it contains no checkout action, package installation, or network fallback. It does not provision runners automatically.

A separate [hosted workflow](../.github/workflows/platform-tests-hosted.yml) serves environments without those runners. It uses GitHub-hosted Windows, Ubuntu, and macOS jobs, exact-commit checkout, and retained JSON results with runtime/source provenance. It supports manual dispatch and an initial push trigger restricted to `zhiheng`, allowing branch verification without changing the default branch. Publishing that branch starts the jobs. Its first [three-platform run](https://github.com/Andy-Wang-010/codex_multi_agent/actions/runs/38040447909) passed all 14 checks on each target. GitHub connectivity, checkout, and artifact transport require an approved route. The current verification record retains actual runtime versions and filesystem cases not exercised, rather than inferring them from matrix labels.

After automated checks pass on a target, rehearse installation and restore in disposable homes. Separately verify agent and Skill discovery in a fresh client session and a small explicitly requested workflow. A byte-equal fake-home installation does not establish client loading, model availability, or human acceptance. Keep those results and any unavailable checks in the current verification record before expanding published support claims.
