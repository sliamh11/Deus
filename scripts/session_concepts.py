#!/usr/bin/env python3
"""Session-scoped keyword accumulation for short-prompt recall improvement."""
from __future__ import annotations

import json
import re
import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from memory_tree import FTS_STOP_WORDS  # noqa: E402

_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")
_SNAKE_RE = re.compile(r"[a-z]+_[a-z]")
_CAMEL_RE = re.compile(r"[a-z][A-Z]")

MAX_CONCEPTS = 20
CONCEPTS_DIR = Path(tempfile.gettempdir())

BASE_WEIGHT = 1.0
CAPITALIZED_BOOST = 0.5
IDENTIFIER_BOOST = 0.5
LONG_TERM_BOOST = 0.3
SHORT_TERM_PENALTY = 0.3
LONG_TERM_MIN_LEN = 8
SHORT_TERM_LEN = 3


def extract_terms(text: str) -> list[tuple[str, float]]:
    """Extract weighted (term, score) pairs from a single prompt."""
    raw_tokens = _TOKEN_RE.findall(text)
    seen: dict[str, float] = {}

    for tok in raw_tokens:
        lower = tok.lower()
        if lower in FTS_STOP_WORDS:
            continue

        weight = BASE_WEIGHT

        if tok[0].isupper() or tok.isupper():
            weight += CAPITALIZED_BOOST
        if _SNAKE_RE.search(tok) or _CAMEL_RE.search(tok):
            weight += IDENTIFIER_BOOST
        if len(tok) >= LONG_TERM_MIN_LEN:
            weight += LONG_TERM_BOOST
        elif len(tok) == SHORT_TERM_LEN:
            weight -= SHORT_TERM_PENALTY

        key = lower
        if key in seen:
            seen[key] = max(seen[key], weight)
        else:
            seen[key] = weight

    return sorted(seen.items(), key=lambda x: x[1], reverse=True)


_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")


def _concepts_path(session_id: str) -> Path:
    safe_id = _SAFE_ID_RE.sub("", session_id) or "unknown"
    return CONCEPTS_DIR / f".deus-concepts-{safe_id}.json"


def load_concepts(session_id: str) -> dict[str, float]:
    """Load existing concepts. Returns empty dict on missing/malformed file."""
    p = _concepts_path(session_id)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        concepts = data.get("concepts", {})
        return concepts if isinstance(concepts, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def update_concepts(
    session_id: str, new_terms: list[tuple[str, float]]
) -> list[str]:
    """Merge new terms, evict to cap, write back. Returns top concept keywords."""
    concepts = load_concepts(session_id)

    for term, weight in new_terms:
        concepts[term] = concepts.get(term, 0.0) + weight

    if len(concepts) > MAX_CONCEPTS:
        ranked = sorted(concepts.items(), key=lambda x: x[1], reverse=True)
        concepts = dict(ranked[:MAX_CONCEPTS])

    p = _concepts_path(session_id)
    try:
        p.write_text(
            json.dumps({"concepts": concepts}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass

    ranked = sorted(concepts.items(), key=lambda x: x[1], reverse=True)
    return [term for term, _ in ranked]
