"""Unit tests for evolution/training/_smoke.py.

NOTE: these tests cover module-level constants, dataclass shape, exception
shape, batch-selection logic, and padding math — they do NOT exercise the
real `mlx_lm.load` path (cost ~3 min on cold cache, unsuitable for CI).

If you edit `_smoke.py`, you MUST run
    python3 evolution/training/train_judge_lora.py --smoke-test-only
locally (via the project's judge-LoRA venv — see the script's module docstring
for how it resolves the venv) before pushing, so regressions in the real-mlx
call path surface before they reach main. The plan-reviewer flagged this gap
as an accepted limitation; the developer is the canary.
"""
from __future__ import annotations

import pytest

from evolution.training import _smoke as S


def test_lora_keys_constant_shape():
    """LORA_KEYS_GEMMA3N is the canonical 7-key allow-list for gemma-3n
    decoder layers. Each key is a path-like string (no leading slash, no
    spaces). None contain the forbidden gemma-3n substrings."""
    assert isinstance(S.LORA_KEYS_GEMMA3N, tuple)
    assert len(S.LORA_KEYS_GEMMA3N) == 7
    for k in S.LORA_KEYS_GEMMA3N:
        assert isinstance(k, str)
        assert k.strip() == k
        assert "/" not in k
        # Sanity: each key looks like a module path with at least one dot
        assert "." in k
        # Forbidden substrings must NOT appear in the allow-list
        for forbidden in S.GEMMA3N_FORBIDDEN_WRAP_SUBSTRINGS:
            assert forbidden not in k, f"LORA_KEYS_GEMMA3N contains forbidden substring {forbidden!r}: {k!r}"


def test_smoke_result_dataclass_shape():
    """SmokeResult carries the five fields the wrapper unpacks into the JSON
    success payload + a 'mode' tag for stress-vs-representative."""
    r = S.SmokeResult(
        loss=1.23, step_ms=456.0, peak_memory_gb=7.89,
        wrapped_modules=56, mode="representative", effective_seq_length=512,
    )
    assert r.loss == 1.23
    assert r.step_ms == 456.0
    assert r.peak_memory_gb == 7.89
    assert r.wrapped_modules == 56
    assert r.mode == "representative"
    assert r.effective_seq_length == 512


def test_smoke_error_carries_reason_and_diagnostic():
    """SmokeError must surface both `reason` (machine-readable category) and
    `diagnostic` (free-form dict) — the wrapper uses both to construct the
    SystemExit message that the developer sees."""
    e = S.SmokeError("model_load", {"model": "fake", "error": "boom"})
    assert e.reason == "model_load"
    assert e.diagnostic == {"model": "fake", "error": "boom"}
    # __str__ should mention both reason and diagnostic for debug logs
    s = str(e)
    assert "model_load" in s
    assert "fake" in s


def test_pad_batch_realistic_matches_iterate_batches_formula():
    """Verifies the padding formula matches mlx_lm/tuner/trainer.py:156-159.

    Formula: eff = min(1 + 32 * ceil(max_in_batch / 32), max_seq_length).
    """
    # Two records of length 500 and 600 → max=600 → 1 + 32 * ceil(600/32) = 1 + 32*19 = 609
    batch = [[1] * 500, [1] * 600]
    eff, padded = S._pad_batch_realistic(batch, max_seq_length=4096, pad_id=0)
    assert eff == 609
    assert all(len(r) == 609 for r in padded)
    # First record padded with 109 zeros at the tail
    assert padded[0][:500] == [1] * 500
    assert padded[0][500:] == [0] * 109

    # max_seq_length CAP: max=3000 capped at 2048
    batch2 = [[1] * 100, [1] * 3000]
    eff2, padded2 = S._pad_batch_realistic(batch2, max_seq_length=2048, pad_id=0)
    assert eff2 == 2048
    assert all(len(r) == 2048 for r in padded2)
    # Long record gets truncated to the cap
    assert padded2[1] == [1] * 2048


def test_select_batch_representative_picks_median_band():
    """Representative (default) mode returns the median-band slice."""
    tokenized = [[1] * n for n in range(1, 101)]  # 100 records, lengths 1..100
    selected = S._select_batch(tokenized, batch_size=2, stress=False)
    # mid = 100 // 2 = 50, so records 50..51 (lengths 51, 52)
    assert len(selected) == 2
    assert len(selected[0]) == 51
    assert len(selected[1]) == 52


def test_select_batch_stress_picks_longest():
    """Stress mode returns the longest batch_size records."""
    tokenized = [[1] * n for n in range(1, 101)]
    selected = S._select_batch(tokenized, batch_size=2, stress=True)
    assert len(selected) == 2
    assert len(selected[0]) == 99
    assert len(selected[1]) == 100


def test_select_batch_raises_when_dataset_too_small():
    """If batch_size exceeds dataset size, surface a SmokeError immediately."""
    with pytest.raises(S.SmokeError) as exc_info:
        S._select_batch([[1, 2, 3]], batch_size=4, stress=False)
    assert exc_info.value.reason == "dataset"


def test_main_cli_arg_parsing_smoke(monkeypatch, tmp_path):
    """Argument-parsing path of the `python -m _smoke` CLI. Mocks run_smoke
    so the test doesn't load a real model — just verifies the CLI accepts
    the wire format that train_judge_lora.py uses."""
    captured: dict = {}

    def fake_run_smoke(**kwargs):
        captured.update(kwargs)
        return S.SmokeResult(
            loss=0.0, step_ms=0.0, peak_memory_gb=0.0,
            wrapped_modules=7, mode="representative", effective_seq_length=64,
        )

    monkeypatch.setattr(S, "run_smoke", fake_run_smoke)
    rc = S.main([
        "--model", "fake-model",
        "--train-jsonl", str(tmp_path / "train.jsonl"),
        "--batch-size", "2",
        "--max-seq-length", "4096",
        "--num-layers", "8",
        "--lora-rank", "16",
        "--lora-scale", "2.0",
        "--lora-dropout", "0.05",
        "--grad-checkpoint",
    ])
    assert rc == 0
    assert captured["model"] == "fake-model"
    assert captured["batch_size"] == 2
    assert captured["grad_checkpoint"] is True
    assert captured["lora_keys"] == S.LORA_KEYS_GEMMA3N  # default when --lora-keys not passed
