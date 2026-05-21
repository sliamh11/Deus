"""
llama.cpp-based judge for the Deus Evolution loop.

Standalone runtime evaluator — scores production interactions via evaluate().
Uses stdlib urllib for HTTP (no new dependency — mirrors evolution/judge/ollama_judge.py).
Targets llama-server's OpenAI-compatible /v1/chat/completions endpoint.

Differences from ollama_judge.py:
- URL: {BASE_URL}/chat/completions instead of /api/generate
- Body shape: {"messages": [{"role": "user", "content": prompt}], "stream": false, "model": <optional>}
  vs Ollama's {"model": ..., "prompt": ..., "stream": false}
- Response parsing: data["choices"][0]["message"]["content"] vs data["response"]
- No _check_model_pulled equivalent: llama-server loads exactly one model at
  startup and uses whatever's loaded — there's no "model not pulled" failure mode.
"""
import asyncio
import json
import urllib.request
import urllib.error
from typing import Optional

from .base import BaseJudge, JudgeResult
from .criteria import RUBRIC, compose_score
from ..config import LLAMA_CPP_BASE_URL, LLAMA_CPP_JUDGE_MODEL


def _llama_cpp_url(path: str) -> str:
    return f"{LLAMA_CPP_BASE_URL.rstrip('/')}{path}"


def is_llama_cpp_available() -> bool:
    """Ping llama-server's /models endpoint; return True if reachable."""
    if not LLAMA_CPP_BASE_URL:
        return False
    try:
        req = urllib.request.Request(_llama_cpp_url("/models"))
        urllib.request.urlopen(req, timeout=2)
        return True
    except (urllib.error.URLError, OSError):
        return False


def _call_llama_cpp(prompt: str, model: str = LLAMA_CPP_JUDGE_MODEL) -> str:
    """Synchronous llama-server chat-completion call."""
    body_dict: dict = {
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    # Llama-server accepts requests with no "model" field (uses whatever's loaded);
    # only include it when the caller explicitly configured one.
    if model:
        body_dict["model"] = model
    body = json.dumps(body_dict).encode()
    req = urllib.request.Request(
        _llama_cpp_url("/chat/completions"),
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer placeholder",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=300) as resp:
        data = json.loads(resp.read().decode())
    choices = data.get("choices") or []
    if not choices:
        return ""
    return (choices[0].get("message", {}).get("content") or "")


async def _call_llama_cpp_async(prompt: str, model: str = LLAMA_CPP_JUDGE_MODEL) -> str:
    """Async llama-server call — runs sync in thread pool to avoid blocking the event loop."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _call_llama_cpp(prompt, model))


# ── Runtime evaluator ─────────────────────────────────────────────────────────

class LlamaCppRuntimeJudge(BaseJudge):
    """
    Evaluates production interactions using the structured RUBRIC.
    Returns a JudgeResult with per-dimension scores and a composite score.
    """

    def __init__(self, model: str = LLAMA_CPP_JUDGE_MODEL):
        self.model = model
        # No pre-flight model-pulled check: llama-server only loads one model.
        # If the server is unreachable or misconfigured, evaluate() will raise
        # loudly via urlopen at request time.

    def evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
    ) -> JudgeResult:
        eval_prompt = _build_eval_prompt(prompt, response, tools_used, context)
        raw = _call_llama_cpp(eval_prompt, self.model)
        return _parse_result(raw)

    async def a_evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
    ) -> JudgeResult:
        eval_prompt = _build_eval_prompt(prompt, response, tools_used, context)
        raw = await _call_llama_cpp_async(eval_prompt, self.model)
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
            is_parse_error=True,
        )


def make_runtime_judge(model: str = LLAMA_CPP_JUDGE_MODEL) -> LlamaCppRuntimeJudge:
    """Return a LlamaCppRuntimeJudge for scoring production interactions."""
    return LlamaCppRuntimeJudge(model=model)
