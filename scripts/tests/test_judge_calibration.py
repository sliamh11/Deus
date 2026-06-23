"""Tests for the judge calibration watchdog (scripts/maintenance/judge_calibration.py).

Hermetic: the bench subprocess is replaced by an injected `runner` returning a
synthetic result row, and the macOS notifier by a recorder — nothing shells out
and Ollama is never touched. `_load_floor` is exercised against throwaway
baselines.json files under tmp_path. The contract under test mirrors
safety_redteam's infra-precedence (`_classify_gate_outcome`): a degraded/partial
run is INCONCLUSIVE (exit 0), never a false-alarm regression.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[1] / "maintenance" / "judge_calibration.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("judge_calibration", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["judge_calibration"] = mod
    spec.loader.exec_module(mod)
    return mod


jc = _load()

FLOOR = 0.58


def _row(pearson, n=200, parse_errors=0, model="gemma4:e4b"):
    """A benchmark_judge json-out result row (shape: dims.quality.pearson)."""
    return {
        "model": model,
        "n": n,
        "parse_errors": parse_errors,
        "dims": {"quality": {"pearson": pearson, "spearman": 0.6, "mae": 0.2}},
    }


# ── _classify_outcome ─────────────────────────────────────────────────────────

def test_classify_ok_when_pearson_at_or_above_floor():
    for p in (FLOOR, FLOOR + 0.01, 0.67):
        code, status, _ = jc._classify_outcome(_row(p), FLOOR)
        assert (code, status) == (0, "OK"), p


def test_classify_warn_below_floor_exits_nonzero():
    code, status, msg = jc._classify_outcome(_row(FLOOR - 0.05), FLOOR)
    assert code == 1 and status == "WARN"
    # Message must name the dimension AND both numbers, for an actionable alert.
    assert "quality" in msg
    assert f"{FLOOR:.3f}" in msg and f"{FLOOR - 0.05:.3f}" in msg


def test_classify_none_result_is_inconclusive_exit0():
    code, status, _ = jc._classify_outcome(None, FLOOR)
    assert (code, status) == (0, "INCONCLUSIVE")


def test_classify_too_few_records_is_inconclusive():
    # Below _MIN_N: a partial run, even with a great Pearson, must not be trusted.
    code, status, _ = jc._classify_outcome(_row(0.9, n=jc._MIN_N - 1), FLOOR)
    assert (code, status) == (0, "INCONCLUSIVE")


def test_classify_min_n_boundary_is_measured():
    # Exactly _MIN_N records is a full-enough run -> measured (here, OK).
    code, status, _ = jc._classify_outcome(_row(0.9, n=jc._MIN_N), FLOOR)
    assert (code, status) == (0, "OK")


def test_classify_high_parse_error_rate_is_inconclusive():
    # >10% malformed judge outputs: Pearson untrustworthy regardless of value.
    n = 200
    bad = int(n * jc._MAX_PARSE_ERROR_RATE) + 1
    code, status, _ = jc._classify_outcome(_row(0.2, n=n, parse_errors=bad), FLOOR)
    assert (code, status) == (0, "INCONCLUSIVE")


def test_classify_parse_errors_within_tolerance_still_measured():
    n = 200
    ok_errs = int(n * jc._MAX_PARSE_ERROR_RATE)  # exactly at the threshold, not over
    code, status, _ = jc._classify_outcome(_row(0.2, n=n, parse_errors=ok_errs), FLOOR)
    # A real regression (0.2 < floor) IS flagged when the run is clean enough.
    assert (code, status) == (1, "WARN")


def test_classify_unreadable_pearson_is_inconclusive():
    row = _row(None)
    row["dims"]["quality"]["pearson"] = None  # degenerate/constant column
    code, status, _ = jc._classify_outcome(row, FLOOR)
    assert (code, status) == (0, "INCONCLUSIVE")


def test_classify_missing_dims_is_inconclusive():
    code, status, _ = jc._classify_outcome({"model": "m", "n": 200, "parse_errors": 0}, FLOOR)
    assert (code, status) == (0, "INCONCLUSIVE")


# ── _load_floor ───────────────────────────────────────────────────────────────

def _patch_baselines(monkeypatch, tmp_path, content):
    p = tmp_path / "baselines.json"
    if content is not None:
        p.write_text(content)
    monkeypatch.setattr(jc, "_BASELINES_PATH", p)
    return p


def test_load_floor_reads_file_value(monkeypatch, tmp_path):
    _patch_baselines(monkeypatch, tmp_path, json.dumps({"quality_pearson": {"floor": 0.6}}))
    assert jc._load_floor() == 0.6


def test_load_floor_falls_back_when_missing(monkeypatch, tmp_path):
    _patch_baselines(monkeypatch, tmp_path, None)
    assert jc._load_floor() == jc._DEFAULT_QUALITY_FLOOR


def test_load_floor_falls_back_when_malformed(monkeypatch, tmp_path):
    _patch_baselines(monkeypatch, tmp_path, "{ not json")
    assert jc._load_floor() == jc._DEFAULT_QUALITY_FLOOR


def test_load_floor_falls_back_when_key_absent(monkeypatch, tmp_path):
    _patch_baselines(monkeypatch, tmp_path, json.dumps({"safety_recall": {"floor": 0.86}}))
    assert jc._load_floor() == jc._DEFAULT_QUALITY_FLOOR


def test_load_floor_falls_back_when_out_of_range(monkeypatch, tmp_path):
    _patch_baselines(monkeypatch, tmp_path, json.dumps({"quality_pearson": {"floor": 1.5}}))
    assert jc._load_floor() == jc._DEFAULT_QUALITY_FLOOR


# ── main (injected runner + recording notifier) ───────────────────────────────

class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, title, message):
        self.calls.append((title, message))


def test_main_healthy_exit0_no_notify(monkeypatch, tmp_path):
    _patch_baselines(monkeypatch, tmp_path, json.dumps({"quality_pearson": {"floor": FLOOR}}))
    rec = _Recorder()
    code = jc.main(argv=[], notifier=rec, runner=lambda *a: _row(0.67))
    assert code == 0
    assert rec.calls == []  # OK never notifies


def test_main_regression_exit1_notifies_once(monkeypatch, tmp_path):
    _patch_baselines(monkeypatch, tmp_path, json.dumps({"quality_pearson": {"floor": FLOOR}}))
    rec = _Recorder()
    code = jc.main(argv=[], notifier=rec, runner=lambda *a: _row(0.40))
    assert code == 1
    assert len(rec.calls) == 1
    assert "quality" in rec.calls[0][1]


def test_main_infra_failure_exit0_no_notify(monkeypatch, tmp_path):
    # runner returns None (Ollama down / timeout / bad json) -> INCONCLUSIVE, quiet.
    _patch_baselines(monkeypatch, tmp_path, json.dumps({"quality_pearson": {"floor": FLOOR}}))
    rec = _Recorder()
    code = jc.main(argv=[], notifier=rec, runner=lambda *a: None)
    assert code == 0
    assert rec.calls == []


def test_run_benchmark_missing_fixture_returns_none(tmp_path):
    # The real runner short-circuits to None (INCONCLUSIVE) when the fixture is
    # absent — never shells out. Exercises the live function, no Ollama needed.
    missing = tmp_path / "nope.jsonl"
    assert jc._run_benchmark(missing, "gemma4:e4b", timeout_s=1) is None
