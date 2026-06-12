"""
Interaction logging for the Evolution loop.
Writes one row per agent call; judge scores are updated asynchronously.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from ..metrics import validate_metrics
from ..storage import get_storage


def log_interaction(
    *,
    prompt: str,
    response: Optional[str],
    group_folder: str,
    latency_ms: Optional[float] = None,
    tools_used: Optional[list[str]] = None,
    session_id: Optional[str] = None,
    eval_suite: str = "runtime",
    interaction_id: Optional[str] = None,
    domain_presets: Optional[list[str]] = None,
    user_signal: Optional[str] = None,
    context_tokens: Optional[int] = None,
    has_code: Optional[int] = None,
    tool_calls: Optional[list[dict]] = None,
    available_tools: Optional[list[str]] = None,
    metrics: Optional[dict] = None,
    retrieved_reflection_ids: Optional[list[str]] = None,
) -> str:
    """
    Persist one agent interaction.  Returns the interaction ID.
    Judge score is written later by update_score().

    metrics is a flat dict of task metrics (see evolution.metrics) — validated
    here so a malformed payload fails loudly at log time, not at analysis time.

    retrieved_reflection_ids are the reflections retrieved for this prompt;
    they are persisted now and credited (times_helpful++) at scoring time by
    update_score, once the judge score is known and >= POSITIVE_THRESHOLD
    (LIA-214 — crediting at log time saw a still-NULL judge score ~97% of the
    time and silently skipped).
    """
    # Canonical validation gate: the MCP path calls this directly with no
    # other check (errors propagate to the caller by design). cli.py
    # pre-validates only to add drop-on-error semantics for its
    # fire-and-forget path — that duplication is intentional.
    if metrics is not None:
        validate_metrics(metrics)
    iid = interaction_id or str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    store = get_storage()
    store.log_interaction(
        prompt=prompt,
        response=response,
        group_folder=group_folder,
        timestamp=ts,
        interaction_id=iid,
        latency_ms=latency_ms,
        tools_used=json.dumps(tools_used or []),
        session_id=session_id,
        eval_suite=eval_suite,
        domain_presets=json.dumps(domain_presets) if domain_presets else None,
        user_signal=user_signal,
        context_tokens=context_tokens,
        has_code=has_code,
        # LIA-154: structured tool-call records (observability only, not scored).
        tool_calls=json.dumps(tool_calls or []),
        # LIA-154: offered tool manifest (observability only; unblocks LIA-151).
        available_tools=json.dumps(available_tools or []),
        metrics=json.dumps(metrics) if metrics is not None else None,
        # LIA-214: persisted now, credited at scoring time (see update_score).
        retrieved_reflection_ids=(
            json.dumps(retrieved_reflection_ids)
            if retrieved_reflection_ids
            else None
        ),
    )
    return iid


def update_score(
    interaction_id: str,
    score: float,
    dims: dict,
    parse_error: bool = False,
    schema_version: int = 1,
) -> None:
    """Attach judge score and dimension breakdown to a logged interaction.

    Once the score is known, credit any reflections retrieved for this
    interaction's prompt (LIA-214). This is the single score seam — batch judge
    (maintenance), async MCP, and backfill all route through here — so crediting
    here, gated on an atomic one-shot claim, fixes the log-time temporal race
    (the judge score was still NULL ~97% of the time) without double-crediting.
    """
    store = get_storage()
    store.update_interaction(
        interaction_id,
        judge_score=score,
        judge_dims=json.dumps(dims),
        parse_error=int(parse_error),
        judge_schema_version=schema_version,
    )
    _credit_retrieved_reflections(store, interaction_id, score)


def _credit_retrieved_reflections(store, interaction_id: str, score: float) -> None:
    """Credit (times_helpful++) reflections retrieved for a positively-scored
    interaction, exactly once.

    Gated three ways: score >= POSITIVE_THRESHOLD, the interaction actually had
    retrieved reflections, and an atomic claim_interaction_credit() that only one
    writer can win — so concurrent score writers and judge re-scores credit a
    reflection at most once.
    """
    from ..config import POSITIVE_THRESHOLD

    if score < POSITIVE_THRESHOLD:
        return
    row = store.get_interaction(interaction_id)
    if not row:
        return
    raw = row.get("retrieved_reflection_ids")
    # This early-return is load-bearing: the backfill/cc_backfill paths call
    # update_score WITHOUT wrapping it in try/except and log rows with NULL
    # retrieved_reflection_ids, so returning here (before any raise-capable call)
    # keeps those paths safe.
    if not raw:
        return
    try:
        ids = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return
    if not ids:
        return
    # Atomic one-shot: only the writer that flips credited_at NULL->now credits.
    # The claim is consumed BEFORE the increment loop ON PURPOSE: under concurrent
    # score writers (batch judge + async MCP) claiming after the loop would let
    # both writers increment before either claims = double-credit. Claiming first
    # bounds credit to exactly once; the cost is that a mid-loop failure leaves
    # partial credit (acceptable — these are monotonic counters, not user data).
    if not store.claim_interaction_credit(interaction_id):
        return
    # Routed through reflexion.store.increment_helpful (the single "mark helpful"
    # chokepoint) rather than store.increment_reflection_helpful directly.
    from ..reflexion.store import increment_helpful

    for rid in ids:
        increment_helpful(rid)


def get_recent(
    group_folder: Optional[str] = None,
    limit: int = 50,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    eval_suite: Optional[str] = "runtime",
    domain: Optional[str] = None,
) -> list[dict]:
    """Fetch recent interactions, optionally filtered.  Pass eval_suite=None to include all suites."""
    store = get_storage()
    return store.get_recent_interactions(
        limit=limit,
        group_folder=group_folder,
        min_score=min_score,
        max_score=max_score,
        eval_suite=eval_suite,
        domain=domain,
    )


def get_previous_in_session(session_id: str, exclude_id: str) -> Optional[dict]:
    """Get the most recent interaction in a session, excluding the current one."""
    if not session_id:
        return None
    store = get_storage()
    return store.get_previous_in_session(session_id, exclude_id)


def score_trend(
    group_folder: Optional[str] = None,
    days: int = 30,
    domain: Optional[str] = None,
) -> list[dict]:
    """Daily average judge scores for the last N days."""
    store = get_storage()
    return store.score_trend(
        group_folder=group_folder,
        days=days,
        domain=domain,
    )
