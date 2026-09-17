"""G0-A3 operational capture runner: scheduled poll + rescan windows.

Implements the scheduling contract of docs/gates/G0.md §2 G0-A.4/A.5:

- ``poll`` mode (invoked every <= 1h by an external scheduler): rescan of the
  previous ``retrieval_interval.rescan.daily_window_hours`` (24h) keyed on
  ``publication_timestamp``. For BME this re-fetches the daily file, which is
  known to mutate intraday: unchanged bytes -> ALREADY_PRESENT, mutated bytes
  -> a new immutable capture_version under the same source_object_id.
- ``rolling`` mode (invoked daily): rescan of the previous
  ``retrieval_interval.rescan.rolling_days`` (7 days), additionally consulting
  the source's ``entrypoint.historical_page`` when one is configured.

The runner never schedules itself: an external trigger (systemd timer / cron /
Task Scheduler) invokes each mode. Retry policy comes from the per-source
``retry_policy`` in config/sources/*.yaml and is bounded per run; outages are
covered by the next poll plus the daily rolling rescan (the catch-up mechanism
for gaps up to 7 days) and are visible as gaps in the journal.

CAPTURE_WINDOW_COMPLETE (G0-A.8) is an operational verification performed later
from the accrued per-run manifests and journal; this module only produces
faithful run records. It performs no normalization and no coverage inference.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .config import load_all_sources
from .harness import SourceRunResult
from .sources import get_adapter
from .sources.base import DiscoveredObject, HttpGetter
from .storage import RawStore

# Publication timestamps whose semantics are not fully verified (e.g. the
# BAPA-POST2 filename token, whose trailing offset convention is unconfirmed per
# config) are bracketed with this margin when applying a rescan window. The
# runner errs toward capturing MORE objects, never fewer: a re-fetch of an
# unchanged object is idempotent (ALREADY_PRESENT), while an under-selected
# window would be a silent capture gap.
UNVERIFIED_TS_MARGIN = timedelta(hours=6)

_BAPA_TOKEN_RE = re.compile(
    r"^(\d{4})(\d{2})(\d{2})-(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(?:[+-]\d{2})?$"
)


def parse_publication_timestamp(ts: str | None) -> tuple[datetime | None, bool]:
    """Parse a source publication timestamp for windowing.

    Returns (instant, verified). ``verified`` is False when the timestamp was
    derived from an unverified source token (window selection then applies
    UNVERIFIED_TS_MARGIN). Returns (None, False) when unparseable — such
    objects are always included in a rescan window (fail toward capture).
    """
    text = (ts or "").strip()
    if not text:
        return None, False
    # The BAPA-POST2 filename token (e.g. "20260914-20:22:55.841-02") happens to
    # be valid ISO-8601 basic format, but config/sources/bloomberg_apae.yaml
    # marks its exact semantics unconfirmed, so it must NOT be trusted as a
    # verified instant: parse the wall-clock portion and flag it unverified.
    m = _BAPA_TOKEN_RE.match(text)
    if m:
        year, mon, day, hh, mm, ss, frac = m.groups()
        micros = int((frac or "0").ljust(6, "0")[:6])
        dt = datetime(int(year), int(mon), int(day), int(hh), int(mm), int(ss),
                      micros, tzinfo=UTC)
        return dt, False
    try:
        # Python 3.11+ parses the 'Z' suffix natively.
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC), True
    except ValueError:
        return None, False


def select_window(
    discovered: Iterable[DiscoveredObject],
    cutoff: datetime,
    *,
    margin: timedelta = UNVERIFIED_TS_MARGIN,
) -> tuple[list[DiscoveredObject], int, int]:
    """Select discovered objects inside the rescan window.

    Returns (selected, skipped_outside_window, unverified_ts_count). Objects
    with unparseable timestamps are always selected; objects with unverified
    token timestamps use ``cutoff - margin``.
    """
    selected: list[DiscoveredObject] = []
    skipped = 0
    unverified = 0
    for obj in discovered:
        pub, verified = parse_publication_timestamp(obj.publication_timestamp)
        if pub is None:
            selected.append(obj)
            unverified += 1
        elif not verified:
            unverified += 1
            if pub >= cutoff - margin:
                selected.append(obj)
            else:
                skipped += 1
        elif pub >= cutoff:
            selected.append(obj)
        else:
            skipped += 1
    return selected, skipped, unverified


def _window_for(mode: str, conf: dict[str, Any]) -> timedelta:
    rescan = (conf.get("retrieval_interval") or {}).get("rescan") or {}
    if mode == "poll":
        hours = rescan.get("daily_window_hours")
        if hours is None:
            raise ValueError(f"{conf.get('source_id')}: missing retrieval_interval.rescan.daily_window_hours")
        return timedelta(hours=float(hours))
    days = rescan.get("rolling_days")
    if days is None:
        raise ValueError(f"{conf.get('source_id')}: missing retrieval_interval.rescan.rolling_days")
    return timedelta(days=float(days))


def _discover(
    source_id: str,
    conf: dict[str, Any],
    http_get: HttpGetter,
    *,
    include_historical: bool,
    result: SourceRunResult,
) -> list[DiscoveredObject]:
    """Discover objects; in rolling mode also scan entrypoint.historical_page.

    Returns the merged, de-duplicated object list. Discovery errors are recorded
    on ``result.errors``/``result.log`` and never silently swallowed.
    """
    adapter = get_adapter(source_id)
    discovered: list[DiscoveredObject] = []
    try:
        discovered = adapter.discover(conf, http_get)
    except Exception as exc:  # noqa: BLE001 - record failure, fail closed
        result.errors.append(f"discover: {exc}")
        result.log.append(f"{source_id}: discover failed: {exc}")

    entry = conf.get("entrypoint") or {}
    historical_page = entry.get("historical_page") or ""
    if include_historical and historical_page:
        aux_conf = {**conf, "entrypoint": {**entry, "public_data_page": historical_page}}
        try:
            aux = adapter.discover(aux_conf, http_get)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"discover historical_page: {exc}")
            result.log.append(f"{source_id}: historical_page discover failed: {exc}")
        else:
            seen = {o.object_key for o in discovered}
            extra = [o for o in aux if o.object_key not in seen]
            if extra:
                result.log.append(
                    f"{source_id}: historical_page contributed {len(extra)} additional objects"
                )
                discovered.extend(extra)

    discovered.sort(key=lambda o: o.publication_timestamp)
    return discovered


def _fetch_with_retry(
    adapter: Any,
    conf: dict[str, Any],
    http_get: HttpGetter,
    obj: DiscoveredObject,
    *,
    max_attempts: int,
    backoff_seconds: list[float],
    sleep: Callable[[float], None],
    result: SourceRunResult,
) -> bytes | None:
    for attempt in range(1, max_attempts + 1):
        try:
            return adapter.fetch(conf, http_get, obj)
        except Exception as exc:  # noqa: BLE001 - recorded, retried per policy
            result.log.append(
                f"{obj.object_key}: fetch attempt {attempt}/{max_attempts} failed: {exc}"
            )
            if attempt < max_attempts:
                delay = backoff_seconds[min(attempt - 1, len(backoff_seconds) - 1)] if backoff_seconds else 0
                if delay:
                    sleep(float(delay))
    result.errors.append(f"fetch {obj.object_key}: exhausted {max_attempts} attempt(s)")
    result.log.append(f"{result.source_id}: fetch failed for {obj.object_key}")
    return None


def run_scheduled(
    source_id: str,
    conf: dict[str, Any],
    store: RawStore,
    http_get: HttpGetter,
    *,
    mode: str,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SourceRunResult:
    """Run one scheduled capture for one source under the given mode window."""
    now = now or datetime.now(UTC)
    observed_at = now.isoformat()
    adapter = get_adapter(source_id)
    result = SourceRunResult(source_id=source_id, attempted=True,
                             status="SUCCEEDED", observed_at=observed_at)

    window = _window_for(mode, conf)
    cutoff = now - window
    retry = conf.get("retry_policy") or {}
    max_attempts = int(retry.get("max_attempts") or 1)
    backoff = [float(b) for b in (retry.get("backoff_seconds") or [])]
    include_historical = mode == "rolling"

    result.context = {
        "mode": mode,
        "window_hours": round(window.total_seconds() / 3600.0, 3),
        "window_cutoff_utc": cutoff.isoformat(),
        "unverified_ts_margin_hours": round(UNVERIFIED_TS_MARGIN.total_seconds() / 3600.0, 3),
        "retry_policy": {"max_attempts": max_attempts, "backoff_seconds": backoff},
        "historical_page_scanned": bool(include_historical and (conf.get("entrypoint") or {}).get("historical_page")),
    }
    result.log.append(
        f"{source_id}: mode={mode} window_hours={result.context['window_hours']} "
        f"cutoff={cutoff.isoformat()}"
    )

    discovered = _discover(source_id, conf, http_get,
                           include_historical=include_historical, result=result)
    if not discovered:
        result.status = "FAILED" if result.errors else "NOT_RUN"
        result.log.append(f"{source_id}: no objects discovered ({result.status})")
        return result

    selected, skipped, unverified = select_window(discovered, cutoff)
    sel_pubs = sorted(o.publication_timestamp for o in selected if o.publication_timestamp)
    result.context.update({
        "discovered_count": len(discovered),
        "selected_count": len(selected),
        "skipped_outside_window": skipped,
        "unverified_ts_selected": unverified,
        "oldest_selected_publication_timestamp": sel_pubs[0] if sel_pubs else None,
        "newest_selected_publication_timestamp": sel_pubs[-1] if sel_pubs else None,
    })
    result.log.append(
        f"{source_id}: discovered={len(discovered)} selected_in_window={len(selected)} "
        f"skipped_outside_window={skipped} unverified_ts_selected={unverified}"
    )
    if not selected:
        result.log.append(f"{source_id}: 0 objects within window (e.g. non-trading period)")

    object_failures = 0
    for obj in selected:
        data = _fetch_with_retry(
            adapter, conf, http_get, obj,
            max_attempts=max_attempts, backoff_seconds=backoff,
            sleep=sleep, result=result,
        )
        if data is None:
            object_failures += 1
            continue
        pub = obj.publication_timestamp or obj.object_key
        extra: dict[str, Any] = {}
        if not obj.publication_timestamp:
            extra["publication_timestamp_derivation"] = "source_token_unverified"
        try:
            record = store.store(
                source_id=source_id,
                collection_date=observed_at[:10],
                object_key=obj.object_key,
                data=data,
                publication_timestamp=pub,
                provenance_url=obj.url,
                observation_utc=observed_at,
                extra=extra or None,
            )
        except Exception as exc:  # noqa: BLE001
            object_failures += 1
            result.errors.append(f"store {obj.object_key}: {exc}")
            result.log.append(f"{source_id}: store failed for {obj.object_key}")
            continue
        result.objects.append(record)
        result.log.append(
            f"{source_id}: {record.status} {obj.object_key} sha256={record.raw_sha256}"
        )

    if object_failures and not result.objects:
        result.status = "FAILED"
    elif result.errors:
        result.status = "PARTIAL"
    return result


def write_run_evidence(
    evidence_dir: Path,
    result: SourceRunResult,
    run_id: str,
) -> Path:
    """Persist a metadata-only run manifest under evidence/g0-a3/runs/."""
    runs_dir = evidence_dir / "g0-a3" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "gate": "G0-A3",
        "purpose": "scheduled capture run (poll/rolling rescan)",
        "results": result.to_evidence(),
    }
    path = runs_dir / f"{run_id}__{result.source_id}__manifest.yaml"
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(manifest, fh, sort_keys=False)
    return path


def append_journal(journal_path: Path, result: SourceRunResult, run_id: str) -> None:
    """Append one JSONL record per source run; the journal is the catch-up /
    outage log the A3 verification reads to explain or expose gaps."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    ctx = result.context or {}
    record = {
        "run_id": run_id,
        "observed_at": result.observed_at,
        "source_id": result.source_id,
        "mode": ctx.get("mode"),
        "window_hours": ctx.get("window_hours"),
        "status": result.status,
        "discovered": ctx.get("discovered_count"),
        "selected": ctx.get("selected_count"),
        "created": sum(1 for o in result.objects if o.status == "CREATED"),
        "already_present": sum(1 for o in result.objects if o.status == "ALREADY_PRESENT"),
        "errors": result.errors,
    }
    with open(journal_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _run_id(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


LOCK_WAIT_TIMEOUT_S = 900.0  # serialization bound; beyond this -> SKIPPED_LOCKED

try:
    import fcntl  # POSIX advisory locking
except ImportError:
    fcntl = None
try:
    import msvcrt  # Windows byte-range locking
except ImportError:
    msvcrt = None


def record_skipped_locked(journal_path: Path, run_id: str, mode: str,
                          waited_s: float) -> None:
    """Journal a run that could not start because another run holds the lock.

    Lock contention is itself an operational event: it MUST be visible to the
    A3 verification rather than leaving an unexplained gap in the journal.
    """
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": run_id,
        "observed_at": datetime.now(UTC).isoformat(),
        "mode": mode,
        "status": "SKIPPED_LOCKED",
        "reason": "concurrent_a3_run",
        "lock_wait_seconds": round(waited_s, 3),
    }
    with open(journal_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _try_lock(fd) -> bool:
    """One non-blocking lock attempt on the platform's advisory mechanism.

    Every contender opens the SAME lock file in "a+b" and locks the SAME
    byte: range [0, 1). msvcrt byte-range locking requires the byte to exist,
    so an empty file is padded with one NUL byte first (harmless under fcntl,
    which locks the whole file)."""
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    if msvcrt is not None:
        fd.seek(0)
        if fd.read(1) == b"":
            fd.write(b"\0")
            fd.flush()
        fd.seek(0)
        msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
        return True
    return True  # no locking primitive available: degrade to no-op


def _unlock(fd) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif msvcrt is not None:
            fd.seek(0)  # unlock the SAME range [0, 1) that _try_lock took
            msvcrt.locking(fd.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


@contextlib.contextmanager
def _run_lock(lock_path: Path, timeout_s: float = LOCK_WAIT_TIMEOUT_S,
              sleep: Callable[[float], None] = time.sleep):
    """Serializing advisory lock: a concurrent run WAITS (bounded) instead of
    being silently dropped — a recovery rolling rescan must never be lost.
    Yields (acquired, waited_seconds). Uses fcntl on POSIX and msvcrt byte-range
    locking on Windows; on platforms with neither it degrades to a no-op."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    waited = 0.0
    start = time.monotonic()
    deadline = start + timeout_s
    with open(lock_path, "a+b") as fd:
        try:
            while True:
                try:
                    acquired = _try_lock(fd)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    sleep(1.0)
                    waited = time.monotonic() - start
            yield acquired, waited
        finally:
            if acquired:
                _unlock(fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="omt-g0a3", description="G0-A3 scheduled capture runner (poll/rolling)")
    parser.add_argument("--mode", required=True, choices=["poll", "rolling"])
    parser.add_argument("--source", default="all", choices=["all", "bme_apa", "blb_apae"])
    parser.add_argument("--config-dir", type=Path, default=Path("config/sources"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--evidence-dir", type=Path, default=Path("evidence"))
    parser.add_argument("--journal", type=Path, default=Path("data/a3/journal.jsonl"))
    parser.add_argument("--lock-file", type=Path, default=Path("data/a3/.runner.lock"))
    args = parser.parse_args(argv)

    configs = load_all_sources(args.config_dir)
    wanted = list(configs) if args.source == "all" else [args.source]

    now = datetime.now(UTC)
    run_id = _run_id(now)

    with _run_lock(args.lock_file) as (acquired, waited):
        if not acquired:
            record_skipped_locked(args.journal, run_id, args.mode, waited)
            print(f"another omt-g0a3 run held the lock for {waited:.0f}s; "
                  f"journaled SKIPPED_LOCKED", file=sys.stderr)
            return 1
        if waited:
            print(f"lock contention: waited {waited:.0f}s for a prior run")

        from .net import default_http_get

        http_get: HttpGetter = default_http_get
        store = RawStore(args.raw_dir)
        worst = 0
        for source_id in wanted:
            if source_id not in configs:
                print(f"ERROR: no config for source_id={source_id}", file=sys.stderr)
                return 2
            result = run_scheduled(source_id, configs[source_id], store, http_get,
                                   mode=args.mode, now=now)
            if waited:
                result.context["lock_wait_seconds"] = round(waited, 3)
            manifest_path = write_run_evidence(args.evidence_dir, result, run_id)
            append_journal(args.journal, result, run_id)
            print(f"{source_id}: mode={args.mode} status={result.status} "
                  f"objects={result.object_count} errors={len(result.errors)}")
            print(f"  evidence: {manifest_path}")
            if result.status != "SUCCEEDED":
                worst = 1
        return worst


if __name__ == "__main__":
    raise SystemExit(main())
