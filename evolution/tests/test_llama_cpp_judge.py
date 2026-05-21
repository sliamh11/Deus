"""Unit tests for evolution/judge/llama_cpp_judge.py — runtime judge wrapper."""
import io
import json
from unittest.mock import patch, MagicMock

import pytest

from evolution.judge.base import JudgeResult
from evolution.judge.llama_cpp_judge import (
    LlamaCppRuntimeJudge,
    _call_llama_cpp,
    _parse_result,
    is_llama_cpp_available,
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
    """Build a minimal OpenAI-compatible chat completion JSON envelope."""
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ]
    }


# ── _call_llama_cpp ──────────────────────────────────────────────────────────


class TestCallLlamaCpp:
    def test_returns_assistant_content_from_envelope(self):
        envelope = _chat_completion_envelope('{"quality": 0.9}')
        with patch(
            "evolution.judge.llama_cpp_judge.urllib.request.urlopen",
            return_value=_stub_urlopen_returning(envelope),
        ):
            result = _call_llama_cpp("hello", model="test-model")
            assert result == '{"quality": 0.9}'

    def test_empty_choices_returns_empty_string(self):
        envelope = {"choices": []}
        with patch(
            "evolution.judge.llama_cpp_judge.urllib.request.urlopen",
            return_value=_stub_urlopen_returning(envelope),
        ):
            assert _call_llama_cpp("hello") == ""

    def test_omits_model_field_when_empty(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return _stub_urlopen_returning(_chat_completion_envelope("ok"))

        with patch(
            "evolution.judge.llama_cpp_judge.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            _call_llama_cpp("hello", model="")

        body = json.loads(captured["body"])
        assert "model" not in body
        assert body["messages"] == [{"role": "user", "content": "hello"}]
        assert body["stream"] is False

    def test_includes_model_field_when_set(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return _stub_urlopen_returning(_chat_completion_envelope("ok"))

        with patch(
            "evolution.judge.llama_cpp_judge.urllib.request.urlopen",
            side_effect=fake_urlopen,
        ):
            _call_llama_cpp("hello", model="gemma3:1b")

        body = json.loads(captured["body"])
        assert body["model"] == "gemma3:1b"


# ── _parse_result ────────────────────────────────────────────────────────────


class TestParseResult:
    def test_well_formed_json_returns_judge_result(self):
        raw = json.dumps({
            "quality": 0.8,
            "safety": 1.0,
            "tool_use": 0.9,
            "personalization": 0.7,
            "rationale": "Looks good",
        })
        result = _parse_result(raw)
        assert isinstance(result, JudgeResult)
        assert result.quality == 0.8
        assert result.safety == 1.0
        assert result.tool_use == 0.9
        assert result.personalization == 0.7
        assert "Looks good" in (result.rationale or "")
        assert not result.is_parse_error
        # Composite score is a float in [0, 1]
        assert 0.0 <= result.score <= 1.0

    def test_strips_markdown_fences(self):
        raw = '```json\n{"quality": 0.5, "safety": 1.0, "tool_use": 1.0, "personalization": 0.5, "rationale": "ok"}\n```'
        result = _parse_result(raw)
        assert result.quality == 0.5
        assert not result.is_parse_error

    def test_invalid_json_returns_neutral_fallback(self):
        result = _parse_result("not json at all")
        assert result.is_parse_error
        assert result.score == 0.5
        assert "Parse error" in (result.rationale or "")


# ── LlamaCppRuntimeJudge ─────────────────────────────────────────────────────


class TestLlamaCppRuntimeJudge:
    def test_evaluate_round_trip(self):
        canned = json.dumps({
            "quality": 0.9,
            "safety": 1.0,
            "tool_use": 1.0,
            "personalization": 0.8,
            "rationale": "Clear and correct",
        })
        envelope = _chat_completion_envelope(canned)
        with patch(
            "evolution.judge.llama_cpp_judge.urllib.request.urlopen",
            return_value=_stub_urlopen_returning(envelope),
        ):
            judge = LlamaCppRuntimeJudge(model="test-model")
            result = judge.evaluate(
                prompt="What's 2+2?",
                response="4",
                tools_used=["calculator"],
            )
        assert isinstance(result, JudgeResult)
        assert result.quality == 0.9
        assert not result.is_parse_error

    def test_init_skips_preflight_check(self):
        # Unlike OllamaRuntimeJudge, __init__ must NOT call _check_model_pulled
        # or any network endpoint. If the server is down, evaluate() will raise
        # at request time, but construction must succeed.
        judge = LlamaCppRuntimeJudge(model="nonexistent-model")
        assert judge.model == "nonexistent-model"

    def test_a_evaluate_runs_in_executor(self):
        # Use asyncio.run rather than @pytest.mark.asyncio so this test does
        # not require the pytest-asyncio plugin (matches the rest of the
        # evolution test suite, which avoids that dependency).
        import asyncio
        canned = json.dumps({
            "quality": 0.6, "safety": 1.0, "tool_use": 1.0,
            "personalization": 0.5, "rationale": "ok",
        })
        envelope = _chat_completion_envelope(canned)
        with patch(
            "evolution.judge.llama_cpp_judge.urllib.request.urlopen",
            return_value=_stub_urlopen_returning(envelope),
        ):
            judge = LlamaCppRuntimeJudge(model="m")
            result = asyncio.run(judge.a_evaluate(prompt="hi", response="hi"))
        assert result.quality == 0.6


# ── is_llama_cpp_available ───────────────────────────────────────────────────


class TestIsLlamaCppAvailable:
    def test_returns_false_when_base_url_empty(self):
        with patch("evolution.judge.llama_cpp_judge.LLAMA_CPP_BASE_URL", ""):
            assert is_llama_cpp_available() is False

    def test_returns_false_on_connection_error(self):
        import urllib.error
        with patch(
            "evolution.judge.llama_cpp_judge.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            assert is_llama_cpp_available() is False

    def test_returns_true_when_reachable(self):
        with patch(
            "evolution.judge.llama_cpp_judge.urllib.request.urlopen",
            return_value=_stub_urlopen_returning({"data": []}),
        ):
            assert is_llama_cpp_available() is True
