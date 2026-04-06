"""
Interaction logging for the Evolution loop.
Writes one row per agent call; judge scores are updated asynchronously.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

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
) -> str:
    """
    Persist one agent interaction.  Returns the interaction ID.
    Judge score is written later by update_score().
    """
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
    )
    return iid


def update_score(
    interaction_id: str,
    score: float,
    dims: dict,
    parse_error: bool = False,
) -> None:
    """Attach judge score and dimension breakdown to a logged interaction."""
    store = get_storage()
    store.update_interaction(
        interaction_id,
        judge_score=score,
        judge_dims=json.dumps(dims),
        parse_error=int(parse_error),
    )


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
