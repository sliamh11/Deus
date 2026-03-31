"""
Reflection store: persists reflections + embeddings for semantic retrieval.
"""
import struct
import uuid
from datetime import datetime, timezone
from typing import Optional

from ..db import open_db, serialize_vec
from ..providers.embeddings import embed as _embed


def save_reflection(
    content: str,
    category: str,
    score_at_gen: float,
    interaction_id: Optional[str] = None,
    group_folder: Optional[str] = None,
) -> str:
    """
    Embed and persist a reflection.  Returns the reflection ID.
    group_folder=None means the reflection applies cross-group.
    """
    rid = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    vec = _embed(content)

    db = open_db()
    # Insert reflection row
    db.execute(
        """
        INSERT INTO reflections
            (id, interaction_id, timestamp, group_folder, content,
             category, score_at_gen)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (rid, interaction_id, ts, group_folder, content, category, score_at_gen),
    )
    # Insert embedding — rowid must be the reflection row's rowid, but we use
    # a separate mapping: store rid as a hex-encoded int rowid for portability.
    row = db.execute("SELECT rowid FROM reflections WHERE id = ?", [rid]).fetchone()
    rowid = row[0]
    db.execute(
        "INSERT INTO reflection_embeddings(rowid, embedding) VALUES (?, ?)",
        (rowid, serialize_vec(vec)),
    )
    db.commit()
    db.close()
    return rid


def increment_retrieved(reflection_id: str) -> None:
    db = open_db()
    db.execute(
        "UPDATE reflections SET times_retrieved = times_retrieved + 1 WHERE id = ?",
        [reflection_id],
    )
    db.commit()
    db.close()


def increment_helpful(reflection_id: str) -> None:
    db = open_db()
    db.execute(
        "UPDATE reflections SET times_helpful = times_helpful + 1 WHERE id = ?",
        [reflection_id],
    )
    db.commit()
    db.close()
