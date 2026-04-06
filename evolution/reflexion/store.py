"""
Reflection store: persists reflections + embeddings for semantic retrieval.
"""
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from ..config import REFLECTION_DEDUP_L2
from ..db import serialize_vec
from ..providers.embeddings import embed as _embed
from ..storage import get_storage

log = logging.getLogger(__name__)


def _is_duplicate(vec: list[float], group_folder: Optional[str], threshold: float = REFLECTION_DEDUP_L2) -> bool:
    """
    Check if a semantically similar reflection already exists.

    Dedup scope:
    - Cross-group (group_folder=None) reflections dedup against ALL reflections.
    - Group-specific reflections dedup only against reflections in the SAME group,
      so a cross-group principle never blocks a group-specific lesson.
    """
    store = get_storage()
    blob = serialize_vec(vec)
    return store.check_reflection_duplicate(blob, group_folder, threshold)


def save_reflection(
    content: str,
    category: str,
    score_at_gen: float,
    interaction_id: Optional[str] = None,
    group_folder: Optional[str] = None,
) -> Optional[str]:
    """
    Embed and persist a reflection.  Returns the reflection ID,
    or None if a semantically similar reflection already exists.
    group_folder=None means the reflection applies cross-group.
    """
    rid = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    vec = _embed(content)

    # Dedup: skip if a near-duplicate reflection already exists
    if _is_duplicate(vec, group_folder):
        return None

    store = get_storage()
    store.save_reflection(
        reflection_id=rid,
        content=content,
        category=category,
        score_at_gen=score_at_gen,
        timestamp=ts,
        embedding=serialize_vec(vec),
        interaction_id=interaction_id,
        group_folder=group_folder,
    )
    return rid


def increment_retrieved(reflection_id: str) -> None:
    store = get_storage()
    store.increment_reflection_retrieved(reflection_id)


def archive_stale_reflections(days: int = 30, dry_run: bool = False) -> int:
    """
    Archive reflections that have never been retrieved and are older than
    `days` days.  Sets archived_at = now (soft-delete).
    Returns the count of archived (or would-be-archived) reflections.
    """
    store = get_storage()
    if dry_run:
        count = store.count_stale_reflections(days)
    else:
        count = store.archive_stale_reflections(days)

    action = "Would archive" if dry_run else "Archived"
    log.info("%s %d stale reflections (threshold: %d days)", action, count, days)
    return count


def increment_helpful(reflection_id: str) -> None:
    store = get_storage()
    store.increment_reflection_helpful(reflection_id)
