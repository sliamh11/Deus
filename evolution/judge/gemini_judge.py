"""
Gemini-based judge for the Deus Evolution loop.

Two roles:
1. DeepEvalBaseLLM — plugs into DeepEval metrics (GEval, AnswerRelevancy, etc.)
   as a drop-in replacement for the blocked ClaudeProxyJudge.
2. Standalone runtime evaluator — scores production interactions via evaluate().

Uses the same google-genai client and API key as memory_indexer.py.
"""
import json
import os
from typing import Any, Optional, Tuple

from deepeval.models import DeepEvalBaseLLM

from ..config import GEN_MODELS, JUDGE_MODEL, load_api_key
from .base import BaseJudge, JudgeResult
from .criteria import RUBRIC, compose_score

_client = None


def _get_client():
    global _client
    if _client is None:
        from google import genai
        _client = genai.Client(api_key=load_api_key())
    return _client


def _call_gemini(prompt: str, model: str = JUDGE_MODEL) -> str:
    """Call Gemini with model fallback chain."""
    client = _get_client()
    models_to_try = [model] + [m for m in GEN_MODELS if m != model]
    last_exc = None
    for m in models_to_try:
        try:
            resp = client.models.generate_content(model=m, contents=prompt)
            return resp.text
        except Exception as exc:
            last_exc = exc
            exc_str = str(exc)
            if any(s in exc_str for s in ("429", "quota", "503", "unavailable", "UNAVAILABLE")):
                continue
            raise
    raise RuntimeError(f"All Gemini models failed. Last: {last_exc}")


async def _call_gemini_async(prompt: str, model: str = JUDGE_MODEL) -> str:
    """Async Gemini call — runs sync in thread pool to avoid blocking."""
    import asyncio
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _call_gemini(prompt, model))


# ── DeepEval integration ───────────────────────────────────────────────────────

class GeminiJudge(DeepEvalBaseLLM):
    """
    Gemini judge that plugs into DeepEval as the LLM backend.
    Pass model=GeminiJudge() to any GEval / AnswerRelevancy / etc. metric.
    """

    def __init__(self, model: str = JUDGE_MODEL):
        self.model = model

    def load_model(self):
        return _get_client()

    def generate(self, prompt: str, schema: Optional[Any] = None) -> Tuple[str, float]:
        if schema is not None:
            prompt = f"{prompt}\n\nRespond with valid JSON matching this schema: {schema}"
        return _call_gemini(prompt, self.model), 0.0

    async def a_generate(self, prompt: str, schema: Optional[Any] = None) -> Tuple[str, float]:
        if schema is not None:
            prompt = f"{prompt}\n\nRespond with valid JSON matching this schema: {schema}"
        return await _call_gemini_async(prompt, self.model), 0.0

    def get_model_name(self) -> str:
        return f"gemini:{self.model}"


# ── Standalone runtime evaluator ───────────────────────────────────────────────

class GeminiRuntimeJudge(BaseJudge):
    """
    Evaluates production interactions using the structured RUBRIC.
    Returns a JudgeResult with per-dimension scores and a composite score.
    """

    def __init__(self, model: str = JUDGE_MODEL):
        self.model = model

    def evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
    ) -> JudgeResult:
        eval_prompt = _build_eval_prompt(prompt, response, tools_used, context)
        raw = _call_gemini(eval_prompt, self.model)
        return _parse_result(raw)

    async def a_evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
    ) -> JudgeResult:
        eval_prompt = _build_eval_prompt(prompt, response, tools_used, context)
        raw = await _call_gemini_async(eval_prompt, self.model)
        return _parse_result(raw)


def _build_eval_prompt(
    prompt: str,
    response: str,
    tools_used: Optional[list[str]],
    context: Optional[str],
) -> str:
    parts = [RUBRIC, "\n## Interaction to evaluate\n"]
    if context:
        parts.append(f"**Context:** {context}\n")
    parts.append(f"**User prompt:**\n{prompt}\n")
    if tools_used:
        parts.append(f"**Tools used:** {', '.join(tools_used)}\n")
    parts.append(f"**Agent response:**\n{response}\n")
    return "\n".join(parts)


def _parse_result(raw: str) -> JudgeResult:
    # Strip markdown fences if present
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
        dims = {
            "quality": float(data.get("quality", 0.5)),
            "safety": float(data.get("safety", 1.0)),
            "tool_use": float(data.get("tool_use", 1.0)),
            "personalization": float(data.get("personalization", 0.5)),
        }
        return JudgeResult(
            score=compose_score(dims),
            rationale=data.get("rationale", ""),
            raw_response=raw,
            **dims,
        )
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        # Fallback: partial parse failure → neutral score
        import sys
        print(
            f"[judge] Parse error: {exc.__class__.__name__}: {exc} | raw={raw[:200]}",
            file=sys.stderr,
        )
        return JudgeResult(
            score=0.5,
            quality=0.5,
            safety=1.0,
            tool_use=1.0,
            personalization=0.5,
            rationale="Parse error — neutral score assigned",
            raw_response=raw,
        )


def make_deepeval_judge(model: str = JUDGE_MODEL) -> GeminiJudge:
    """Return a GeminiJudge instance for use with DeepEval metrics."""
    return GeminiJudge(model=model)


def make_runtime_judge(model: str = JUDGE_MODEL) -> GeminiRuntimeJudge:
    """Return a GeminiRuntimeJudge for scoring production interactions."""
    return GeminiRuntimeJudge(model=model)
