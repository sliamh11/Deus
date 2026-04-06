"""
Ollama-based judge for the Deus Evolution loop.

Two roles:
1. DeepEvalBaseLLM — plugs into DeepEval metrics (GEval, AnswerRelevancy, etc.)
   as a local alternative to GeminiJudge.
2. Standalone runtime evaluator — scores production interactions via evaluate().

Uses stdlib urllib for HTTP — no new dependencies required.
"""
import asyncio
import json
import os
import urllib.request
import urllib.error
from typing import Any, Optional, Tuple

from deepeval.models import DeepEvalBaseLLM

from .base import BaseJudge, JudgeResult
from .criteria import RUBRIC, compose_score
from ..config import OLLAMA_HOST, OLLAMA_MODEL


def _ollama_url(path: str) -> str:
    return f"{OLLAMA_HOST.rstrip('/')}{path}"


def is_ollama_available() -> bool:
    """Ping Ollama server; return True if reachable."""
    try:
        req = urllib.request.Request(_ollama_url("/api/tags"))
        urllib.request.urlopen(req, timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        return False


def _check_model_pulled(model: str) -> None:
    """Verify the model exists locally. Raises RuntimeError if not."""
    try:
        body = json.dumps({"name": model}).encode()
        req = urllib.request.Request(
            _ollama_url("/api/show"),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(
                f"Ollama model '{model}' not found. Run: ollama pull {model}"
            ) from exc
        raise
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(
            f"Cannot reach Ollama at {OLLAMA_HOST}. Is it running?"
        ) from exc


def _call_ollama(prompt: str, model: str = OLLAMA_MODEL) -> str:
    """Synchronous Ollama generate call."""
    # /no_think prevents qwen3.5 thinking mode from returning empty — only needed for qwen
    full_prompt = f"{prompt}\n/no_think" if "qwen" in model.lower() else prompt
    body = json.dumps({
        "model": model,
        "prompt": full_prompt,
        "stream": False,
    }).encode()
    req = urllib.request.Request(
        _ollama_url("/api/generate"),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode())
    return data.get("response", "")


async def _call_ollama_async(prompt: str, model: str = OLLAMA_MODEL) -> str:
    """Async Ollama call — runs sync in thread pool to avoid blocking."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _call_ollama(prompt, model))


# ── DeepEval integration ───────────────────────────────────────────────────────

class OllamaJudge(DeepEvalBaseLLM):
    """
    Ollama judge that plugs into DeepEval as the LLM backend.
    Pass model=OllamaJudge() to any GEval / AnswerRelevancy / etc. metric.
    """

    def __init__(self, model: str = OLLAMA_MODEL):
        self.model = model
        _check_model_pulled(self.model)

    def load_model(self):
        return self.model

    def generate(self, prompt: str, schema: Optional[Any] = None) -> Tuple[str, float]:
        if schema is not None:
            prompt = f"{prompt}\n\nRespond with valid JSON matching this schema: {schema}"
        return _call_ollama(prompt, self.model), 0.0

    async def a_generate(self, prompt: str, schema: Optional[Any] = None) -> Tuple[str, float]:
        if schema is not None:
            prompt = f"{prompt}\n\nRespond with valid JSON matching this schema: {schema}"
        return await _call_ollama_async(prompt, self.model), 0.0

    def get_model_name(self) -> str:
        return f"ollama:{self.model}"


# ── Standalone runtime evaluator ───────────────────────────────────────────────

class OllamaRuntimeJudge(BaseJudge):
    """
    Evaluates production interactions using the structured RUBRIC.
    Returns a JudgeResult with per-dimension scores and a composite score.
    """

    def __init__(self, model: str = OLLAMA_MODEL):
        self.model = model
        _check_model_pulled(self.model)

    def evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
    ) -> JudgeResult:
        eval_prompt = _build_eval_prompt(prompt, response, tools_used, context)
        raw = _call_ollama(eval_prompt, self.model)
        return _parse_result(raw)

    async def a_evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
    ) -> JudgeResult:
        eval_prompt = _build_eval_prompt(prompt, response, tools_used, context)
        raw = await _call_ollama_async(eval_prompt, self.model)
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
    except (json.JSONDecodeError, KeyError, ValueError):
        # Fallback: partial parse failure -> neutral score
        return JudgeResult(
            score=0.5,
            quality=0.5,
            safety=1.0,
            tool_use=1.0,
            personalization=0.5,
            rationale="Parse error — neutral score assigned",
            raw_response=raw,
        )


def make_deepeval_judge(model: str = OLLAMA_MODEL) -> OllamaJudge:
    """Return an OllamaJudge instance for use with DeepEval metrics."""
    return OllamaJudge(model=model)


def make_runtime_judge(model: str = OLLAMA_MODEL) -> OllamaRuntimeJudge:
    """Return an OllamaRuntimeJudge for scoring production interactions."""
    return OllamaRuntimeJudge(model=model)
