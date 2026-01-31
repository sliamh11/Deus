"""
Interaction logging for the Evolution loop.
Writes one row per agent call; judge scores are updated asynchronously.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from ..db import open_db


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
) -> str:
    """
    Persist one agent interaction.  Returns the interaction ID.
    Judge score is written later by update_score().
    """
    iid = interaction_id or str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    db = open_db()
    db.execute(
        """
        INSERT OR REPLACE INTO interactions
            (id, timestamp, group_folder, prompt, response, tools_used,
             latency_ms, eval_suite, session_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            iid, ts, group_folder, prompt, response,
            json.dumps(tools_used or []),
            latency_ms, eval_suite, session_id,
        ),
    )
    db.commit()
    db.close()
    return iid


def update_score(
    interaction_id: str,
    score: float,
    dims: dict,
) -> None:
    """Attach judge score and dimension breakdown to a logged interaction."""
    db = open_db()
    db.execute(
        "UPDATE interactions SET judge_score = ?, judge_dims = ? WHERE id = ?",
        (score, json.dumps(dims), interaction_id),
    )
    db.commit()
    db.close()


def get_recent(
    group_folder: Optional[str] = None,
    limit: int = 50,
    min_score: Optional[float] = None,
    max_score: Optional[float] = None,
    eval_suite: Optional[str] = "runtime",
) -> list[dict]:
    """Fetch recent interactions, optionally filtered.  Pass eval_suite=None to include all suites."""
    db = open_db()
    clauses: list[str] = []
    params: list = []

    if eval_suite is not None:
        clauses.append("eval_suite = ?")
        params.append(eval_suite)
    if group_folder:
        clauses.append("group_folder = ?")
        params.append(group_folder)
    if min_score is not None:
        clauses.append("judge_score >= ?")
        params.append(min_score)
    if max_score is not None:
        clauses.append("judge_score <= ?")
        params.append(max_score)

    where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = db.execute(
        f"SELECT * FROM interactions {where_clause} ORDER BY timestamp DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]


def score_trend(group_folder: Optional[str] = None, days: int = 30) -> list[dict]:
    """Daily average judge scores for the last N days."""
    db = open_db()
    params: list = []
    group_clause = ""
    if group_folder:
        group_clause = "AND group_folder = ?"
        params.append(group_folder)
    rows = db.execute(
        f"""
        SELECT DATE(timestamp) AS day, AVG(judge_score) AS avg_score, COUNT(*) AS count
        FROM interactions
        WHERE judge_score IS NOT NULL
          AND timestamp >= DATETIME('now', '-{days} days')
          {group_clause}
        GROUP BY day
        ORDER BY day
        """,
        params,
    ).fetchall()
    db.close()
    return [dict(r) for r in rows]
