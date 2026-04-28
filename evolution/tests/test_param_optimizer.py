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
)


def test_load_labels():
    """Benchmark labels file exists and is parseable."""
    assert BENCH_LABELS.exists(), f"Benchmark labels not found: {BENCH_LABELS}"
    labels = _load_labels()
    assert len(labels) == 90
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
