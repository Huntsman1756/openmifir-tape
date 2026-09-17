# G0-A3 capture operations runbook

The scheduled capture runner is `collectors/a3.py`. It implements the
scheduling contract of `docs/gates/G0.md` §2 G0-A.4/A.5 using the per-source
windows in `config/sources/*.yaml`:

- `--mode poll` — rescan objects with `publication_timestamp >= now - 24h`.
  Run every <= 1 hour. For BME this re-fetches the daily file, which mutates
  intraday; changed bytes create a new immutable `capture_version`, unchanged
  bytes yield `ALREADY_PRESENT` (zero state change).
- `--mode rolling` — request a rescan of the previous 7 days; additionally
  scans the source's `entrypoint.historical_page` when configured. Run daily.
  The requested 7-day window is not a guarantee that the provider exposes 7
  days of history.

Retries follow each source's `retry_policy` (bounded per run). Outages are
covered by the next poll plus whatever historical depth the source actually
exposes, and remain visible in the journal for A3 verification.

**Bloomberg source-bound recovery:** the live rolling smoke on 2026-09-16
successfully captured 1,328 `BAPA-POST2` objects with zero errors, but
`/historyfiles` exposed only about 29 hours of interval files at that time
(oldest `20260915-00:05:55.845-02`, newest
`20260916-05:05:55.850-01`). Therefore poll continuity is the primary capture
mechanism for `blb_apae`; a Bloomberg gap older than the source's available
historical depth is not automatically recoverable by `rolling` and must be
explicitly evaluated during A3 verification. Do not describe Bloomberg rolling
as guaranteed 7-day catch-up. BME recovery remains governed by the history
actually returned by its listing and the configured 7-day selection window.

Objects whose publication timestamp is unparseable or whose semantics are
unverified (BAPA-POST2 filename tokens) are always selected, with a 6h window
margin — the runner errs toward over-capture, never under-capture.

## Layout

| path                        | contents                                        |
|-----------------------------|-------------------------------------------------|
| `data/raw/`                 | immutable raw payloads (gitignored, write-once) |
| `data/a3/journal.jsonl`     | one JSON record per source run (catch-up log)   |
| `evidence/g0-a3/runs/`      | metadata-only per-run manifests (committable)   |

## VPS install (systemd)

```bash
git clone <repo> /opt/openmifir-tape && cd /opt/openmifir-tape
python3 -m venv .venv && .venv/bin/pip install .
cp ops/systemd/omt-a3-*.service ops/systemd/omt-a3-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now omt-a3-poll.timer omt-a3-rolling.timer
systemctl list-timers omt-a3-*
```

Poll runs every 30 min (:07/:37) — twice the `poll_max_seconds=3600` bound so
scheduler delay has substantial margin before the 1h contract limit. Rolling
runs daily at 06:15 UTC. Both timers are `Persistent=true` so a missed run
fires on boot. Concurrent runs serialize on `data/a3/.runner.lock` (15 min
bound); a run that still cannot acquire the lock journals `SKIPPED_LOCKED` and
exits non-zero — it never disappears silently.

## Alternative: cron

```cron
7,37 * * * * cd /opt/openmifir-tape && .venv/bin/python -m collectors.a3 --mode poll
15 6 * * *   cd /opt/openmifir-tape && .venv/bin/python -m collectors.a3 --mode rolling
```

## Windows (Task Scheduler)

```powershell
schtasks /create /tn "omt-a3-poll" /sc minute /mo 30 `
  /tr "\"<repo>\\.venv\\Scripts\\python.exe\" -m collectors.a3 --mode poll" /ru <user>
schtasks /create /tn "omt-a3-rolling" /sc daily /st 06:15 `
  /tr "\"<repo>\\.venv\\Scripts\\python.exe\" -m collectors.a3 --mode rolling" /ru <user>
```

## Exit status / monitoring

Exit 0 = every source SUCCEEDED; exit 1 = any PARTIAL/FAILED/NOT_RUN or a
`SKIPPED_LOCKED` timeout (wire to cron mail / systemd `OnFailure` if desired).
Inspect:
`journalctl -u omt-a3-poll.service`, `journalctl -u omt-a3-rolling.service`,
`tail data/a3/journal.jsonl`.

## End of window

After 5 trading days accrue, open the G0-A3 verification issue. Evidence =
`evidence/g0-a3/runs/` manifests + `data/a3/journal.jsonl` (no unexplained
gaps; outages must have catch-up evidence or be classified as unrecoverable
source-bound gaps). For Bloomberg specifically, verify poll continuity against
the effective historical depth observed in rolling manifests; do not assume a
7-day provider archive. Then E1 may run per `g0-issue-series.md` G0-E1.
