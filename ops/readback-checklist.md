# VPS health readback checklist

**Status:** prepared by the agent, executed by the operator. Read-only.

**Authority:** review plan F0.3 (phase 0, infrastructure). This document is not
a gate artifact and defines no semantics; it only schedules a read-only health
inspection of the capture host. The capture contract it inspects is
`ops/A3-runbook.md` (which implements `docs/gates/G0.md` §2 G0-A.4/A.5).

**Purpose:** confirm, from the host itself, that the October state matches what
the repository asserts — the deployed revision is the verified one, both A3
timers are active, the poll service has run recently without failures, the
journal shows no unexplained gap, and the data partition is not filling up.

**What this is not**

- No writes: no `git pull`, no deploy, no `systemctl start/stop/restart`, no
  package install, no config change, no retry of a failed run.
- Not a capture-window claim. A green readback does not verify or extend any
  gate window; it is operational hygiene only.
- Not a payload export. The commands below project metadata (revision, timer
  state, statuses, timestamps, counts, disk figures). Do not paste raw captures
  or provider files.

## Expected values

| Probe | Expected | Source of truth |
|---|---|---|
| `rev-parse HEAD` | `2cedea5525664c4dc0cae513f18d833908ce5688` | `evidence/g0-a3-h2/G0-A3-H2_validation.yaml` (`deployed_revision`) |
| `omt-a3-poll.timer` / `omt-a3-rolling.timer` | `active` (`UnitFileState=enabled`) | `ops/A3-runbook.md` (install step) |
| poll cadence | a run every 30 min, `:07`/`:37` | `ops/A3-runbook.md` (poll bound ≤ 1h) |
| journal run `status` | `SUCCEEDED` | `collectors/a3.py` exit-status contract |
| data partition usage | well below 90% blocks and 90% inodes | — |

## How to run

Run the whole block on the VPS as the user that owns `/opt/openmifir-tape`
(it reads systemd state, so it may need `sudo` for `journalctl`/`systemctl`).
Copy the command and paste it into one terminal session:

```bash
bash -s <<'OMT_READBACK'
set -u
cd /opt/openmifir-tape

echo "== revision (expected 2cedea5525664c4dc0cae513f18d833908ce5688 = 2cedea5) =="
git rev-parse HEAD
git status --porcelain --branch

echo "== timers =="
systemctl list-timers 'omt-a3-*' --all --no-pager

echo "== timer units =="
systemctl show -p Id -p ActiveState -p UnitFileState -p NextElapseUSecRealtime omt-a3-poll.timer omt-a3-rolling.timer

echo "== failed units =="
systemctl --failed --no-pager

echo "== poll service, last 48h =="
journalctl -u omt-a3-poll.service --since '-48h' --no-pager | tail -n 40

echo "== rolling service, last 7d =="
journalctl -u omt-a3-rolling.service --since '-7d' --no-pager | tail -n 20

echo "== journal, last entry per source (metadata only) =="
python3 - <<'PY'
import collections, json
rows = [json.loads(l) for l in open("data/a3/journal.jsonl") if l.strip()]
by_source = collections.OrderedDict()
for r in rows:
    by_source.setdefault(r.get("source_id") or "<unattributed>", []).append(r)
def show(value):
    return "-" if value is None else str(value)
for source, entries in by_source.items():
    print("%s: %d journal entries" % (source, len(entries)))
    for r in entries[-5:]:
        print("  observed_at=%s mode=%s status=%s created=%s already_present=%s errors=%s"
              % (show(r.get("observed_at")), show(r.get("mode")), show(r.get("status")),
                 show(r.get("created")), show(r.get("already_present")),
                 len(r.get("errors") or [])))
PY

echo "== runtime manifests =="
ls -1 data/a3/evidence/g0-a3/runs/ 2>/dev/null | wc -l

echo "== runner lock =="
ls -l data/a3/.runner.lock 2>/dev/null || echo "no lock file"

echo "== disk =="
df -h /opt/openmifir-tape/data
df -i /opt/openmifir-tape/data

echo "== venv python =="
.venv/bin/python -V

echo "== end readback =="
OMT_READBACK
```

## Before pasting the output back

- Replace any hostname or IP address with `<vps>` (`journalctl` lines can carry
  them). The repository is public; keep the receipt host-agnostic.
- Trim nothing else. If a command errored, keep the error text — a failed
  probe is itself a finding.

## What the agent does with it

The output is converted into `evidence/ops-readback-2026-10/readback.json`
(metadata only: revision, timer states, per-source last statuses, disk figures)
plus `SHA256SUMS.txt`, and committed. No provider payload enters the repository.

## Stop conditions (report to the owner before continuing to G2)

- `rev-parse HEAD` differs from the expected `2cedea5` (deployed revision drift).
- Either timer is not `active`, or `systemctl --failed` lists an `omt-a3-*` unit.
- The poll service has no successful run in the last 2 hours, or the journal's
  newest entry per source is older than the poll interval by a wide margin.
- Any journal run entry whose `status` is not `SUCCEEDED` (i.e. `PARTIAL`,
  `FAILED`, `NOT_RUN` or `SKIPPED_LOCKED`). Object-level `already_present` counts
  are normal and are not a finding.
- `df -h` shows the data partition at or above 90% used, or `df -i` at or above
  90% inodes.
