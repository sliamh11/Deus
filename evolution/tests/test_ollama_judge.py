"""Unit tests for evolution/judge/ollama_judge.py — family-specific thinking
suppression in the Ollama judge request body (LIA-186)."""
import json
from unittest.mock import patch, MagicMock

from evolution.judge.ollama_judge import _call_ollama


def _capturing_urlopen(captured: dict):
    """urlopen replacement that records the decoded request body and returns a
    minimal valid (empty-object) judge response."""

    def _fake(req, *args, **kwargs):
        captured["body"] = json.loads(req.data.decode())
        resp = MagicMock()
        resp.read.return_value = json.dumps({"response": "{}"}).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    return _fake


def test_gemma4_suppresses_thinking_via_body_key():
    # Gemma4: "think": false at the TOP level (not under options), no prompt suffix.
    captured = {}
    with patch("urllib.request.urlopen", side_effect=_capturing_urlopen(captured)):
        _call_ollama("rate this response", model="gemma4:e4b")
    body = captured["body"]
    assert body.get("think") is False
    assert "think" not in body.get("options", {})
    assert "/no_think" not in body["prompt"]


def test_qwen_suppresses_thinking_via_prompt_suffix():
    # Qwen keeps the /no_think prompt-suffix mechanism and no body-level key.
    captured = {}
    with patch("urllib.request.urlopen", side_effect=_capturing_urlopen(captured)):
        _call_ollama("rate this response", model="qwen3:4b")
    body = captured["body"]
    assert body["prompt"].endswith("/no_think")
    assert "think" not in body


def test_non_thinking_model_sends_no_controls():
    # A model from neither family gets no thinking controls at all.
    captured = {}
    with patch("urllib.request.urlopen", side_effect=_capturing_urlopen(captured)):
        _call_ollama("rate this response", model="llama3.1:8b")
    body = captured["body"]
    assert "think" not in body
    assert "/no_think" not in body["prompt"]


def test_earlier_gemma_variants_excluded_from_think_key():
    # Boundary canary: the body-key suppression is scoped to gemma4 only. Earlier
    # Gemma variants must NOT receive "think" — guards against widening the
    # predicate to a bare "gemma" substring (which would match gemma2/gemma3).
    for model in ("gemma2:9b", "gemma3:27b"):
        captured = {}
        with patch("urllib.request.urlopen", side_effect=_capturing_urlopen(captured)):
            _call_ollama("rate this response", model=model)
        assert "think" not in captured["body"], f"{model} should not get think key"
