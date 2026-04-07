"""
Evolution maintenance tasks.

Handles periodic cleanup of the evolution DB:
  1. Judge pending interactions (batch scoring of unjudged entries)
  2. Archive stale reflections (never retrieved, older than N days)
  3. Compact old interactions (replace full text with summary after N days)

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


def judge_pending_interactions() -> int:
    """
    Judge all unjudged interactions in a single batch.

    Called by maintenance to catch up on interactions that were logged
    but not immediately judged (due to batch thresholds not being met).

    Returns the number of interactions successfully judged.
    """
    import asyncio
    from .config import REFLECTION_THRESHOLD, POSITIVE_THRESHOLD
    from .ilog.interaction_log import update_score
    from .judge import make_runtime_judge
    from .reflexion.generator import generate_reflection, generate_positive_reflection
    from .reflexion.store import save_reflection
    from .storage import get_storage

    store = get_storage()
    unjudged = store.get_unjudged_interactions(limit=50)
    if not unjudged:
        return 0

    try:
        judge = make_runtime_judge()
    except Exception as exc:
        log.warning("Could not create judge for batch judging: %s", exc)
        return 0

    judged = 0
    for row in unjudged:
        try:
            result = asyncio.run(judge.a_evaluate(
                prompt=row["prompt"],
                response=row.get("response") or "",
                tools_used=row.get("tools_used"),
            ))
            dims = {
                "quality": result.quality,
                "safety": result.safety,
                "tool_use": result.tool_use,
                "personalization": result.personalization,
            }
            update_score(row["id"], result.score, dims, parse_error=result.is_parse_error)
            judged += 1

            if result.is_parse_error:
                continue
            if result.score < REFLECTION_THRESHOLD:
                content, category = generate_reflection(
                    prompt=row["prompt"],
                    response=row.get("response") or "",
                    score=result.score,
                    dims=dims,
                    rationale=result.rationale,
                    tools_used=row.get("tools_used"),
                )
                save_reflection(
                    content=content,
                    category=category,
                    score_at_gen=result.score,
                    interaction_id=row["id"],
                    group_folder=row.get("group_folder"),
                )
            elif result.score >= POSITIVE_THRESHOLD:
                content, category = generate_positive_reflection(
                    prompt=row["prompt"],
                    response=row.get("response") or "",
                    score=result.score,
                    dims=dims,
                    rationale=result.rationale,
                    tools_used=row.get("tools_used"),
                )
                save_reflection(
                    content=content,
                    category=category,
                    score_at_gen=result.score,
                    interaction_id=row["id"],
                    group_folder=row.get("group_folder"),
                )
        except Exception as exc:
            log.warning("Failed to judge interaction %s: %s", row["id"], exc)

    return judged


def _truncation_fallback(prompt_snippet: str, tools_info: str, score_info: str) -> str:
    """Build a compact summary from truncated prompt + metadata when no LLM is available."""
    parts = [prompt_snippet[:200]]
    if tools_info:
        parts.append(tools_info.strip())
    if score_info:
        parts.append(score_info.strip())
    return " ".join(parts) + " [compacted]"


def compact_old_interactions() -> int:
    """
    Replace old interactions' full text with a one-line summary.

    Uses the generative provider (Gemma4 via Ollama preferred, Gemini fallback)
    to summarize each interaction. On provider failure, falls back to simple
    truncation so compaction always progresses.

    Returns the number of interactions compacted.
    """
    from .config import COMPACT_AFTER_DAYS
    from .storage import get_storage

    store = get_storage()
    compactable = store.get_compactable_interactions(days=COMPACT_AFTER_DAYS, limit=50)
    if not compactable:
        return 0

    # Try to use the generative module for intelligent summarization
    can_generate = False
    try:
        from .generative import generate as gen_generate
        from .generative.provider import GenerativeRegistry
        provider = GenerativeRegistry.default().resolve()
        can_generate = provider.is_available()
    except Exception:
        pass

    compacted = 0
    for row in compactable:
        try:
            prompt_snippet = (row["prompt"] or "")[:500]
            response_snippet = (row.get("response") or "")[:500]

            tools_info = ""
            if row.get("tools_used"):
                tools_info = f" Tools used: {row['tools_used']}."
            score_info = ""
            if row.get("judge_score") is not None:
                score_info = f" Judge score: {row['judge_score']:.2f}."

            if can_generate:
                summary_prompt = (
                    "Summarize this AI interaction for a quality evaluation pipeline. "
                    "The summary will be used for pattern extraction and trend analysis "
                    "(the interaction has already been scored — preserve enough context "
                    "for a reader to understand WHY it scored well or poorly). "
                    "Include: (1) what the user asked for, (2) what the assistant did, "
                    "(3) tools used if any, (4) whether the outcome was successful. "
                    "Keep it under 100 words, one paragraph.\n\n"
                    f"User asked: {prompt_snippet}\n"
                    f"Assistant responded: {response_snippet}\n"
                    f"{tools_info}{score_info}"
                )
                try:
                    summary = gen_generate(summary_prompt)
                    summary = summary.strip()[:500]
                except Exception:
                    summary = _truncation_fallback(prompt_snippet, tools_info, score_info)
            else:
                summary = _truncation_fallback(prompt_snippet, tools_info, score_info)

            store.compact_interaction(row["id"], summary)
            compacted += 1
        except Exception as exc:
            log.warning("Failed to compact interaction %s: %s", row["id"], exc)

    return compacted


def run_maintenance(*, days: int = ARCHIVE_AFTER_DAYS, force: bool = False) -> dict:
    """
    Run evolution maintenance tasks.

    Tasks performed:
      1. Judge pending interactions (catch up on unjudged entries).
      2. Archive stale reflections (never retrieved, older than ``days`` days).
      3. Compact old interactions (replace full text with summary).

    Returns a summary dict:
        {
          "judged_interactions": int,
          "archived_reflections": int,
          "compacted_interactions": int,
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
        return {
            "judged_interactions": 0,
            "archived_reflections": 0,
            "compacted_interactions": 0,
            "ran_at": None,
            "skipped": True,
        }

    ran_at = datetime.now(timezone.utc).isoformat()
    log.info("Running evolution maintenance (total_interactions=%d)", total)

    # 1. Judge pending interactions (before compaction so newly-judged entries
    #    aren't immediately compacted)
    judged = judge_pending_interactions()
    if judged:
        log.info("Batch-judged %d pending interaction(s)", judged)

    # 2. Archive stale reflections
    archived = archive_stale_reflections(days=days)
    log.info("Archived %d stale reflection(s) (threshold: %d days)", archived, days)

    # 3. Compact old interactions
    compacted = compact_old_interactions()
    if compacted:
        log.info("Compacted %d old interaction(s)", compacted)

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
        "judged_interactions": judged,
        "archived_reflections": archived,
        "compacted_interactions": compacted,
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
        parts = []
        if result["judged_interactions"]:
            parts.append(f"judged {result['judged_interactions']} interaction(s)")
        parts.append(f"archived {result['archived_reflections']} stale reflection(s)")
        if result["compacted_interactions"]:
            parts.append(f"compacted {result['compacted_interactions']} interaction(s)")
        print(f"Maintenance complete: {', '.join(parts)} at {result['ran_at']}")


if __name__ == "__main__":
    _main()
