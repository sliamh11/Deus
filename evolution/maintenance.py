"""
Evolution maintenance tasks.

Handles periodic cleanup of the evolution DB:
  - Archive stale reflections (never retrieved, older than N days)

Can be called programmatically or from the CLI:
    python3 -m evolution.maintenance

Scheduling logic uses a lightweight timestamp check stored in the DB so
maintenance never runs more than once per calendar day regardless of how
many interactions are logged.
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# Allow running as a module entry-point
if __name__ == "__main__" and __package__ is None:
    _project_root = str(Path(__file__).parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
    __package__ = "evolution"  # type: ignore

# ── Constants ─────────────────────────────────────────────────────────────────

#: Stale reflection threshold in days.
ARCHIVE_AFTER_DAYS = 30

#: Maintenance runs at most once per this many interactions.
MAINTENANCE_INTERACTION_INTERVAL = 25

#: Key used to store the last-maintenance timestamp in the meta table
#: (stored as a plain interaction with a sentinel group_folder).
_SENTINEL_GROUP = "__maintenance__"
_SENTINEL_ID = "maintenance:last_run"


# ── Public API ────────────────────────────────────────────────────────────────


def is_maintenance_due(*, interaction_count: Optional[int] = None) -> bool:
    """
    Return True if maintenance should run now.

    Maintenance is due when EITHER condition is satisfied:
      1. It has never run before.
      2. It has been at least MAINTENANCE_INTERACTION_INTERVAL interactions
         since the last run (based on total interaction count delta).

    The check is cheap and uses only the existing storage layer — no extra
    DB tables or files required.

    Args:
        interaction_count: Pre-fetched total interaction count. If None the
            function queries the DB itself (adds ~1 ms).
    """
    from .storage import get_storage

    store = get_storage()
    last = store.get_interaction(_SENTINEL_ID)
    if last is None:
        return True  # Never ran before

    # Compare stored interaction count snapshot against current total
    try:
        stored_count = int(last.get("latency_ms") or 0)
    except (ValueError, TypeError):
        return True  # Corrupt record — run maintenance to be safe

    if interaction_count is None:
        interaction_count = store.count_interactions()

    return (interaction_count - stored_count) >= MAINTENANCE_INTERACTION_INTERVAL


def run_maintenance(*, days: int = ARCHIVE_AFTER_DAYS, force: bool = False) -> dict:
    """
    Run evolution maintenance tasks.

    Tasks performed:
      1. Archive stale reflections (never retrieved, older than ``days`` days).

    Returns a summary dict:
        {
          "archived_reflections": int,
          "ran_at": ISO-8601 timestamp,
          "skipped": bool,   # True when is_maintenance_due() returned False
        }

    Args:
        days:  Age threshold for archiving reflections (default: 30).
        force: Skip the is_maintenance_due() check and run unconditionally.
    """
    from .reflexion.store import archive_stale_reflections
    from .storage import get_storage

    store = get_storage()
    total = store.count_interactions()

    if not force and not is_maintenance_due(interaction_count=total):
        log.debug("Maintenance skipped — not due yet (total=%d)", total)
        return {"archived_reflections": 0, "ran_at": None, "skipped": True}

    ran_at = datetime.now(timezone.utc).isoformat()
    log.info("Running evolution maintenance (total_interactions=%d)", total)

    # 1. Archive stale reflections
    archived = archive_stale_reflections(days=days)
    log.info("Archived %d stale reflection(s) (threshold: %d days)", archived, days)

    # Record that maintenance ran by upserting a sentinel interaction.
    # We reuse latency_ms to store the interaction count snapshot so
    # is_maintenance_due() can compute the delta without a new DB column.
    try:
        existing = store.get_interaction(_SENTINEL_ID)
        if existing:
            store.update_interaction(
                _SENTINEL_ID,
                latency_ms=total,
                timestamp=ran_at,
            )
        else:
            store.log_interaction(
                prompt="[maintenance sentinel]",
                response=None,
                group_folder=_SENTINEL_GROUP,
                timestamp=ran_at,
                interaction_id=_SENTINEL_ID,
                latency_ms=float(total),
                eval_suite="maintenance",
            )
    except Exception as exc:
        # Non-fatal — worst case maintenance runs more often than needed
        log.warning("Could not record maintenance timestamp: %s", exc)

    return {
        "archived_reflections": archived,
        "ran_at": ran_at,
        "skipped": False,
    }


# ── CLI entry-point ───────────────────────────────────────────────────────────


def _main() -> None:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="python3 -m evolution.maintenance",
        description="Run evolution maintenance tasks.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=ARCHIVE_AFTER_DAYS,
        help=f"Stale reflection threshold in days (default: {ARCHIVE_AFTER_DAYS})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if maintenance is not yet due",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="Output result as JSON",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = run_maintenance(days=args.days, force=args.force)

    if args.as_json:
        print(json.dumps(result))
    elif result["skipped"]:
        print("Maintenance skipped — not due yet.")
    else:
        print(
            f"Maintenance complete: archived {result['archived_reflections']} "
            f"stale reflection(s) at {result['ran_at']}"
        )


if __name__ == "__main__":
    _main()
