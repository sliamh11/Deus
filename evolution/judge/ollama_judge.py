"""
Ollama-based judge for the Deus Evolution loop.

Standalone runtime evaluator — scores production interactions via evaluate().
Uses stdlib urllib for HTTP — no new dependencies required.
"""
import asyncio
import json
import os
import re
import urllib.request
import urllib.error
from typing import Optional

from .base import BaseJudge, JudgeResult
from .criteria import RUBRIC, compose_score, _normalize_dim
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
    # Suppress model "thinking" for structured output — otherwise the thinking
    # preamble slows the call and can corrupt the JSON-grammar response. The
    # mechanism is family-specific: Qwen uses the /no_think prompt suffix; Gemma4
    # (the default OLLAMA_MODEL) uses the "think": false request-body key. Prior
    # to this fix the default judge ran with thinking ON and no suppression.
    # Scoped to gemma4 specifically — the only Gemma we run — rather than all
    # "gemma*" so earlier variants aren't sent a key they may not support.
    full_prompt = f"{prompt}\n/no_think" if "qwen" in model.lower() else prompt
    payload = {
        "model": model,
        "prompt": full_prompt,
        "stream": False,
        "format": {
            "type": "object",
            "properties": {
                "safe": {"type": "boolean"},
                "quality_level": {"type": "integer", "minimum": 1, "maximum": 5},
                "recalled_preference": {"type": "boolean"},
                "format_matched": {"type": "boolean"},
                "tone_matched": {"type": "boolean"},
                "execution_quality": {"type": "integer", "minimum": 1, "maximum": 5},
                "rationale": {"type": "string"}
            },
            "required": ["safe", "quality_level", "recalled_preference", "format_matched", "tone_matched", "execution_quality", "rationale"]
        },
        "options": {
            "temperature": 0,
            "seed": 42,
        },
    }
    if "gemma4" in model.lower():
        payload["think"] = False
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        _ollama_url("/api/generate"),
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode())
    return data.get("response", "")


async def _call_ollama_async(prompt: str, model: str = OLLAMA_MODEL) -> str:
    """Async Ollama call — runs sync in thread pool to avoid blocking."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _call_ollama(prompt, model))


# ── Runtime evaluator ─────────────────────────────────────────────────────────

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


_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}")


def _parse_result(raw: str) -> JudgeResult:
    # Defensive fallback: constrained decoding guarantees valid JSON, but older
    # Ollama versions silently ignore the `format` field — keep parsing guards.
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    data = None
    try:
        candidate = json.loads(text)
        if isinstance(candidate, dict):
            data = candidate
    except json.JSONDecodeError:
        pass

    if data is None:
        match = _JSON_BLOCK_RE.search(text)
        if match:
            try:
                candidate = json.loads(match.group(0))
                if isinstance(candidate, dict):
                    data = candidate
            except json.JSONDecodeError:
                pass

    if data is None:
        return JudgeResult(
            score=0.5,
            quality=0.5,
            safety=1.0,
            tool_use=1.0,
            personalization=0.5,
            rationale="Parse error — neutral score assigned",
            raw_response=raw,
            is_parse_error=True,
        )

    try:
        quality = _normalize_dim("quality", data)
        safety = _normalize_dim("safety", data)
        tool_use = _normalize_dim("tool_use", data)
        personalization = _normalize_dim("personalization", data)
        dims = {
            "quality": quality,
            "safety": safety,
            "tool_use": tool_use,
            "personalization": personalization,
        }
        return JudgeResult(
            score=compose_score(dims),
            rationale=data.get("rationale", ""),
            raw_response=raw,
            **dims,
        )
    except (KeyError, ValueError):
        return JudgeResult(
            score=0.5,
            quality=0.5,
            safety=1.0,
            tool_use=1.0,
            personalization=0.5,
            rationale="Parse error — neutral score assigned",
            raw_response=raw,
            is_parse_error=True,
        )



def make_runtime_judge(model: str = OLLAMA_MODEL) -> OllamaRuntimeJudge:
    """Return an OllamaRuntimeJudge for scoring production interactions."""
    return OllamaRuntimeJudge(model=model)
