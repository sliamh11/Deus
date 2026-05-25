"""
Reflexion generator: produces a concise "lesson learned" from a low-scoring interaction.

The lesson is stored in the reflections table and retrieved for similar future queries,
improving agent behavior without any model weight updates.
"""
import json
import re
from typing import Optional

from ..config import JUDGE_MODEL
from ..generative import generate

_REFLECTION_PROMPT = """Analyze this low-scoring AI interaction and extract an actionable lesson.

User: {prompt}
Assistant: {response}
Tools: {tools}
Score: {score:.2f}/1.0 | Breakdown: {dims} | Rationale: {rationale}

Style issues to look for: naming conventions (camelCase vs snake_case), import ordering, \
library/framework preferences, function structure (flat vs nested, early returns), \
comment verbosity, error handling patterns, type annotation density, response formatting.

Reply in this exact format (under 100 words, agent-fixable issues only):
- What went wrong: (1 sentence, specific)
- Next time: (1-2 sentences, concrete action)
- Category: tool_use | reasoning | style | safety
  (Use "style" for: naming, formatting, library choice, code structure, response tone)
"""


def generate_reflection(
    prompt: str,
    response: str,
    score: float,
    dims: Optional[dict] = None,
    rationale: str = "",
    tools_used: Optional[list[str]] = None,
    model: str = JUDGE_MODEL,
) -> tuple[str, str]:
    """
    Generate a reflection for a low-scoring interaction.
    Returns (content, category).
    """
    formatted = _REFLECTION_PROMPT.format(
        prompt=prompt[:1500],
        response=(response or "")[:1500],
        tools=", ".join(tools_used or []) or "none",
        score=score,
        dims=json.dumps(dims or {}),
        rationale=rationale or "no rationale provided",
    )

    text = generate(formatted, model=model)
    category = _extract_category(text)
    return text, category


_POSITIVE_PROMPT = """Analyze this high-scoring AI interaction and extract the replicable pattern.

User: {prompt}
Assistant: {response}
Tools: {tools}
Score: {score:.2f}/1.0 | Breakdown: {dims} | Rationale: {rationale}

Style patterns to look for: naming conventions, code structure preferences, \
library/framework choices, response formatting and tone, comment style, \
error handling approach, type usage patterns.

Reply in this exact format (under 100 words, focus on replicable patterns):
- What worked: (1 sentence, specific technique/approach)
- Pattern to replicate: (1-2 sentences, generalizable principle)
- Category: tool_use | reasoning | style | positive_pattern
  (Use "style" for: naming, formatting, library choice, code structure, response tone)
"""


def generate_positive_reflection(
    prompt: str,
    response: str,
    score: float,
    dims: Optional[dict] = None,
    rationale: str = "",
    tools_used: Optional[list[str]] = None,
    model: str = JUDGE_MODEL,
) -> tuple[str, str]:
    """
    Generate a positive pattern reflection for a high-scoring interaction.
    Returns (content, category).
    """
    formatted = _POSITIVE_PROMPT.format(
        prompt=prompt[:1500],
        response=(response or "")[:1500],
        tools=", ".join(tools_used or []) or "none",
        score=score,
        dims=json.dumps(dims or {}),
        rationale=rationale or "no rationale provided",
    )

    text = generate(formatted, model=model)
    category = _extract_positive_category(text)
    return text, category


_CATEGORY_RE = re.compile(r'-\s*Category:\s*(\w+)', re.IGNORECASE)
_VALID_CATEGORIES = frozenset({"tool_use", "safety", "reasoning", "style"})
_VALID_POSITIVE_CATEGORIES = frozenset({"tool_use", "reasoning", "style", "positive_pattern"})


def _extract_category(text: str) -> str:
    m = _CATEGORY_RE.search(text)
    if m and m.group(1).lower() in _VALID_CATEGORIES:
        return m.group(1).lower()
    return "reasoning"


def _extract_positive_category(text: str) -> str:
    m = _CATEGORY_RE.search(text)
    if m and m.group(1).lower() in _VALID_POSITIVE_CATEGORIES:
        return m.group(1).lower()
    return "positive_pattern"
