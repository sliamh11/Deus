#!/usr/bin/env python3
"""
Safety net for the SessionEnd auto-save queue (see ../session_end_hook.py and
../auto_compress.py). Recovers entries a crashed/killed detached worker
missed:

- Stale `*.json` (queued_at older than the debounce window): process
  synchronously via the same claim -> run -> outcome path the worker uses.
- Orphaned `*.json.running` (older than 2x the worker's own belt window):
  the worker that claimed it died mid-run; reset for retry.
- `attempts >= 3`: give up, rename to `.failed`, print one stderr line
  (surfaces in logs/maintenance.log for /review-logs).

Capped at 3 entries per run to bound runtime and daily cost. Registered in
scripts/maintenance.py's Daily section.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from session_end_hook import (  # noqa: E402
    QUEUE_DIR,
    _debounce_minutes,
    _worker_ceiling,
    process_entry,
)

MAX_ENTRIES_PER_RUN = 3
MAX_ATTEMPTS = 3


def sweep(dry_run: bool = False) -> int:
    if not QUEUE_DIR.exists():
        print("compress_sweep: no queue dir, nothing to do")
        return 0

    debounce_s = _debounce_minutes() * 60
    ceiling = _worker_ceiling()
    running_stale_after = 2 * (debounce_s + ceiling)

    now = time.time()
    processed = 0

    for running in sorted(QUEUE_DIR.glob("*.json.running")):
        try:
            age = now - running.stat().st_mtime
        except OSError:
            continue
        if age <= running_stale_after:
            continue
        json_path = running.with_suffix("")
        if dry_run:
            print(f"compress_sweep: would recover orphaned {running.name}")
            continue
        try:
            payload = json.loads(running.read_text())
        except (json.JSONDecodeError, OSError):
            running.unlink(missing_ok=True)
            continue
        payload["attempts"] = payload.get("attempts", 0) + 1
        tmp = json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, json_path)
        running.unlink(missing_ok=True)
        print(f"compress_sweep: recovered orphaned {running.name} -> attempts={payload['attempts']}")

    entries = sorted(QUEUE_DIR.glob("*.json"))
    for entry in entries:
        if processed >= MAX_ENTRIES_PER_RUN:
            print(f"compress_sweep: hit {MAX_ENTRIES_PER_RUN}-entry cap, {len(entries) - processed} remaining for next run")
            break
        try:
            payload = json.loads(entry.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        try:
            queued_age = now - entry.stat().st_mtime
        except OSError:
            continue
        if queued_age <= debounce_s:
            continue  # not stale yet -- the debounced worker still owns this

        if payload.get("attempts", 0) >= MAX_ATTEMPTS:
            failed = entry.with_suffix(".failed")
            if not dry_run:
                entry.rename(failed)
            print(
                f"compress_sweep: {entry.name} exceeded {MAX_ATTEMPTS} attempts, "
                f"giving up (renamed to {failed.name})",
                file=sys.stderr,
            )
            continue

        if dry_run:
            print(f"compress_sweep: would process stale {entry.name}")
            processed += 1
            continue

        running = entry.with_suffix(".json.running")
        try:
            os.rename(entry, running)
        except FileNotFoundError:
            continue  # lost the race
        process_entry(payload, running, ceiling)
        processed += 1

    print(f"compress_sweep: processed {processed} stale entr{'y' if processed == 1 else 'ies'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen; touch nothing.")
    args = parser.parse_args(argv)
    return sweep(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
