"""
Reflexion generator: produces a concise "lesson learned" from a low-scoring interaction.

The lesson is stored in the reflections table and retrieved for similar future queries,
improving agent behavior without any model weight updates.
"""
import json
from typing import Optional

from ..config import JUDGE_MODEL
from ..generative import generate

_REFLECTION_PROMPT = """
You are analyzing an AI assistant interaction that received a low quality score.
Generate a concise, actionable lesson that the assistant should remember for similar
future situations.

## Interaction

**User prompt:**
{prompt}

**Assistant response:**
{response}

**Tools used:** {tools}

**Quality score:** {score:.2f} / 1.0
**Score breakdown:** {dims}
**Judge rationale:** {rationale}

## Instructions

Write a reflection in exactly this format:
- **What went wrong:** (1 sentence — be specific)
- **Next time:** (1-2 sentences — concrete action to take differently)
- **Category:** one of: tool_use | reasoning | style | safety

Keep the whole reflection under 100 words. Be concrete and actionable.
Focus only on what is fixable by the agent (not API errors or timeouts).
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
        prompt=prompt[:1000],
        response=(response or "")[:1000],
        tools=", ".join(tools_used or []) or "none",
        score=score,
        dims=json.dumps(dims or {}),
        rationale=rationale or "no rationale provided",
    )

    text = generate(formatted, model=model)
    category = _extract_category(text)
    return text, category


_POSITIVE_PROMPT = """
You are analyzing an AI assistant interaction that received a HIGH quality score.
Extract the key pattern that made this response excellent, so it can be replicated.

## Interaction

**User prompt:**
{prompt}

**Assistant response:**
{response}

**Tools used:** {tools}

**Quality score:** {score:.2f} / 1.0
**Score breakdown:** {dims}
**Judge rationale:** {rationale}

## Instructions

Write a reflection in exactly this format:
- **What worked:** (1 sentence — be specific about the technique/approach)
- **Pattern to replicate:** (1-2 sentences — generalizable principle)
- **Category:** one of: tool_use | reasoning | style | positive_pattern

Keep the whole reflection under 100 words. Focus on what is replicable in future interactions.
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
        prompt=prompt[:1000],
        response=(response or "")[:1000],
        tools=", ".join(tools_used or []) or "none",
        score=score,
        dims=json.dumps(dims or {}),
        rationale=rationale or "no rationale provided",
    )

    text = generate(formatted, model=model)
    category = _extract_positive_category(text)
    return text, category


def _extract_category(text: str) -> str:
    lower = text.lower()
    for cat in ("tool_use", "safety", "reasoning", "style"):
        if cat in lower:
            return cat
    return "reasoning"


def _extract_positive_category(text: str) -> str:
    lower = text.lower()
    for cat in ("tool_use", "reasoning", "style"):
        if cat in lower:
            return cat
    return "positive_pattern"
