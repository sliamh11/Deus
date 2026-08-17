#!/usr/bin/env python3
"""
Stale-job sweep for the LIA-527 Phase 2 CC write-path queue
(see ../warden_policy/cc_attestations.py and
../../docs/decisions/opa-warden-attestations-v1.md's Phase 2 section).

Deletes any `*.json` in `cc_attestations.QUEUE_DIR` older than 24h -- a job the
detached worker never got to, or got to and crashed before deleting. No
`.running`/`.failed` state machine or attempts counter is needed here (unlike
compress_sweep.py's sibling queue): this design already chose
single-best-effort-no-retry, so a stale file is definitionally one there is
nothing left to retry, only to reclaim.

Registered in scripts/maintenance.py's Daily section.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))
from warden_policy.cc_attestations import QUEUE_DIR  # noqa: E402

STALE_AFTER_SECONDS = 24 * 60 * 60


def sweep(dry_run: bool = False) -> int:
    if not QUEUE_DIR.exists():
        print("cc_write_queue_sweep: no queue dir, nothing to do")
        return 0

    now = time.time()
    deleted = 0
    # `*.json`: normal queued jobs. `.tmp-cc-job-*`: enqueue_verdict's own mkstemp staging
    # prefix (cc_attestations.py) -- a SIGKILL between mkstemp and os.replace leaks one of
    # these with no `.json` suffix, otherwise unreclaimable by this sweep.
    targets = sorted(QUEUE_DIR.glob("*.json")) + sorted(QUEUE_DIR.glob(".tmp-cc-job-*"))
    for entry in targets:
        try:
            age = now - entry.stat().st_mtime
        except OSError:
            continue
        if age <= STALE_AFTER_SECONDS:
            continue
        if dry_run:
            print(f"cc_write_queue_sweep: would delete stale {entry.name} (age {age:.0f}s)")
            continue
        try:
            entry.unlink()
        except OSError:
            continue
        deleted += 1
        print(f"cc_write_queue_sweep: deleted stale {entry.name} (age {age:.0f}s)")

    print(f"cc_write_queue_sweep: deleted {deleted} stale job(s)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen; touch nothing.")
    args = parser.parse_args(argv)
    return sweep(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
