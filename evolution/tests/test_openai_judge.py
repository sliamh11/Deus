"""Unit tests for evolution/judge/openai_judge.py — runtime judge wrapper."""
import asyncio
import json
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

from evolution.judge.base import JudgeResult
from evolution.judge.openai_judge import (
    OpenAIRuntimeJudge,
    _call_openai,
    _cap_context_and_profile,
    _parse_result,
    is_openai_available,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _stub_urlopen_returning(body: dict):
    """Build a MagicMock that mimics urlopen's context-manager response."""
    response = MagicMock()
    response.read.return_value = json.dumps(body).encode()
    cm = MagicMock()
    cm.__enter__.return_value = response
    cm.__exit__.return_value = None
    return cm


def _chat_completion_envelope(content: str) -> dict:
    return {
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
        ]
    }


def _http_error(code: int, body: bytes = b'{"error": "boom"}') -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://api.openai.com/v1/chat/completions",
        code=code,
        msg="error",
        hdrs=None,
        fp=MagicMock(read=lambda: body),
    )


@pytest.fixture(autouse=True)
def _fake_key():
    with patch("evolution.judge.openai_judge.load_openai_api_key", return_value="sk-test-key"):
        yield


# ── _call_openai ─────────────────────────────────────────────────────────────


class TestCallOpenAI:
    def test_returns_assistant_content_from_envelope(self):
        envelope = _chat_completion_envelope('{"quality_level": 5}')
        with patch(
            "evolution.judge.openai_judge.urllib.request.urlopen",
            return_value=_stub_urlopen_returning(envelope),
        ):
            assert _call_openai("hello", model="gpt-5.6-luna") == '{"quality_level": 5}'

    def test_empty_choices_returns_empty_string(self):
        with patch(
            "evolution.judge.openai_judge.urllib.request.urlopen",
            return_value=_stub_urlopen_returning({"choices": []}),
        ):
            assert _call_openai("hello") == ""

    def test_request_shape(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["headers"] = dict(req.headers)
            captured["body"] = json.loads(req.data)
            return _stub_urlopen_returning(_chat_completion_envelope("ok"))

        with patch("evolution.judge.openai_judge.urllib.request.urlopen", side_effect=fake_urlopen):
            _call_openai("hello", model="gpt-5.6-luna")

        assert captured["headers"]["Authorization"] == "Bearer sk-test-key"
        assert captured["body"]["model"] == "gpt-5.6-luna"
        assert captured["body"]["messages"] == [{"role": "user", "content": "hello"}]
        assert captured["body"]["response_format"]["type"] == "json_schema"

    def test_non_retryable_http_error_raises_immediately_with_no_key_leak(self):
        with patch(
            "evolution.judge.openai_judge.urllib.request.urlopen",
            side_effect=_http_error(401, b'{"error": "invalid_api_key"}'),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                _call_openai("hello", model="gpt-5.6-luna")
        msg = str(exc_info.value)
        assert "401" in msg
        assert "sk-test-key" not in msg  # the bearer key must never appear in a raised error

    def test_retryable_http_error_retries_then_succeeds(self):
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _http_error(429, b'{"error": "rate_limited"}')
            return _stub_urlopen_returning(_chat_completion_envelope("ok"))

        with patch("evolution.judge.openai_judge.urllib.request.urlopen", side_effect=fake_urlopen), \
             patch("evolution.judge.openai_judge.time.sleep"):
            result = _call_openai("hello", model="gpt-5.6-luna")
        assert result == "ok"
        assert calls["n"] == 2

    def test_retryable_http_error_exhausts_retries_and_raises(self):
        with patch(
            "evolution.judge.openai_judge.urllib.request.urlopen",
            side_effect=_http_error(503, b'{"error": "unavailable"}'),
        ), patch("evolution.judge.openai_judge.time.sleep"):
            with pytest.raises(RuntimeError, match="503"):
                _call_openai("hello", model="gpt-5.6-luna")

    def test_url_error_raises_runtime_error(self):
        with patch(
            "evolution.judge.openai_judge.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ), patch("evolution.judge.openai_judge.time.sleep"):
            with pytest.raises(RuntimeError, match="Cannot reach OpenAI API"):
                _call_openai("hello", model="gpt-5.6-luna")


# ── _parse_result ────────────────────────────────────────────────────────────


class TestParseResult:
    def test_well_formed_json_returns_judge_result(self):
        raw = json.dumps({
            "safe": True,
            "quality_level": 4,
            "recalled_preference": True,
            "format_matched": False,
            "tone_matched": True,
            "execution_quality": 5,
            "rationale": "Looks good",
        })
        result = _parse_result(raw)
        assert isinstance(result, JudgeResult)
        assert not result.is_parse_error
        assert 0.0 <= result.score <= 1.0
        assert "Looks good" in (result.rationale or "")

    def test_strips_markdown_fences(self):
        raw = (
            '```json\n{"safe": true, "quality_level": 3, "recalled_preference": false, '
            '"format_matched": false, "tone_matched": false, "execution_quality": 3, '
            '"rationale": "ok"}\n```'
        )
        result = _parse_result(raw)
        assert not result.is_parse_error

    def test_invalid_json_returns_neutral_fallback(self):
        result = _parse_result("not json at all")
        assert result.is_parse_error
        assert result.score == 0.5
        assert "Parse error" in (result.rationale or "")


# ── OpenAIRuntimeJudge ───────────────────────────────────────────────────────


class TestOpenAIRuntimeJudge:
    def test_evaluate_round_trip(self):
        canned = json.dumps({
            "safe": True, "quality_level": 5, "recalled_preference": True,
            "format_matched": True, "tone_matched": True, "execution_quality": 5,
            "rationale": "Clear and correct",
        })
        with patch(
            "evolution.judge.openai_judge.urllib.request.urlopen",
            return_value=_stub_urlopen_returning(_chat_completion_envelope(canned)),
        ):
            judge = OpenAIRuntimeJudge(model="gpt-5.6-luna")
            result = judge.evaluate(prompt="What's 2+2?", response="4", tools_used=["calculator"])
        assert isinstance(result, JudgeResult)
        assert not result.is_parse_error

    def test_init_skips_preflight_check(self):
        judge = OpenAIRuntimeJudge(model="gpt-5.6-luna")
        assert judge.model == "gpt-5.6-luna"

    def test_a_evaluate_runs_in_executor(self):
        canned = json.dumps({
            "safe": True, "quality_level": 3, "recalled_preference": False,
            "format_matched": False, "tone_matched": False, "execution_quality": 3,
            "rationale": "ok",
        })
        with patch(
            "evolution.judge.openai_judge.urllib.request.urlopen",
            return_value=_stub_urlopen_returning(_chat_completion_envelope(canned)),
        ):
            judge = OpenAIRuntimeJudge(model="gpt-5.6-luna")
            result = asyncio.run(judge.a_evaluate(prompt="hi", response="hi"))
        assert not result.is_parse_error


# ── _cap_context_and_profile ─────────────────────────────────────────────────


class TestCapContextAndProfile:
    def test_none_values_pass_through(self):
        assert _cap_context_and_profile(None, None) == (None, None)

    def test_context_truncated_to_judge_max_prompt_chars(self):
        from evolution.config import JUDGE_MAX_PROMPT_CHARS
        huge = "x" * (JUDGE_MAX_PROMPT_CHARS + 500)
        capped, _ = _cap_context_and_profile(huge, None)
        assert len(capped) == JUDGE_MAX_PROMPT_CHARS

    def test_user_profile_truncated_to_judge_max_persona_chars(self):
        from evolution.config import JUDGE_MAX_PERSONA_CHARS
        huge = "y" * (JUDGE_MAX_PERSONA_CHARS + 500)
        _, capped = _cap_context_and_profile(None, huge)
        assert len(capped) == JUDGE_MAX_PERSONA_CHARS

    def test_short_values_unaffected(self):
        assert _cap_context_and_profile("short ctx", "short profile") == ("short ctx", "short profile")


# ── is_openai_available ──────────────────────────────────────────────────────


class TestIsOpenAIAvailable:
    def test_returns_true_when_key_present(self):
        assert is_openai_available() is True

    def test_returns_false_when_key_absent(self):
        with patch(
            "evolution.judge.openai_judge.load_openai_api_key",
            side_effect=RuntimeError("OPENAI_API_KEY not found"),
        ):
            assert is_openai_available() is False
