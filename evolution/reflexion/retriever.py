"""
Reflection retriever: semantic top-k lookup keyed by query + planned tools.
Returns a formatted <reflections> block ready to prepend to the agent prompt.
"""
from typing import Optional

from ..config import MAX_REFLECTIONS_PER_QUERY
from ..db import open_db, serialize_vec
from ..providers.embeddings import embed as _embed
from .store import increment_retrieved


def get_reflections(
    query: str,
    group_folder: Optional[str] = None,
    tools_planned: Optional[list[str]] = None,
    top_k: int = MAX_REFLECTIONS_PER_QUERY,
) -> list[dict]:
    """
    Return top-k reflections most semantically relevant to the query.
    Merges the query with planned tool names for better retrieval.
    Group-scoped reflections are prioritised over cross-group ones.
    """
    search_text = query
    if tools_planned:
        search_text += " tools: " + ", ".join(tools_planned)

    vec = _embed(search_text)
    db = open_db()

    # Group-scoped + cross-group (NULL), ordered by L2 distance then helpfulness
    rows = db.execute(
        f"""
        SELECT r.id, r.content, r.category, r.score_at_gen,
               r.times_helpful, r.times_retrieved,
               re.distance
        FROM reflection_embeddings re
        JOIN reflections r ON r.rowid = re.rowid
        WHERE (r.group_folder = ? OR r.group_folder IS NULL)
        ORDER BY re.distance, r.times_helpful DESC
        LIMIT ?
        """,
        [group_folder, top_k * 2],  # fetch extra, trim after
        # Note: vec0 WHERE vec_distance_cosine(...) syntax below
    ).fetchall()
    db.close()

    # Fallback: vec0 KNN syntax (requires passing the query vector)
    if not rows:
        db = open_db()
        blob = serialize_vec(vec)
        rows = db.execute(
            f"""
            SELECT r.id, r.content, r.category, r.score_at_gen,
                   r.times_helpful, r.times_retrieved,
                   re.distance
            FROM reflection_embeddings re
            JOIN reflections r ON r.rowid = re.rowid
            WHERE re.embedding MATCH ? AND k = ?
              AND (r.group_folder = ? OR r.group_folder IS NULL)
            ORDER BY re.distance, r.times_helpful DESC
            """,
            [blob, top_k * 2, group_folder],
        ).fetchall()
        db.close()

    results = [dict(zip(
        ["id", "content", "category", "score_at_gen", "times_helpful", "times_retrieved", "distance"],
        row,
    )) for row in rows[:top_k]]

    # Track retrieval counts
    for r in results:
        increment_retrieved(r["id"])

    return results


def format_reflections_block(reflections: list[dict]) -> str:
    """
    Format retrieved reflections as a compact prompt block.
    Returns empty string if list is empty (no tokens added).
    """
    if not reflections:
        return ""

    lines = ["<reflections>"]
    for i, r in enumerate(reflections, 1):
        lines.append(f"[{i}] ({r['category']}) {r['content'].strip()}")
    lines.append("</reflections>")
    return "\n".join(lines)
