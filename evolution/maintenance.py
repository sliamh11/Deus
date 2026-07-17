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


def _score_single(row: dict, judge) -> dict | None:
    """Score a single interaction. Returns score info dict or None on failure."""
    from .ilog.interaction_log import update_score
    from .persona import digest_for_group

    try:
        result = judge.evaluate(
            prompt=row["prompt"],
            response=row.get("response") or "",
            # Deliberately NOT passing available_tools (LIA-154) — it is
            # observability-only; feeding it to the judge prompt would move live
            # scores. Tool-aware scoring is LIA-151's scope.
            tools_used=row.get("tools_used"),
            user_profile=digest_for_group(row.get("group_folder")),
        )
        dims = {
            "quality": result.quality,
            "safety": result.safety,
            "tool_use": result.tool_use,
            "personalization": result.personalization,
        }
        update_score(row["id"], result.score, dims, parse_error=result.is_parse_error, schema_version=result.schema_version)
        return {
            "row": row,
            "score": result.score,
            "dims": dims,
            "rationale": result.rationale,
            "is_parse_error": result.is_parse_error,
        }
    except Exception as exc:
        log.warning("Failed to score interaction %s: %s", row["id"], exc)
        return None


def _reflect_single(scored: dict, config: dict) -> bool:
    """Generate reflection(s) for a scored interaction. Returns True on success."""
    from .config import MAX_REFLECTIONS_TO_GENERATE
    from .metrics import parse_metrics
    from .reflexion.generator import generate_reflection, generate_positive_reflection
    from .reflexion.store import save_reflection

    row = scored["row"]
    metrics = parse_metrics(row.get("metrics"))
    try:
        if scored["score"] < config["reflection_threshold"]:
            generated_contents: set[str] = set()
            for _ in range(MAX_REFLECTIONS_TO_GENERATE):
                content, category = generate_reflection(
                    prompt=row["prompt"],
                    response=row.get("response") or "",
                    score=scored["score"],
                    dims=scored["dims"],
                    rationale=scored["rationale"],
                    tools_used=row.get("tools_used"),
                    metrics=metrics,
                )
                if content in generated_contents:
                    break  # LLM returned identical text; stop early
                generated_contents.add(content)
                save_reflection(
                    content=content,
                    category=category,
                    score_at_gen=scored["score"],
                    interaction_id=row["id"],
                    group_folder=row.get("group_folder"),
                    polarity="corrective",
                )
        elif scored["score"] >= config["positive_threshold"]:
            generated_contents = set()
            for _ in range(MAX_REFLECTIONS_TO_GENERATE):
                content, category = generate_positive_reflection(
                    prompt=row["prompt"],
                    response=row.get("response") or "",
                    score=scored["score"],
                    dims=scored["dims"],
                    rationale=scored["rationale"],
                    tools_used=row.get("tools_used"),
                    metrics=metrics,
                )
                if content in generated_contents:
                    break  # LLM returned identical text; stop early
                generated_contents.add(content)
                save_reflection(
                    content=content,
                    category=category,
                    score_at_gen=scored["score"],
                    interaction_id=row["id"],
                    group_folder=row.get("group_folder"),
                    polarity="positive",
                )
        return True
    except Exception as exc:
        log.warning("Failed to generate reflection for %s: %s", row["id"], exc)
        return False


def judge_pending_interactions() -> int:
    """
    Judge unjudged interactions using a two-pass parallel approach:
      Pass 1: Score all interactions concurrently (GPU-bound)
      Pass 2: Generate reflections for low/high scorers concurrently

    Returns the number of interactions successfully scored.
    """
    import os
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from .config import REFLECTION_THRESHOLD, POSITIVE_THRESHOLD
    from .judge import make_runtime_judge
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

    config = {
        "reflection_threshold": REFLECTION_THRESHOLD,
        "positive_threshold": POSITIVE_THRESHOLD,
    }

    workers = int(os.environ.get("EVOLUTION_JUDGE_WORKERS", "4"))

    # Pass 1: Score all in parallel
    scored_results = []
    parse_errors = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_score_single, row, judge): row["id"]
            for row in unjudged
        }
        for future in as_completed(futures):
            result = future.result()
            if result is None:
                continue
            if result["is_parse_error"]:
                parse_errors += 1
            else:
                scored_results.append(result)

    # Pass 2: Reflect in parallel for interactions that need it
    needs_reflection = [
        s for s in scored_results
        if s["score"] < config["reflection_threshold"]
        or s["score"] >= config["positive_threshold"]
    ]
    if needs_reflection:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(lambda s: _reflect_single(s, config), needs_reflection))

    return len(scored_results) + parse_errors


def process_human_feedback(eps: float = 0.05) -> dict:
    """
    Route human-scored interactions (record_human_feedback writers) into
    corrective/positive reflections, mirroring judge-driven reflection
    generation but keyed on a human_score instead of a judge score.

    For each unprocessed row (human_score IS NOT NULL, processed_at IS NULL):
      - human_score <= REFLECTION_THRESHOLD -> corrective reflection.
      - human_score >= POSITIVE_THRESHOLD - eps AND the judge disagreed
        (judge_score < POSITIVE_THRESHOLD) -> positive reflection. Requiring
        judge disagreement avoids a redundant positive reflection when the
        judge already scored the interaction as positive. A missing
        judge_score (judge never ran) is treated as an optimistic 1.0 --
        NOT disagreement -- so an unjudged high-human-score row is skipped
        rather than generating a possibly-redundant positive reflection.
      - otherwise -> skipped (no reflection warranted).

    Zone-alignment archival: once a reflection is generated (or dedups
    against an existing one) for a row, any EXISTING reflection on the same
    interaction with the CONTRADICTING polarity is archived — the
    human-verified zone is now authoritative. NULL-polarity legacy rows are
    never archived (deliberate; see docs/KNOWN_LIMITATIONS.md).

    The try/except wraps the ENTIRE per-row body, including the
    direction-is-None early-skip branch, so ANY database write failure for a
    row lands uniformly in `errored` and never aborts the rest of the batch.
    Every counter increment happens strictly AFTER its row's corresponding
    store.update_interaction(processed_at=now) call succeeds, so a mid-row
    failure can only ever land in `errored` -- never double-counted into a
    success bucket too.

    Known limitations (accepted, not fixed here):
      - No atomic per-row claim before processing. run_maintenance() is
        invoked inline from the fire-and-forget per-interaction path
        (cli.py cmd_log_interaction), so overlapping invocations are
        possible if multiple interactions land concurrently across
        channels. A race would produce a duplicate generate_reflection call
        on the same row, not a duplicate DB row (save_reflection's existing
        dedup check catches the second write). This is the same
        unaddressed architectural gap judge_pending_interactions() already
        has (no atomic claim there either) -- not novel to this function,
        and out of scope to fix in isolation here.
      - is_maintenance_due()'s due-check is driven by new-interaction count
        delta only; record_human_feedback() (an UPDATE on an existing row)
        does not bump that counter, so a feedback row written with no new
        interactions following it would not trigger maintenance. This has
        no live effect yet: record_human_feedback() has no caller in this
        PR (the external-trace ingester and write-back caller are
        explicitly deferred -- see docs/KNOWN_LIMITATIONS.md), so no row
        can actually reach human_score IS NOT NULL today. Flagged as a
        requirement for LIA-443, which wires the producer.

    Returns counters: {"corrective": int, "positive": int, "skipped": int, "errored": int}.
    """
    import json

    from .config import REFLECTION_THRESHOLD, POSITIVE_THRESHOLD
    from .metrics import parse_metrics
    from .reflexion.generator import generate_reflection, generate_positive_reflection
    from .reflexion.sanitize_human_comment import sanitize_human_comment
    from .reflexion.store import archive_reflection_by_id, save_reflection
    from .reflexion.validation import is_valid_reflection
    from .storage import get_storage

    store = get_storage()
    rows = store.get_unprocessed_human_feedback()
    now = datetime.now(timezone.utc).isoformat()
    counters = {"corrective": 0, "positive": 0, "skipped": 0, "errored": 0}

    for row in rows:
        try:
            judge_score = row.get("judge_score")
            judge_score = 1.0 if judge_score is None else judge_score

            if row["human_score"] <= REFLECTION_THRESHOLD:
                direction = "corrective"
            elif row["human_score"] >= POSITIVE_THRESHOLD - eps and judge_score < POSITIVE_THRESHOLD:
                direction = "positive"
            else:
                direction = None

            if direction is None:
                store.update_interaction(row["id"], processed_at=now)
                counters["skipped"] += 1
                continue

            rationale = sanitize_human_comment(row.get("human_comment"))
            metrics = parse_metrics(row.get("metrics"))
            # tools_used is stored as a JSON TEXT column (see db.py schema);
            # decode before passing to the generator, which expects a list
            # and joins its elements (matching cli.py:472's convention --
            # NOT row.get("tools_used") directly, which would hand the
            # generator a raw JSON string and produce character-joined
            # metadata in the prompt).
            tools_used = json.loads(row.get("tools_used") or "[]")
            gen_fn = generate_reflection if direction == "corrective" else generate_positive_reflection
            content, category = gen_fn(
                prompt=row["prompt"], response=row.get("response") or "",
                score=row["human_score"], rationale=rationale,
                tools_used=tools_used, metrics=metrics,
            )
            ok, reason = is_valid_reflection(content)
            if not ok:
                log.warning("Generated reflection failed validation for %s (%s); leaving unprocessed for retry next cycle", row["id"], reason)
                counters["errored"] += 1
                continue  # processed_at NOT set -- validation failure not stable, retry may pass next cycle

            reflection_id = save_reflection(
                content=content, category=category, score_at_gen=row["human_score"],
                interaction_id=row["id"], group_folder=row.get("group_folder"), polarity=direction,
            )

            # Zone-alignment archival: runs whenever content passed validation
            # (fresh save OR dedup against an existing reflection) -- the
            # human-verified zone is established either way. NULL-polarity
            # legacy rows never archived (verbatim per original issue #1011
            # spec; documented in docs/KNOWN_LIMITATIONS.md).
            contradicting = "positive" if direction == "corrective" else "corrective"
            for r in store.get_reflections_for_interaction(row["id"]):
                if r.get("polarity") == contradicting:
                    archive_reflection_by_id(r["id"])  # soft-delete, ADR-compliant

            store.update_interaction(row["id"], processed_at=now)

            # Counter increment happens ONLY after the row is fully committed
            # (processed_at set), so a mid-row failure lands in `errored`
            # alone -- never double-counted into a success bucket too.
            if reflection_id is not None:
                counters[direction] += 1
            else:
                # content already confirmed valid above -> None here can only
                # be the dedup branch -- stable, correctly permanent.
                counters["skipped"] += 1
        except Exception as exc:
            # Accepted limitation: a row that always raises (generation OR
            # archival OR either update_interaction call) is retried every
            # cycle forever -- no attempts cap (would need a new schema
            # column, judged not cheap relative to this plan's existing
            # 5-column migration; deferred). Reprocessing stays visible via
            # the errored counter + this warning log. Exactly one counter
            # bucket is ever incremented per row -- see fix note above.
            log.warning("Failed to process human feedback for %s: %s", row["id"], exc)
            counters["errored"] += 1
            continue
    return counters


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
                    "Summarize this AI interaction for eval pipeline trend analysis. "
                    "Preserve WHY it scored well/poorly. Include: user ask, assistant action, "
                    "tools used, outcome success. Under 100 words, one paragraph.\n\n"
                    f"User: {prompt_snippet}\n"
                    f"Assistant: {response_snippet}\n"
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
      1.5. Process human feedback (route human-scored interactions into
           corrective/positive reflections; LIA-1011).
      2. Archive stale reflections (never retrieved, older than ``days`` days).
      3. Compact old interactions (replace full text with summary).

    Returns a summary dict:
        {
          "judged_interactions": int,
          "human_feedback_processed": dict,  # {"corrective", "positive", "skipped", "errored"}
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
            "human_feedback_processed": {"corrective": 0, "positive": 0, "skipped": 0, "errored": 0},
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

    # 1.5. Process human feedback (LIA-1011)
    human_feedback_processed = process_human_feedback()
    if any(human_feedback_processed.values()):
        log.info("Processed human feedback: %s", human_feedback_processed)

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
        "human_feedback_processed": human_feedback_processed,
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
