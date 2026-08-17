"""Deterministic tests for benchmark_judge fixture-mode extensions (no Ollama calls)."""
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import pytest

from evolution import benchmark_judge as bj


# ── metrics ──────────────────────────────────────────────────────────────────

def test_pearson_matches_hand_value():
    r = bj._pearson([1, 2, 3, 4, 5], [2, 4, 5, 4, 5])
    assert r is not None and abs(r - 0.7745966) < 1e-4


def test_pearson_constant_is_none():
    assert bj._pearson([1, 1, 1, 1], [1, 2, 3, 4]) is None   # safety dim (all-safe)
    assert bj._pearson([1, 2], [1, 2]) is None               # n<3


def test_spearman_handles_ties():
    s = bj._spearman([1, 1, 2, 3], [1, 2, 2, 3])
    assert s is not None and abs(s - 0.8333333) < 1e-4


def test_mae():
    assert abs(bj._mae([0.0, 1.0], [0.5, 0.5]) - 0.5) < 1e-9


# ── trivial baselines (fixture gameability) ─────────────────────────────────

def test_constant_mean_mae_hand_value():
    assert abs(bj._constant_mean_mae([0.0, 0.5, 1.0]) - 1 / 3) < 1e-9
    assert bj._constant_mean_mae([]) is None


def test_logistic_fit_pearson_deterministic_and_realistic_scale():
    # log(len)-like feature magnitude (~2-9), NOT standardized by the caller —
    # must not overflow/diverge (this is exactly what plan-review flagged).
    feature = [2.3, 3.1, 4.0, 4.8, 5.5, 6.2, 6.9, 7.5, 8.1, 8.8] * 3
    truth = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0] * 3
    r1 = bj._logistic_fit_pearson(feature, truth)
    r2 = bj._logistic_fit_pearson(feature, truth)
    assert r1 == r2                      # identical inputs -> bit-identical output
    assert r1 is not None and r1 > 0.9   # near-linear signal fits well


def test_logistic_fit_pearson_edge_cases():
    assert bj._logistic_fit_pearson([], []) is None                    # n=0
    assert bj._logistic_fit_pearson([1.0] * 5, [0.1, 0.2, 0.3, 0.4, 0.5]) is None  # constant feature


def test_compute_trivial_baselines_end_to_end():
    interactions = [
        {"response": "short", "ground_truth_score": 0.2},
        {"response": "a bit longer response here", "ground_truth_score": 0.5},
        {"response": "a substantially longer and more detailed response than the others", "ground_truth_score": 0.9},
        {"response": "medium length one", "ground_truth_score": 0.4},
        {"response": "yet another medium-ish length response for this row", "ground_truth_score": 0.6},
    ]
    baselines = bj.compute_trivial_baselines(interactions)
    assert set(baselines) == {"log_length_pearson", "constant_mean_mae", "logistic_pearson"}
    assert baselines["log_length_pearson"] is not None
    assert baselines["constant_mean_mae"] is not None


def test_compute_trivial_baselines_reproduces_verified_fixture_numbers():
    # Exact-match regression guard against the independently spot-verified
    # numbers in Handoffs/2026-08-02-01-15-gpt56-judge-benchmark-reliability.md:
    # a bare log(response_length) regressor scores r=0.485 on fixture-v1 (n=200)
    # and r=0.338 on fixture-openai-v1 (n=104).
    openai_fixture = Path(__file__).resolve().parents[2] / "finetune" / "judge-bench" / "fixture-openai-v1.jsonl"
    if not openai_fixture.exists():
        pytest.skip("fixture-openai-v1.jsonl not present in this checkout (gitignored)")
    interactions = bj._load_fixture(openai_fixture)
    baselines = bj.compute_trivial_baselines(interactions)
    assert abs(baselines["log_length_pearson"] - 0.338) < 5e-3


def test_threshold_pr_hand_example():
    # truth flagged (<0.6): idx 0,3 ; pred flagged: idx 0,1
    pred = [0.3, 0.4, 0.7, 0.8]
    truth = [0.2, 0.9, 0.7, 0.5]
    r = bj._threshold_pr(pred, truth, thresh=0.6)
    assert (r["tp"], r["fp"], r["fn"], r["tn"]) == (1, 1, 1, 1)
    assert r["precision"] == 0.5 and r["recall"] == 0.5
    assert r["n_flagged"] == 2


def test_threshold_pr_no_positives_gives_none():
    r = bj._threshold_pr([0.9, 0.8], [0.9, 0.8])
    assert r["precision"] is None and r["recall"] is None


def test_prf_defined_but_zero_f1_is_zero_not_none():
    # tp=0 with fp>0 and fn>0: precision=0.0, recall=0.0 → F1 is defined (0.0), not None
    prec, rec, f1 = bj._prf(tp=0, fp=3, fn=2)
    assert prec == 0.0 and rec == 0.0 and f1 == 0.0


def test_prf_undefined_when_no_predictions():
    assert bj._prf(tp=0, fp=0, fn=5) == (None, 0.0, None)  # no predicted-positives → precision None


def test_modelresult_pearson_delegates_and_coerces_none():
    mr = bj.ModelResult(model="m")
    mr.scores = [1.0, 1.0, 1.0]          # constant → standalone returns None
    mr.ground_truth = [0.0, 0.5, 1.0]
    assert mr.pearson == 0.0 and mr.spearman == 0.0   # legacy float contract preserved


# ── bootstrap determinism ──────────────────────────────────────────────────────

def test_bootstrap_ci_is_deterministic():
    a = [0.1, 0.2, 0.9, 0.8, 0.5, 0.4, 0.3, 0.7]
    b = [0.15, 0.25, 0.85, 0.75, 0.55, 0.45, 0.35, 0.65]
    ci1 = bj._bootstrap_ci(a, b, bj._pearson, seed=99)
    ci2 = bj._bootstrap_ci(a, b, bj._pearson, seed=99)
    assert ci1 == ci2                       # same seed → identical bounds
    assert ci1[0] <= ci1[1]
    assert bj._bootstrap_ci([1, 2], [1, 2], bj._pearson) is None  # n<3


def test_paired_delta_ci_deterministic_and_signed():
    truth = [0.0, 0.25, 0.5, 0.75, 1.0, 0.3, 0.6, 0.9]
    worse = [0.9, 0.6, 0.3, 1.0, 0.75, 0.5, 0.25, 0.0]  # non-constant, decorrelated
    better = list(truth)                                # perfect
    d1 = bj._paired_delta_ci(worse, better, truth, bj._pearson, seed=5)
    d2 = bj._paired_delta_ci(worse, better, truth, bj._pearson, seed=5)
    assert d1 == d2
    assert d1["median"] > 0 and d1["p_b_gt_a"] > 0.9   # better model wins


# ── fixture + safety-probe loaders ─────────────────────────────────────────────

# ── fixture identity (--json-out top-level "fixture" block) ─────────────────

def test_fixture_identity_records_path_hash_and_n(tmp_path):
    p = tmp_path / "fix.jsonl"
    p.write_text(json.dumps({"id": "a", "prompt": "P"}) + "\n", encoding="utf-8")
    expected_sha = hashlib.sha256(p.read_bytes()).hexdigest()
    identity = bj._fixture_identity(p, n=7)
    assert identity["path"] == str(p)
    assert identity["sha256"] == expected_sha
    assert identity["n"] == 7


def test_fixture_identity_hash_changes_with_content(tmp_path):
    p = tmp_path / "fix.jsonl"
    p.write_text("one\n", encoding="utf-8")
    id1 = bj._fixture_identity(p, n=1)
    p.write_text("two\n", encoding="utf-8")
    id2 = bj._fixture_identity(p, n=1)
    assert id1["sha256"] != id2["sha256"]


def test_load_fixture_maps_and_skips(tmp_path):
    p = tmp_path / "fix.jsonl"
    lines = [
        {"_meta": "doc"},  # skipped (no prompt)
        {"id": "ok", "prompt": "P", "response": "R", "tools_used": ["Bash"],
         "gemini_dims": {"quality": 0.5, "safety": 1.0, "tool_use": 1.0, "personalization": 0.25},
         "gemini_composite": 0.6, "is_parse_error": False, "digest_text": "ws: concise"},
        {"id": "bad", "prompt": "P2", "response": "R2", "gemini_dims": {"quality": 0.0},
         "gemini_composite": 0.0, "is_parse_error": True},  # skipped (parse error)
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    recs = bj._load_fixture(p)
    assert len(recs) == 1
    r = recs[0]
    assert r["id"] == "ok" and r["ground_truth_score"] == 0.6
    assert r["ground_truth_dims"]["personalization"] == 0.25
    assert r["digest_text"] == "ws: concise"


def test_load_fixture_empty_raises(tmp_path):
    p = tmp_path / "empty.jsonl"
    p.write_text(json.dumps({"_meta": "only"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError):
        bj._load_fixture(p)


def test_load_safety_probes_skips_meta(tmp_path):
    p = tmp_path / "probes.jsonl"
    p.write_text(
        json.dumps({"_meta": "doc"}) + "\n" +
        json.dumps({"id": "s1", "prompt": "P", "response": "R", "tools_used": None,
                    "gold_safety": 0.0, "category": "bias"}) + "\n",
        encoding="utf-8",
    )
    probes = bj._load_safety_probes(p)
    assert len(probes) == 1 and probes[0]["gold_safety"] == 0.0


def test_committed_safety_probes_are_valid():
    probes = bj._load_safety_probes(bj.SAFETY_PROBES_PATH)
    assert len(probes) >= 10
    assert any(p["gold_safety"] < 0.5 for p in probes)   # has unsafe
    assert any(p["gold_safety"] >= 0.5 for p in probes)  # has safe controls


# ── benchmark(): digest symmetry + per-dim recording ───────────────────────────

@dataclass
class _Res:
    quality: float = 0.5
    safety: float = 1.0
    tool_use: float = 1.0
    personalization: float = 0.25
    score: float = 0.55
    rationale: str = "ok"
    raw_response: str = "{}"
    is_parse_error: bool = False
    # LIA-580: benchmark_judge checks this before appending to its score arrays.
    is_schema_error: bool = False


def test_benchmark_passes_digest_and_records_per_dim(monkeypatch):
    captured = {}

    class _FakeJudge:
        def __init__(self, model):
            self.model = model

        def evaluate(self, prompt, response, tools_used=None, user_profile=None):
            captured["user_profile"] = user_profile
            return _Res()

    monkeypatch.setattr(bj, "OllamaRuntimeJudge", _FakeJudge)
    monkeypatch.setattr(bj, "_check_model_pulled", lambda m: None)

    interactions = [{
        "id": "i1", "prompt": "P", "response": "R", "tools_used": None,
        "ground_truth_score": 0.6,
        "ground_truth_dims": {"quality": 0.75, "safety": 1.0, "tool_use": 1.0, "personalization": 0.5},
        "digest_text": "work-style: concise + direct",
    }]
    results = bj.benchmark(["gemma4:e4b"], interactions, verbose=False)
    mr = results[0]
    # digest symmetry: the same digest used at label time reached the local judge
    assert captured["user_profile"] == "work-style: concise + direct"
    # per-dim arrays populated from ground_truth_dims
    assert mr.dim_scores["personalization"] == [0.25]
    assert mr.dim_truth["personalization"] == [0.5]
    assert mr.scores == [0.55] and mr.ground_truth == [0.6]
    # per-record dim_truth/dim_scores (what --json-out's "records" array serializes
    # directly, for paired cross-run/cross-fixture comparison by interaction_id)
    assert len(mr.details) == 1
    d0 = mr.details[0]
    assert d0.interaction_id == "i1"
    assert d0.dim_scores == {"quality": 0.5, "safety": 1.0, "tool_use": 1.0, "personalization": 0.25}
    assert d0.dim_truth == {"quality": 0.75, "safety": 1.0, "tool_use": 1.0, "personalization": 0.5}


# ── print_comparison back-compat ────────────────────────────────────────────

def test_print_comparison_without_trivial_baselines_still_works(capsys):
    mr = bj.ModelResult(model="m")
    mr.scores = [0.1, 0.5, 0.9]
    mr.ground_truth = [0.2, 0.4, 0.8]
    mr.total = 3
    bj.print_comparison([mr])  # trivial_baselines defaults to None — no crash, no Δ column
    out = capsys.readouterr().out
    assert "Δ vs log-len" not in out
    assert "Trivial baselines" not in out


def test_print_comparison_with_trivial_baselines(capsys):
    mr = bj.ModelResult(model="m")
    mr.scores = [0.1, 0.5, 0.9]
    mr.ground_truth = [0.2, 0.4, 0.8]
    mr.total = 3
    bj.print_comparison([mr], trivial_baselines={
        "log_length_pearson": 0.3, "logistic_pearson": 0.35, "constant_mean_mae": 0.2,
    })
    out = capsys.readouterr().out
    assert "Trivial baselines" in out and "log-length r=0.300" in out
    assert "Δ vs log-len" in out
