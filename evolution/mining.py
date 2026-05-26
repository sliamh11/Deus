"""
Session-correction mining for the Evolution loop.

Retroactively extracts implicit negative signals from existing interactions
by detecting correction patterns in follow-up messages within the same session.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .config import CORRECTION_VOCAB, CORRECTION_MAX_PROMPT_LEN
from .storage import get_storage

log = logging.getLogger(__name__)

_CORRECTION_PATTERNS = [re.compile(re.escape(v), re.IGNORECASE) for v in CORRECTION_VOCAB]


def _is_correction(text: str) -> bool:
    """Check if text matches any correction vocabulary pattern."""
    for pattern in _CORRECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


def mine_corrections(
    *,
    dry_run: bool = False,
    limit: Optional[int] = None,
) -> dict:
    """
    Mine session-correction signals from existing interactions.

    Finds pairs (A, B) where B is a short follow-up in the same session
    that matches correction vocabulary, indicating A was unsatisfactory.
    Labels A with user_signal='correction'.

    Safety: only updates rows where user_signal IS NULL.

    Returns dict with keys: matched, updated, skipped, examples.
    """
    store = get_storage()
    rows = store.get_correction_candidates(max_followup_len=CORRECTION_MAX_PROMPT_LEN)

    seen_targets: set = set()
    matched = []
    for row in rows:
        target_id = row["target_id"]
        if target_id in seen_targets:
            continue
        followup = row["followup_prompt"]
        if _is_correction(followup):
            seen_targets.add(target_id)
            matched.append({
                "target_id": target_id,
                "target_prompt": row["target_prompt"][:100],
                "followup": followup[:100],
                "session_id": row["session_id"],
            })
            if limit and len(matched) >= limit:
                break

    updated = 0
    now = datetime.now(timezone.utc).isoformat()

    if not dry_run and matched:
        ids = [m["target_id"] for m in matched]
        updated = store.bulk_label_corrections(ids=ids, mined_at=now)

    return {
        "matched": len(matched),
        "updated": updated,
        "skipped": len(matched) - updated if not dry_run else 0,
        "examples": matched[:5],
    }
