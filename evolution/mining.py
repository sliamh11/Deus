"""
Session-correction mining for the Evolution loop.

Retroactively extracts implicit negative signals from existing interactions by
detecting correction patterns in follow-up messages within the same session.
Batch Filter/Transform pattern: stateless pure functions, no class hierarchy.
"""
import logging
import re
from datetime import datetime, timezone
from typing import Optional

from .config import CORRECTION_VOCAB, CORRECTION_MAX_PROMPT_LEN
from .storage import get_storage

log = logging.getLogger(__name__)

# Pre-compile vocab patterns for efficiency
_CORRECTION_PATTERNS = [re.compile(re.escape(v), re.IGNORECASE) for v in CORRECTION_VOCAB]


def _is_correction(text: str) -> bool:
    """Check if text matches any correction vocabulary pattern."""
    lower = text.lower().strip()
    for pattern in _CORRECTION_PATTERNS:
        if pattern.search(lower):
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
    db = store._connect()

    # Self-join: find interactions followed by short corrective messages
    query = """
        SELECT a.id AS target_id,
               a.prompt AS target_prompt,
               b.prompt AS followup_prompt,
               a.session_id
        FROM interactions a
        JOIN interactions b ON b.session_id = a.session_id
            AND b.timestamp > a.timestamp
            AND b.id != a.id
        WHERE a.user_signal IS NULL
          AND a.session_id IS NOT NULL
          AND LENGTH(b.prompt) < ?
        ORDER BY a.session_id, a.timestamp
    """
    rows = db.execute(query, (CORRECTION_MAX_PROMPT_LEN,)).fetchall()

    # Deduplicate: only the first follow-up per target interaction
    seen_targets: set = set()
    matched = []
    for row in rows:
        target_id = row[0]
        if target_id in seen_targets:
            continue
        followup = row[2]
        if _is_correction(followup):
            seen_targets.add(target_id)
            matched.append({
                "target_id": target_id,
                "target_prompt": row[1][:100],
                "followup": followup[:100],
                "session_id": row[3],
            })
            if limit and len(matched) >= limit:
                break

    updated = 0
    skipped = 0
    now = datetime.now(timezone.utc).isoformat()

    if not dry_run and matched:
        for m in matched:
            try:
                db.execute(
                    """UPDATE interactions
                       SET user_signal = 'correction',
                           correction_mined_at = ?
                       WHERE id = ? AND user_signal IS NULL""",
                    (now, m["target_id"]),
                )
                updated += 1
            except Exception as exc:
                log.warning("Failed to update %s: %s", m["target_id"], exc)
                skipped += 1
        db.commit()

    db.close()

    return {
        "matched": len(matched),
        "updated": updated,
        "skipped": skipped,
        "examples": matched[:5],
    }
