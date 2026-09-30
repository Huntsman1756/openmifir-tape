# Self-hosted CI fallback — operations

Contingency path for validating commits while GitHub-hosted Actions
entitlement/quota is unavailable. The hosted pipeline
`.github/workflows/ci.yml` remains the primary CI and is unchanged.

## Why manual-only

Repo-scoped self-hosted runners execute checked-out code with host-level
access. To keep that safe, `.github/workflows/ci-self-hosted.yml` is
**`workflow_dispatch` only** — it never runs on `push`, `pull_request`, or
`pull_request_target`. Someone with write access must deliberately request
a run and state the exact `target_sha`.

## Runners

Two dedicated, repo-scoped runners (registered against
`Huntsman1756/openmifir-tape` only):

| Runner | Host | Service | Custom labels |
|---|---|---|---|
| `omt-openmifir-linux-01` | Ubuntu 24.04 VPS, dedicated unprivileged user `gha-openmifir`, runner dir `/opt/actions-runner-openmifir` | `systemd` (`actions.runner.Huntsman1756-openmifir-tape.omt-openmifir-linux-01.service`) | `omt-ci`, `omt-linux-x64` |
| `omt-openmifir-win-01` | Windows host, unprivileged interactive account | detached process (service install needs an interactive elevated session — see "Windows service" below) | `omt-ci`, `omt-windows-x64` |

All jobs also match `self-hosted` + the platform's automatic OS/arch
labels. Jobs in this workflow pin `[self-hosted, omt-ci, omt-<os>-x64]`
so no other runner can pick them up.

Check status:

```bash
gh api repos/Huntsman1756/openmifir-tape/actions/runners \
  --jq '.runners[] | {name, os, status, busy, labels: [.labels[].name]}'
```

Expected: both `status: online`, `busy: false`.

## Service control (Linux)

```bash
systemctl status 'actions.runner.Huntsman1756-openmifir-tape.*'
# from the runner directory, as root:
cd /opt/actions-runner-openmifir
./svc.sh stop | start | status
```

The service runs as `gha-openmifir` — no sudoers entry, no docker socket,
no provider credentials, dedicated HOME.

## Windows service (pending elevation)

`omt-openmifir-win-01` currently runs `run.cmd` as a detached process
under the unprivileged interactive account (a Windows service install via
`config.cmd --runasservice` requires an interactive elevated session and
a service-account password — deliberately not automated). If the host
reboots, restart the runner manually:

```powershell
Start-Process -WindowStyle Hidden -FilePath "C:\actions-runner-openmifir\run.cmd" `
  -WorkingDirectory "C:\actions-runner-openmifir"
```

## Running the fallback CI

```bash
gh workflow run ci-self-hosted.yml \
  --ref main \
  -f target_sha=<full-40-hex-sha> \
  -f reason=billing-fallback

gh run list --workflow ci-self-hosted.yml --limit 5
gh run watch <RUN_ID>
```

Every job validates the SHA format (`^[0-9a-f]{40}$`), checks out
`target_sha` with `persist-credentials: false`, and asserts
`git rev-parse HEAD == target_sha` before running anything. The
`run-name` records the tested SHA.

Tests use a run/version-specific `runner.temp` directory and print slow-case
durations. On the Windows operator host, the same three-publication fixture
took 7.773s in the default process temp versus 0.051s under runner temp; mkdir/open dominated
the slow profile. No test scope or assertion was removed. Wheel jobs force
a non-editable reinstall before isolated audit-CLI smoke, so an earlier
editable install cannot make packaging checks pass accidentally. Legacy
target commits without the audit smoke tool retain their original wheel
checks; targets containing the tool must pass it.

## Removing a runner

```bash
# Linux (as root, on the host)
cd /opt/actions-runner-openmifir
./svc.sh stop && ./svc.sh uninstall
sudo -u gha-openmifir ./config.sh remove --token <new ephemeral removal token>
userdel -r gha-openmifir

# Windows: stop the process, then
cd C:\actions-runner-openmifir
.\config.cmd remove --token <new ephemeral removal token>
```

Removal/registration tokens come from
`POST /repos/Huntsman1756/openmifir-tape/actions/runners/{registration,remove}-token`
— ephemeral, never stored, never committed.

## Do NOT

- add `push` / `pull_request` / `pull_request_target` triggers to the
  fallback without an explicit decision;
- give runner accounts admin rights, docker access, or provider secrets;
- store tokens or passwords in repo files, scripts, or logs;
- point the workflow at runner labels outside `omt-ci`/`omt-<os>-x64`;
- run it as a substitute for hosted CI once quota is restored — hosted CI
  stays primary; this fleet is for outages and optional independent
  verification.
