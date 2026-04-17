"""
Reflection retriever: semantic top-k lookup keyed by query + planned tools.
Returns a formatted <reflections> block ready to prepend to the agent prompt.
"""
import math
from typing import Optional

from ..config import MAX_REFLECTIONS_PER_QUERY
from ..db import serialize_vec
from ..providers.embeddings import embed as _embed
from ..storage import get_storage
from .store import increment_retrieved

# Small tiebreaker weight for helpful_count re-ranking. Monkeypatch in tests.
HELPFUL_WEIGHT = 0.05


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
    Helpful count applies a log-scaled tiebreaker bonus so frequently-useful
    reflections float up without overriding cosine similarity.
    """
    search_text = query
    if tools_planned:
        search_text += " tools: " + ", ".join(tools_planned)

    vec = _embed(search_text)
    blob = serialize_vec(vec)
    store = get_storage()

    results = store.get_reflections_by_embedding(
        embedding=blob, top_k=top_k, group_folder=group_folder,
    )

    # Re-rank: lower distance = better; subtract helpful bonus to keep unified ordering
    if results:
        for r in results:
            helpful_count = r.get("times_helpful") or 0
            base_score = r.get("distance", 0.0)
            r["_adjusted"] = base_score - math.log(1 + helpful_count) * HELPFUL_WEIGHT
        results.sort(key=lambda r: r["_adjusted"])

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
