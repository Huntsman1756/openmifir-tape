# Self-hosted CI fallback — RETIRED

**Status: RETIRED (2026-10-17, decision D4).** Hosted CI
(`.github/workflows/ci.yml`) is now the only CI. This document is kept for
history and for the host-side removal steps.

## Why the fallback existed

While the repository was private and GitHub-hosted Actions were blocked by
account billing, every hosted check failed in 3–4 s. A repo-scoped self-hosted
runner fleet was added as a deliberate, manual-only contingency so commits could
still be verified. The hosted pipeline was always the primary CI.

## Decision (2026-10-17)

The repository was made public, so Actions is free again and the billing failure
no longer applies. GitHub advises against self-hosted runners on public
repositories, and the Linux runner ran on the capture VPS — the same host that
must poll data providers for G0-A.4. Retiring the fleet frees that host.

Actions taken:

- `ci-self-hosted.yml` moved from `.github/workflows/` to
  `docs/ops/archived/ci-self-hosted.yml`. GitHub only executes workflows under
  `.github/workflows/`, so the archived copy is inert.
- `omt-openmifir-linux-01` to be deregistered (see runbook below). The host-side
  service removal is an operator step and must not be skipped.
- `omt-openmifir-win-01` was never registered against this repository: as of
  2026-10-17 the runners API listed only the Linux runner.
- The hung `workflow_dispatch` runs 39042911533 and 39042693822 were cancelled;
  no run is left queued.
- No verification receipt is rewritten: existing evidence that cites the
  fallback remains a true record of what was executed at the time.

## Removal runbook (operador)

Run on the VPS host, as root. The order matters: stop and uninstall the service
**before** removing the GitHub registration.

```bash
# 1. Stop and uninstall the systemd service
systemctl stop 'actions.runner.Huntsman1756-openmifir-tape.omt-openmifir-linux-01.service'
cd /opt/actions-runner-openmifir
./svc.sh stop && ./svc.sh uninstall

# 2. Get a short-lived removal token (never store it, never commit it)
gh api -X POST repos/Huntsman1756/openmifir-tape/actions/runners/remove-token --jq .token

# 3. Deregister the runner from GitHub using that token
sudo -u gha-openmifir ./config.sh remove --token <REMOVE_TOKEN>

# 4. Optional: retire the dedicated account
userdel -r gha-openmifir
```

If the runner still appears in the API (for example because the host was
unreachable), delete the registration directly:

```bash
gh api repos/Huntsman1756/openmifir-tape/actions/runners \
  --jq '.runners[] | "\(.id) \(.name) \(.status)"'
gh api -X DELETE repos/Huntsman1756/openmifir-tape/actions/runners/<RUNNER_ID>
```

Verify the fleet is empty:

```bash
gh api repos/Huntsman1756/openmifir-tape/actions/runners --jq '.runners[].name'
# expected: no output
```

On the Windows host, if a detached `run.cmd` process is still running, stop the
process and remove `C:\actions-runner-openmifir`; there is no GitHub-side
registration to delete.

## Restoring the fallback (do not do this implicitly)

Restoring requires an explicit decision and, at minimum:

1. Move `docs/ops/archived/ci-self-hosted.yml` back to `.github/workflows/`.
2. Re-register a runner carrying the `omt-ci` and `omt-<os>-x64` labels.
3. Record the reason in this document and in the triggering issue/PR.

The original constraints still apply: never give runner accounts admin rights,
docker access or provider secrets; never point the workflow at labels outside
`omt-ci` / `omt-<os>-x64`; never add `push` / `pull_request` /
`pull_request_target` triggers to the fallback without an explicit decision.

---

## Appendix — original operating notes (pre-retirement, unchanged)

The content below described the fleet while it was live. It is preserved verbatim
for traceability; do not follow it as current instructions.

### Why manual-only

Repo-scoped self-hosted runners execute checked-out code with host-level
access. To keep that safe, `.github/workflows/ci-self-hosted.yml` is
**`workflow_dispatch` only** — it never runs on `push`, `pull_request`, or
`pull_request_target`. Someone with write access must deliberately request
a run and state the exact `target_sha`.

### Runners

Two dedicated, repo-scoped runners (registered against
`Huntsman1756/openmifir-tape` only):

| Runner | Host | Service | Custom labels |
|---|---|---|---|
| `omt-openmifir-linux-01` | Ubuntu 24.04 VPS, dedicated unprivileged user `gha-openmifir`, runner dir `/opt/actions-runner-openmifir` | `systemd` (`actions.runner.Huntsman1756-openmifir-tape.omt-openmifir-linux-01.service`) | `omt-ci`, `omt-linux-x64` |
| `omt-openmifir-win-01` | Windows host, unprivileged interactive account | detached process (service install needs an interactive elevated session) | `omt-ci`, `omt-windows-x64` |

All jobs also matched `self-hosted` + the platform's automatic OS/arch
labels. Jobs pinned `[self-hosted, omt-ci, omt-<os>-x64]` so no other runner
could pick them up.

### Service control (Linux)

```bash
systemctl status 'actions.runner.Huntsman1756-openmifir-tape.*'
# from the runner directory, as root:
cd /opt/actions-runner-openmifir
./svc.sh stop | start | status
```

The service ran as `gha-openmifir` — no sudoers entry, no docker socket,
no provider credentials, dedicated HOME.

### Windows service (pending elevation)

`omt-openmifir-win-01` ran `run.cmd` as a detached process under the
unprivileged interactive account. If the host rebooted, the runner had to be
restarted manually:

```powershell
Start-Process -WindowStyle Hidden -FilePath "C:\actions-runner-openmifir\run.cmd" `
  -WorkingDirectory "C:\actions-runner-openmifir"
```

### Running the fallback CI

```bash
gh workflow run ci-self-hosted.yml \
  --ref main \
  -f target_sha=<full-40-hex-sha> \
  -f reason=billing-fallback

gh run list --workflow ci-self-hosted.yml --limit 5
gh run watch <RUN_ID>
```

Every job validated the SHA format (`^[0-9a-f]{40}$`), checked out
`target_sha` with `persist-credentials: false`, and asserted
`git rev-parse HEAD == target_sha` before running anything. The
`run-name` recorded the tested SHA.

Tests used a run/version-specific `runner.temp` directory and print slow-case
durations. On the Windows operator host, the same three-publication fixture
took 7.773s in the default process temp versus 0.051s under runner temp;
mkdir/open dominated the slow profile. No test scope or assertion was removed.
Wheel jobs forced a non-editable reinstall before isolated audit-CLI smoke, so
an earlier editable install could not make packaging checks pass accidentally.
Legacy target commits without the audit smoke tool retained their original wheel
checks; targets containing the tool had to pass it.
