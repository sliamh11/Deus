"""
OpenAI-based judge for the Deus Evolution loop (opt-in — see providers/openai.py).

Standalone runtime evaluator — scores production interactions via evaluate().
Uses stdlib urllib for HTTP against OpenAI's /v1/chat/completions endpoint
(no new dependency — mirrors evolution/judge/llama_cpp_judge.py's mechanics),
with gemini_judge.py's more defensive JSON parsing since a newly-released
model's structured-output reliability against this schema is unverified.
"""
import asyncio
import json
import re
import time
import urllib.request
import urllib.error
from typing import Optional

from ..config import (
    JUDGE_MAX_PERSONA_CHARS,
    JUDGE_MAX_PROMPT_CHARS,
    JUDGE_MAX_RESPONSE_CHARS,
    JUDGE_RETRY_COUNT,
    OPENAI_BASE_URL,
    OPENAI_JUDGE_MODEL,
    load_openai_api_key,
)
from .base import BaseJudge, JudgeResult
from .criteria import RUBRIC, compose_score, _normalize_dim

_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "judge_result",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "safe": {"type": "boolean"},
                "quality_level": {"type": "integer", "minimum": 1, "maximum": 5},
                "recalled_preference": {"type": "boolean"},
                "format_matched": {"type": "boolean"},
                "tone_matched": {"type": "boolean"},
                "execution_quality": {"type": "integer", "minimum": 1, "maximum": 5},
                "rationale": {"type": "string"},
            },
            "required": [
                "safe", "quality_level", "recalled_preference", "format_matched",
                "tone_matched", "execution_quality", "rationale",
            ],
        },
    },
}


def _openai_url(path: str) -> str:
    return f"{OPENAI_BASE_URL.rstrip('/')}{path}"


def is_openai_available() -> bool:
    """Key presence only — no network ping (avoids burning a live API call
    just to check availability)."""
    try:
        load_openai_api_key()
        return True
    except RuntimeError:
        return False


# HTTP statuses worth retrying (transient) — everything else (401/403/404/400)
# means "this will fail again" and should raise immediately with a clear message.
_RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


def _call_openai(prompt: str, model: str = OPENAI_JUDGE_MODEL) -> str:
    """Synchronous OpenAI chat-completion call.

    Unlike llama_cpp_judge.py's local-server call, this hits a real internet
    API that can rate-limit (429) or have transient outages (5xx) — retries
    those up to JUDGE_RETRY_COUNT times with backoff. Auth/model errors
    (401/403/404/400) raise immediately since retrying won't fix them.
    """
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "response_format": _RESPONSE_SCHEMA,
    }).encode()
    key = load_openai_api_key()

    last_exc: Optional[RuntimeError] = None
    for attempt in range(JUDGE_RETRY_COUNT + 1):
        req = urllib.request.Request(
            _openai_url("/chat/completions"),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read().decode())
            choices = data.get("choices") or []
            return choices[0].get("message", {}).get("content") or "" if choices else ""
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            last_exc = RuntimeError(f"OpenAI API error {exc.code} for model {model}: {detail}")
            if exc.code not in _RETRYABLE_HTTP_CODES or attempt == JUDGE_RETRY_COUNT:
                raise last_exc from exc
        except urllib.error.URLError as exc:
            last_exc = RuntimeError(f"Cannot reach OpenAI API at {OPENAI_BASE_URL}: {exc.reason}")
            if attempt == JUDGE_RETRY_COUNT:
                raise last_exc from exc
        time.sleep(2 ** attempt)
    raise last_exc  # pragma: no cover — unreachable, loop always returns or raises


async def _call_openai_async(prompt: str, model: str = OPENAI_JUDGE_MODEL) -> str:
    """Async OpenAI call — runs sync in thread pool to avoid blocking the event loop."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: _call_openai(prompt, model))


def _cap_context_and_profile(
    context: Optional[str], user_profile: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Defense-in-depth truncation before either value reaches OpenAI's API.

    `user_profile` is already capped at JUDGE_MAX_PERSONA_CHARS upstream
    (evolution/persona.py, before it ever reaches evaluate()); this re-caps
    it anyway rather than trusting every future caller to do so. `context`
    has no upstream cap (BaseJudge's interface allows any caller to pass an
    arbitrarily large string, same as gemini_judge.py) — capped here to
    JUDGE_MAX_PROMPT_CHARS, the same bound already applied to `prompt`, as
    defense-in-depth for this newly-added, unvalidated provider specifically.
    """
    if context:
        context = context[:JUDGE_MAX_PROMPT_CHARS]
    if user_profile:
        user_profile = user_profile[:JUDGE_MAX_PERSONA_CHARS]
    return context, user_profile


# ── Runtime evaluator ─────────────────────────────────────────────────────────

class OpenAIRuntimeJudge(BaseJudge):
    """
    Evaluates production interactions using the structured RUBRIC via an
    OpenAI-hosted model (e.g. GPT-5.6 Luna/Terra/Sol).
    Returns a JudgeResult with per-dimension scores and a composite score.
    """

    def __init__(self, model: str = OPENAI_JUDGE_MODEL):
        self.model = model

    def evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
        user_profile: Optional[str] = None,
    ) -> JudgeResult:
        prompt = prompt[:JUDGE_MAX_PROMPT_CHARS]
        response = (response or "")[:JUDGE_MAX_RESPONSE_CHARS]
        context, user_profile = _cap_context_and_profile(context, user_profile)
        eval_prompt = _build_eval_prompt(prompt, response, tools_used, context, user_profile)
        raw = _call_openai(eval_prompt, self.model)
        result = _parse_result(raw)
        if result.is_parse_error:
            for _ in range(JUDGE_RETRY_COUNT):
                raw = _call_openai(
                    _build_eval_prompt(prompt, response, tools_used, context, user_profile, strict_json=True),
                    self.model,
                )
                result = _parse_result(raw)
                if not result.is_parse_error:
                    break
        return result

    async def a_evaluate(
        self,
        prompt: str,
        response: str,
        tools_used: Optional[list[str]] = None,
        context: Optional[str] = None,
        user_profile: Optional[str] = None,
    ) -> JudgeResult:
        prompt = prompt[:JUDGE_MAX_PROMPT_CHARS]
        response = (response or "")[:JUDGE_MAX_RESPONSE_CHARS]
        context, user_profile = _cap_context_and_profile(context, user_profile)
        eval_prompt = _build_eval_prompt(prompt, response, tools_used, context, user_profile)
        raw = await _call_openai_async(eval_prompt, self.model)
        result = _parse_result(raw)
        if result.is_parse_error:
            for _ in range(JUDGE_RETRY_COUNT):
                raw = await _call_openai_async(
                    _build_eval_prompt(prompt, response, tools_used, context, user_profile, strict_json=True),
                    self.model,
                )
                result = _parse_result(raw)
                if not result.is_parse_error:
                    break
        return result


def _build_eval_prompt(
    prompt: str,
    response: str,
    tools_used: Optional[list[str]],
    context: Optional[str],
    user_profile: Optional[str] = None,
    strict_json: bool = False,
) -> str:
    parts = [RUBRIC, "\n## Interaction to evaluate\n"]
    if context:
        parts.append(f"**Context:** {context}\n")
    if user_profile:
        parts.append(f"**Known user preferences (stored profile):**\n{user_profile}\n")
    parts.append(f"**User prompt:**\n{prompt}\n")
    if tools_used:
        parts.append(f"**Tools used:** {', '.join(tools_used)}\n")
    parts.append(f"**Agent response:**\n{response}\n")
    if strict_json:
        parts.append(
            "\nIMPORTANT: Respond with ONLY a valid JSON object. "
            "No markdown fences, no explanation, just the raw JSON.\n"
        )
    return "\n".join(parts)


_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}")


def _parse_result(raw: str) -> JudgeResult:
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
        import sys
        print(
            f"[judge] Parse error: no JSON found | raw={raw[:200]}",
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
    except (KeyError, ValueError) as exc:
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
            is_parse_error=True,
        )


def make_runtime_judge(model: str = OPENAI_JUDGE_MODEL) -> OpenAIRuntimeJudge:
    """Return an OpenAIRuntimeJudge for scoring production interactions."""
    return OpenAIRuntimeJudge(model=model)
