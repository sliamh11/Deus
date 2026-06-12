"""Tests for parameter optimizer — unit tests that don't require a live DB."""
import json
import random
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from evolution.optimizer.param_optimizer import (
    BENCH_LABELS,
    INT_PARAMS,
    SEARCH_SPACE,
    _load_labels,
    _sample_params,
    _score_result,
    optimize_and_save,
    optimize_params,
)


def test_load_labels():
    """Benchmark labels file exists and is parseable."""
    assert BENCH_LABELS.exists(), f"Benchmark labels not found: {BENCH_LABELS}"
    labels = _load_labels()
    assert len(labels) >= 90
    for label in labels:
        assert "query" in label
        assert "tag" in label


def test_sample_params_ranges():
    """Sampled params stay within defined search space."""
    rng = random.Random(42)
    for _ in range(50):
        params = _sample_params(rng)
        for name, (lo, hi) in SEARCH_SPACE.items():
            assert lo <= params[name] <= hi, f"{name}={params[name]} out of [{lo}, {hi}]"
        for name in INT_PARAMS:
            assert isinstance(params[name], int), f"{name} should be int"


def test_sample_params_deterministic():
    """Same seed produces same params."""
    p1 = _sample_params(random.Random(123))
    p2 = _sample_params(random.Random(123))
    assert p1 == p2


def test_score_result_filters_bad_abstain():
    """Results with abstain accuracy below threshold return None."""
    result = {
        "recall_at_k": 0.9,
        "mrr_at_k": 0.85,
        "abstain_accuracy": 0.5,
    }
    assert _score_result(result, min_abstain=0.8) is None


def test_score_result_passes_good_abstain():
    """Results meeting abstain threshold get a positive score."""
    result = {
        "recall_at_k": 0.9,
        "mrr_at_k": 0.85,
        "abstain_accuracy": 0.9,
    }
    score = _score_result(result, min_abstain=0.8)
    assert score is not None
    assert score > 0


def test_score_result_weights():
    """Score = 0.8 * recall + 0.2 * mrr."""
    result = {
        "recall_at_k": 1.0,
        "mrr_at_k": 1.0,
        "abstain_accuracy": 1.0,
    }
    assert _score_result(result) == pytest.approx(1.0)

    result2 = {
        "recall_at_k": 0.5,
        "mrr_at_k": 0.0,
        "abstain_accuracy": 1.0,
    }
    assert _score_result(result2) == pytest.approx(0.4)


def test_score_result_error():
    """Error results return None."""
    assert _score_result({"error": "empty dataset"}) is None


def test_score_result_no_abstain():
    """Results without abstain data still score (abstain constraint skipped)."""
    result = {
        "recall_at_k": 0.8,
        "mrr_at_k": 0.7,
    }
    score = _score_result(result)
    assert score is not None
    assert score == pytest.approx(0.8 * 0.8 + 0.7 * 0.2)


# ── LIA-209: self-calibrating abstain floor ──────────────────────────────────


@patch("evolution.optimizer.param_optimizer._open_db")
@patch("evolution.optimizer.param_optimizer._precompute_embeddings")
@patch("evolution.optimizer.param_optimizer._run_trial")
@patch("evolution.optimizer.param_optimizer._load_labels")
def test_optimize_params_self_calibrates_floor(mock_labels, mock_trial, mock_embed, mock_db):
    """With min_abstain=None the floor calibrates to the baseline's own abstain
    (0.727), so the baseline is no longer rejected by a fixed 0.8 — returns a
    result where the old hardcoded floor returned None."""
    mock_labels.return_value = [{"query": "q1", "tag": "t"}]
    mock_embed.return_value = {}
    mock_db.return_value = MagicMock()
    # Every trial (incl. baseline at i==0) reports abstain 0.727 < the old 0.8.
    mock_trial.return_value = {
        "recall_at_k": 0.6, "mrr_at_k": 0.5, "abstain_accuracy": 0.727,
    }

    # verbose=True also exercises the `abstain floor={effective_floor:.3f}`
    # log line (the float-formatting path).
    result = optimize_params(trials=3, min_abstain=None, verbose=True)
    assert result is not None, "self-calibrated floor should accept the baseline"
    assert result["score"] >= 0
    assert result["abstain_accuracy"] == pytest.approx(0.727)


@patch("evolution.optimizer.param_optimizer._seed_from_defaults", return_value=[])
@patch("evolution.optimizer.param_optimizer._open_db")
@patch("evolution.optimizer.param_optimizer._load_labels")
def test_optimize_params_refuses_self_calibration_without_defaults(
    mock_labels, mock_db, mock_seed
):
    """When production defaults can't be imported (_seed_from_defaults() == []),
    self-calibration (min_abstain=None) refuses — it must not anchor the floor to
    a random sample. Fails fast before the embedding step (LIA-209 ai-eng review)."""
    mock_labels.return_value = [{"query": "q1", "tag": "t"}]
    mock_db.return_value = MagicMock()
    assert optimize_params(trials=3, min_abstain=None, verbose=False) is None


@patch("evolution.optimizer.param_optimizer._run_trial")
@patch("evolution.optimizer.param_optimizer._precompute_embeddings")
@patch("evolution.optimizer.param_optimizer._open_db")
@patch("evolution.optimizer.param_optimizer._seed_from_defaults", return_value=[])
@patch("evolution.optimizer.param_optimizer._load_labels")
def test_optimize_params_explicit_floor_runs_without_defaults(
    mock_labels, mock_seed, mock_db, mock_embed, mock_trial
):
    """An explicit min_abstain has no dependency on the production defaults, so it
    proceeds even when _seed_from_defaults() is empty."""
    mock_labels.return_value = [{"query": "q1", "tag": "t"}]
    mock_db.return_value = MagicMock()
    mock_embed.return_value = {}
    mock_trial.return_value = {
        "recall_at_k": 0.6, "mrr_at_k": 0.5, "abstain_accuracy": 0.9,
    }
    result = optimize_params(trials=3, min_abstain=0.8, verbose=False)
    assert result is not None  # 0.9 >= 0.8 floor, runs fine without a seed


@patch("evolution.optimizer.param_optimizer._open_db")
@patch("evolution.optimizer.param_optimizer._precompute_embeddings")
@patch("evolution.optimizer.param_optimizer._run_trial")
@patch("evolution.optimizer.param_optimizer._load_labels")
def test_optimize_params_explicit_min_abstain_overrides(mock_labels, mock_trial, mock_embed, mock_db):
    """An explicit min_abstain above the trials' abstain rejects every trial
    (the pre-LIA-209 'No valid trial found' behavior, now opt-in)."""
    mock_labels.return_value = [{"query": "q1", "tag": "t"}]
    mock_embed.return_value = {}
    mock_db.return_value = MagicMock()
    mock_trial.return_value = {
        "recall_at_k": 0.6, "mrr_at_k": 0.5, "abstain_accuracy": 0.727,
    }

    result = optimize_params(trials=3, min_abstain=0.8, verbose=False)
    assert result is None, "explicit floor of 0.8 must reject all 0.727 trials"


@patch("evolution.optimizer.param_optimizer.optimize_params")
def test_optimize_and_save_skips_on_zero_delta(mock_opt):
    """LIA-209: delta == 0 (no improvement) must NOT save — tightened from < 0."""
    mock_opt.return_value = {
        "delta": 0.0, "params": {}, "baseline_score": 0.5, "score": 0.5, "trials": 3,
    }
    assert optimize_and_save(verbose=False) is None


@patch("evolution.optimizer.artifacts.save_artifact", return_value="aid-xyz")
@patch("evolution.optimizer.param_optimizer._detect_provider", return_value="ollama")
@patch("evolution.optimizer.param_optimizer.optimize_params")
def test_optimize_and_save_saves_on_positive_delta(mock_opt, mock_prov, mock_save):
    """A strictly-positive delta still saves an artifact."""
    mock_opt.return_value = {
        "delta": 0.05, "params": {"a": 1}, "baseline_score": 0.5, "score": 0.55, "trials": 3,
    }
    aid = optimize_and_save(verbose=False)
    assert aid == "aid-xyz"
    mock_save.assert_called_once()


def test_cli_optimize_params_dry_run_does_not_save(monkeypatch, capsys):
    """The optimize-params --dry-run CLI path computes via optimize_params and
    never calls optimize_and_save (so no artifact reaches live retrieval)."""
    from evolution import cli

    calls = {"params": 0, "save": 0}

    def fake_params(**kw):
        calls["params"] += 1
        return {"params": {"a": 1}, "score": 0.5, "baseline_score": 0.5, "delta": 0.0, "trials": 1}

    def fake_save(**kw):
        calls["save"] += 1
        return "should-not-be-saved"

    monkeypatch.setattr("evolution.optimizer.param_optimizer.optimize_params", fake_params)
    monkeypatch.setattr("evolution.optimizer.param_optimizer.optimize_and_save", fake_save)
    monkeypatch.setattr("sys.argv", ["cli", "optimize-params", "--dry-run", "--trials", "1"])

    cli.main()

    assert calls["params"] == 1
    assert calls["save"] == 0, "--dry-run must never save an artifact"
    assert '"params"' in capsys.readouterr().out
