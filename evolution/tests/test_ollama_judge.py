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


def test_num_ctx_is_sent_explicitly():
    # LIA-558: without an explicit num_ctx, Ollama applies its own 4096 default
    # while a real eval prompt runs ~4000 tokens — so the prompt or the response
    # truncates depending on the host's build, and scores stop being comparable
    # across machines. Pinning it is the whole point; assert it reaches the wire.
    from evolution.config import JUDGE_NUM_CTX

    captured = {}
    with patch("urllib.request.urlopen", side_effect=_capturing_urlopen(captured)):
        _call_ollama("rate this response", model="gemma4:e4b")
    options = captured["body"]["options"]
    assert options["num_ctx"] == JUDGE_NUM_CTX
    assert options["num_ctx"] > 4096, "must exceed Ollama's default to be useful"


def test_judge_model_is_recorded_on_the_result():
    # LIA-558: a model that ignores the structured-output schema produces rows
    # that are otherwise indistinguishable from healthy ones, so the score has
    # to carry the model that produced it.
    from evolution.judge.ollama_judge import _parse_result

    result = _parse_result(
        json.dumps({
            "safe": True, "quality_level": 5, "recalled_preference": True,
            "format_matched": True, "tone_matched": True,
            "execution_quality": 5, "rationale": "ok",
        }),
        "gemma4:e4b",
    )
    assert result.model == "gemma4:e4b"


def test_judge_model_is_recorded_even_on_a_parse_error():
    # The parse-error path is exactly where knowing the model matters most.
    from evolution.judge.ollama_judge import _parse_result

    result = _parse_result("not json at all", "gemma4:12b-mlx")
    assert result.is_parse_error is True
    assert result.model == "gemma4:12b-mlx"
