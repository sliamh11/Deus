"""
Reflexion generator: produces a concise "lesson learned" from a low-scoring interaction.

The lesson is stored in the reflections table and retrieved for similar future queries,
improving agent behavior without any model weight updates.
"""
import json
from typing import Optional

from ..config import GEN_MODELS, JUDGE_MODEL, load_api_key

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
    from google import genai

    client = genai.Client(api_key=load_api_key())
    formatted = _REFLECTION_PROMPT.format(
        prompt=prompt[:1000],
        response=(response or "")[:1000],
        tools=", ".join(tools_used or []) or "none",
        score=score,
        dims=json.dumps(dims or {}),
        rationale=rationale or "no rationale provided",
    )

    models_to_try = [model] + [m for m in GEN_MODELS if m != model]
    last_exc = None
    text = ""
    for m in models_to_try:
        try:
            resp = client.models.generate_content(model=m, contents=formatted)
            text = resp.text.strip()
            break
        except Exception as exc:
            last_exc = exc
            if "429" in str(exc) or "quota" in str(exc).lower():
                continue
            raise

    if not text:
        raise RuntimeError(f"All Gemini models failed generating reflection. Last: {last_exc}")

    category = _extract_category(text)
    return text, category


def _extract_category(text: str) -> str:
    lower = text.lower()
    for cat in ("tool_use", "safety", "reasoning", "style"):
        if cat in lower:
            return cat
    return "reasoning"
