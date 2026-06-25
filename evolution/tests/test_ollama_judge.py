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


def test_qwen3_suppresses_thinking_via_both_mechanisms():
    # Qwen3 gets BOTH controls: the body-level "think": false key (the real
    # control — qwen3.5+ ignores the /no_think suffix and returns an empty
    # response under the strict format schema) AND the /no_think prompt suffix
    # (harmless belt-and-suspenders for qwen3.0 compat). Covers both the older
    # qwen3.0 (suffix originally validated in LIA-186) and qwen3.5 (the variant
    # the suffix alone left broken).
    for model in ("qwen3:4b", "qwen3.5:4b"):
        captured = {}
        with patch("urllib.request.urlopen", side_effect=_capturing_urlopen(captured)):
            _call_ollama("rate this response", model=model)
        body = captured["body"]
        assert body["prompt"].endswith("/no_think"), f"{model} missing suffix"
        assert body.get("think") is False, f"{model} missing think key"
        assert "think" not in body.get("options", {})


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


def test_earlier_qwen_variants_excluded_from_controls():
    # Boundary canary: both the think key and the /no_think suffix are scoped to
    # qwen3 only. Non-thinking qwen variants (qwen2.5, hypothetical qwen-embed)
    # must receive NEITHER — guards against widening the predicate to a bare
    # "qwen" substring, which would send a thinking control to a model that has
    # no thinking mode to suppress.
    for model in ("qwen2:7b", "qwen2.5:7b", "qwen-embed:0.6b"):
        captured = {}
        with patch("urllib.request.urlopen", side_effect=_capturing_urlopen(captured)):
            _call_ollama("rate this response", model=model)
        body = captured["body"]
        assert "think" not in body, f"{model} should not get think key"
        assert "/no_think" not in body["prompt"], f"{model} should not get suffix"
