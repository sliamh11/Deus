"""Unit tests for scripts/bench/attention_dilution_probe.py (M1c).

Covers deterministic logic only — no live LLM calls. The live-LLM path
(`--pilot` and full runs) is the human-run gate before opening the PR.

Pre-registered hypotheses + thresholds are tested for arithmetic consistency,
not for empirical truth. The probe ARMS, threshold values (e.g. 60/100 for
H1 = 0.0284 binomial p at N=100), and aggregator shape are part of the
contract.
"""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BENCH_PATH = (
    Path(__file__).resolve().parent.parent / "bench" / "attention_dilution_probe.py"
)


def _load_bench():
    """Load the bench module without polluting sys.modules across tests."""
    spec = importlib.util.spec_from_file_location(
        "attention_dilution_probe", _BENCH_PATH
    )
    assert spec and spec.loader, f"cannot load spec from {_BENCH_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["attention_dilution_probe"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def bench():
    return _load_bench()


# ---------------------------------------------------------------------------
# Contract: ARMS config locked to 3 entries (zero / tight / prod)
# ---------------------------------------------------------------------------


def test_arms_locked_to_five(bench):
    """ARMS is exactly 5 entries: zero, minimal@400, tight@800, prod@1500, bloated@3000."""
    arms = bench.ARMS
    assert len(arms) == 5, f"expected 5 arms, got {len(arms)}"
    names = [a["name"] for a in arms]
    assert names == ["zero", "minimal", "tight", "prod", "bloated"], (
        f"unexpected arm names: {names}"
    )
    by_name = {a["name"]: a for a in arms}
    assert by_name["zero"]["format"] is None and by_name["zero"]["budget"] == 0
    assert (
        by_name["minimal"]["format"] == "name_desc"
        and by_name["minimal"]["budget"] == 400
    )
    assert (
        by_name["tight"]["format"] == "name_desc" and by_name["tight"]["budget"] == 800
    )
    assert (
        by_name["prod"]["format"] == "name_desc" and by_name["prod"]["budget"] == 1500
    )
    assert (
        by_name["bloated"]["format"] == "name_desc"
        and by_name["bloated"]["budget"] == 3000
    )


# ---------------------------------------------------------------------------
# Standards-text builder — zero arm short-circuits to empty
# ---------------------------------------------------------------------------


def test_standards_text_for_zero_arm_is_empty(bench):
    """The zero arm returns '' WITHOUT invoking standards_pack subprocess."""
    out = bench._build_standards_text(
        {"name": "zero", "format": None, "budget": 0}, auto_mem_dir=None
    )
    assert out == "", f"zero arm must return empty string, got: {out!r}"


# ---------------------------------------------------------------------------
# Order assignment — pre-registered force-balanced (15 AB + 15 BA per pair)
# ---------------------------------------------------------------------------


def test_order_assignment_is_balanced_per_pair(bench):
    """Order assignment for N=100 trials produces exactly 50 AB + 50 BA."""
    assignments = bench._assign_orders(n_trials=100)
    assert len(assignments) == 100
    ab_count = sum(1 for o in assignments if o == "AB")
    ba_count = sum(1 for o in assignments if o == "BA")
    assert ab_count == 50, f"expected 50 AB, got {ab_count}"
    assert ba_count == 50, f"expected 50 BA, got {ba_count}"


def test_order_assignment_odd_n_raises(bench):
    """Force-balanced ordering requires even N — odd N must error loudly."""
    with pytest.raises(ValueError, match=r"even"):
        bench._assign_orders(n_trials=31)


# ---------------------------------------------------------------------------
# Pairwise judge JSON parser — happy path + malformed fallback
# ---------------------------------------------------------------------------


def test_pairwise_judge_json_parse_ok(bench):
    """Well-formed Gemini JSON parses to {winner, reasoning}."""
    raw = '{"winner": "A", "reasoning": "Response A flags the bare-except."}'
    parsed = bench._parse_pairwise_response(raw)
    assert parsed["winner"] == "A"
    assert parsed["reasoning"] == "Response A flags the bare-except."


def test_pairwise_judge_json_parse_malformed_returns_tie(bench):
    """Malformed JSON falls back to {winner: 'TIE', reasoning: '<parse_error>'}.

    Graceful degradation: a malformed judge response should NOT poison the run.
    A TIE result is the safest fallback — counted as 0.5 wins each.
    """
    parsed = bench._parse_pairwise_response("not-json-at-all{")
    assert parsed["winner"] == "TIE"
    assert "parse_error" in parsed["reasoning"].lower()


def test_pairwise_judge_unknown_winner_returns_tie(bench):
    """Winner field with unexpected value (e.g. 'C') falls back to TIE."""
    raw = '{"winner": "C", "reasoning": "neither"}'
    parsed = bench._parse_pairwise_response(raw)
    assert parsed["winner"] == "TIE"


# ---------------------------------------------------------------------------
# Aggregator — win-matrix → per-arm win-rate + pairwise
# ---------------------------------------------------------------------------


def test_aggregate_win_rate_three_arms(bench):
    """Synthetic raw_judge records produce correct per-arm + pairwise win-rates.

    Scenario: 30 trials × 3 pairs (zero·tight, zero·prod, tight·prod).
    zero·tight: tight wins every trial (30 B wins, order varies).
    zero·prod:  prod  wins every trial.
    tight·prod: 15 ties, 15 prod wins.

    Expected per-arm win-rates:
      - zero: 0 wins  / 60 games = 0.00
      - tight: 100 wins + 25 (50 ties × 0.5) / 200 = 0.625
      - prod:  100 wins + 75 (50 + 50 ties × 0.5) / 200 = 0.875
    """
    raw_judge = []
    # zero·tight: tight always wins
    for t in range(100):
        order = "AB" if t < 50 else "BA"
        a_arm = "zero" if order == "AB" else "tight"
        b_arm = "tight" if order == "AB" else "zero"
        winner = "B" if a_arm == "zero" else "A"  # tight always wins
        raw_judge.append(
            {
                "trial": t,
                "pair": "zero__vs__tight",
                "order": order,
                "a_arm": a_arm,
                "b_arm": b_arm,
                "winner": winner,
                "reasoning": "stub",
            }
        )
    # zero·prod: prod always wins
    for t in range(100):
        order = "AB" if t < 50 else "BA"
        a_arm = "zero" if order == "AB" else "prod"
        b_arm = "prod" if order == "AB" else "zero"
        winner = "B" if a_arm == "zero" else "A"
        raw_judge.append(
            {
                "trial": t,
                "pair": "zero__vs__prod",
                "order": order,
                "a_arm": a_arm,
                "b_arm": b_arm,
                "winner": winner,
                "reasoning": "stub",
            }
        )
    # tight·prod: 15 ties + 15 prod wins
    for t in range(100):
        order = "AB" if t < 50 else "BA"
        a_arm = "tight" if order == "AB" else "prod"
        b_arm = "prod" if order == "AB" else "tight"
        if t < 50:
            winner = "TIE"
        else:
            winner = "A" if a_arm == "prod" else "B"
        raw_judge.append(
            {
                "trial": t,
                "pair": "tight__vs__prod",
                "order": order,
                "a_arm": a_arm,
                "b_arm": b_arm,
                "winner": winner,
                "reasoning": "stub",
            }
        )

    agg = bench._aggregate(raw_judge, arm_names=["zero", "tight", "prod"])
    per_arm = agg["per_arm"]
    assert per_arm["zero"]["win_rate"] == pytest.approx(0.0, abs=1e-6)
    assert per_arm["tight"]["win_rate"] == pytest.approx(0.625, abs=1e-6)
    assert per_arm["prod"]["win_rate"] == pytest.approx(0.875, abs=1e-6)
    # win/tie/loss counts
    assert per_arm["zero"]["wins"] == 0
    assert per_arm["zero"]["ties"] == 0
    assert per_arm["zero"]["losses"] == 200
    assert per_arm["tight"]["wins"] == 100
    assert per_arm["tight"]["ties"] == 50
    assert per_arm["tight"]["losses"] == 50


def test_aggregate_hypotheses_pass_at_full_dominance(bench):
    """When prod beats zero on 30/30, H1 PASSes (clears 20/30 threshold)."""
    raw_judge = []
    # Only the prod vs zero pair, 30 trials, prod always wins
    for t in range(100):
        order = "AB" if t < 50 else "BA"
        a_arm = "zero" if order == "AB" else "prod"
        b_arm = "prod" if order == "AB" else "zero"
        winner = "B" if a_arm == "zero" else "A"
        raw_judge.append(
            {
                "trial": t,
                "pair": "zero__vs__prod",
                "order": order,
                "a_arm": a_arm,
                "b_arm": b_arm,
                "winner": winner,
                "reasoning": "stub",
            }
        )
    # also need empty data for the other pairs the aggregator expects
    for pair_name, a_arm_default, b_arm_default in [
        ("zero__vs__tight", "zero", "tight"),
        ("tight__vs__prod", "tight", "prod"),
    ]:
        for t in range(100):
            order = "AB" if t < 50 else "BA"
            a_arm = a_arm_default if order == "AB" else b_arm_default
            b_arm = b_arm_default if order == "AB" else a_arm_default
            raw_judge.append(
                {
                    "trial": t,
                    "pair": pair_name,
                    "order": order,
                    "a_arm": a_arm,
                    "b_arm": b_arm,
                    "winner": "TIE",
                    "reasoning": "stub",
                }
            )
    agg = bench._aggregate(raw_judge, arm_names=["zero", "tight", "prod"])
    assert agg["hypotheses"]["H1"] == "PASS", (
        f"H1 should PASS with 100/100 prod wins, got {agg['hypotheses']['H1']}"
    )


def test_aggregate_hypotheses_h2_pass_when_tight_near_prod(bench):
    """H2 PASS when tight win-rate vs prod is within +/-10pp of 50%.

    Scenario: 30 trials, 50 tight wins + 50 prod wins → tight win-rate = 0.50.
    Other pairs filled with TIEs to satisfy the aggregator's expectations.
    """
    raw_judge = []
    for t in range(100):
        order = "AB" if t < 50 else "BA"
        a_arm = "tight" if order == "AB" else "prod"
        b_arm = "prod" if order == "AB" else "tight"
        # Alternate winners so it lands at exactly 50/50
        if t % 2 == 0:
            winner = "A" if a_arm == "tight" else "B"  # tight wins
        else:
            winner = "B" if a_arm == "tight" else "A"  # prod wins
        raw_judge.append(
            {
                "trial": t,
                "pair": "tight__vs__prod",
                "order": order,
                "a_arm": a_arm,
                "b_arm": b_arm,
                "winner": winner,
                "reasoning": "stub",
            }
        )
    for pair_name, a_default, b_default in [
        ("zero__vs__tight", "zero", "tight"),
        ("zero__vs__prod", "zero", "prod"),
    ]:
        for t in range(100):
            order = "AB" if t < 50 else "BA"
            a_arm = a_default if order == "AB" else b_default
            b_arm = b_default if order == "AB" else a_default
            raw_judge.append(
                {
                    "trial": t,
                    "pair": pair_name,
                    "order": order,
                    "a_arm": a_arm,
                    "b_arm": b_arm,
                    "winner": "TIE",
                    "reasoning": "stub",
                }
            )
    agg = bench._aggregate(raw_judge, arm_names=["zero", "tight", "prod"])
    assert agg["hypotheses"]["H2"] == "PASS", (
        f"H2 should PASS at exact 50/50 (within 10pp), got {agg['hypotheses']['H2']}"
    )


def test_aggregate_hypotheses_h2_fail_when_tight_dominates_prod(bench):
    """H2 FAIL when tight win-rate vs prod exceeds +/-10pp of 50%.

    Scenario: tight wins 25/30 vs prod (win-rate ≈ 0.833 — 33pp from 50%).
    The dominance breaks the non-inferiority claim.
    """
    raw_judge = []
    for t in range(100):
        order = "AB" if t < 50 else "BA"
        a_arm = "tight" if order == "AB" else "prod"
        b_arm = "prod" if order == "AB" else "tight"
        if t < 83:
            winner = "A" if a_arm == "tight" else "B"  # tight wins
        else:
            winner = "B" if a_arm == "tight" else "A"  # prod wins
        raw_judge.append(
            {
                "trial": t,
                "pair": "tight__vs__prod",
                "order": order,
                "a_arm": a_arm,
                "b_arm": b_arm,
                "winner": winner,
                "reasoning": "stub",
            }
        )
    for pair_name, a_default, b_default in [
        ("zero__vs__tight", "zero", "tight"),
        ("zero__vs__prod", "zero", "prod"),
    ]:
        for t in range(100):
            order = "AB" if t < 50 else "BA"
            a_arm = a_default if order == "AB" else b_default
            b_arm = b_default if order == "AB" else a_default
            raw_judge.append(
                {
                    "trial": t,
                    "pair": pair_name,
                    "order": order,
                    "a_arm": a_arm,
                    "b_arm": b_arm,
                    "winner": "TIE",
                    "reasoning": "stub",
                }
            )
    agg = bench._aggregate(raw_judge, arm_names=["zero", "tight", "prod"])
    assert agg["hypotheses"]["H2"] == "FAIL", (
        f"H2 should FAIL at 83/100 dominance, got {agg['hypotheses']['H2']}"
    )


def test_aggregate_hypotheses_fail_at_60_pct(bench):
    """At 59/100 (60% — old threshold) H1 FAILs the corrected 67% threshold."""
    raw_judge = []
    # 18 prod wins, 12 zero wins, all in zero__vs__prod pair
    for t in range(100):
        order = "AB" if t < 50 else "BA"
        a_arm = "zero" if order == "AB" else "prod"
        b_arm = "prod" if order == "AB" else "zero"
        if t < 59:
            winner = "B" if a_arm == "zero" else "A"  # prod wins
        else:
            winner = "A" if a_arm == "zero" else "B"  # zero wins
        raw_judge.append(
            {
                "trial": t,
                "pair": "zero__vs__prod",
                "order": order,
                "a_arm": a_arm,
                "b_arm": b_arm,
                "winner": winner,
                "reasoning": "stub",
            }
        )
    for pair_name, a_arm_default, b_arm_default in [
        ("zero__vs__tight", "zero", "tight"),
        ("tight__vs__prod", "tight", "prod"),
    ]:
        for t in range(100):
            order = "AB" if t < 50 else "BA"
            a_arm = a_arm_default if order == "AB" else b_arm_default
            b_arm = b_arm_default if order == "AB" else a_arm_default
            raw_judge.append(
                {
                    "trial": t,
                    "pair": pair_name,
                    "order": order,
                    "a_arm": a_arm,
                    "b_arm": b_arm,
                    "winner": "TIE",
                    "reasoning": "stub",
                }
            )
    agg = bench._aggregate(raw_judge, arm_names=["zero", "tight", "prod"])
    assert agg["hypotheses"]["H1"] == "FAIL", (
        f"H1 should FAIL at 59/100 (p=0.18), got {agg['hypotheses']['H1']}"
    )


# ---------------------------------------------------------------------------
# Frozen-task fetch — gh pr diff invocation
# ---------------------------------------------------------------------------


def test_fetch_pr_diff_invokes_gh_with_correct_args(bench, monkeypatch):
    """_fetch_pr_diff(402) shells out to `gh pr diff 402` and returns stdout."""
    fake_runner = MagicMock()
    fake_runner.return_value = subprocess.CompletedProcess(
        args=["gh", "pr", "diff", "402"],
        returncode=0,
        stdout="diff --git a/x.py b/x.py\n+def y(): pass\n",
        stderr="",
    )
    diff_text = bench._fetch_pr_diff(402, runner=fake_runner)
    assert "diff --git" in diff_text
    assert "def y(): pass" in diff_text
    # Verify the command shape
    call_args = fake_runner.call_args[0][0]
    assert call_args[:4] == ["gh", "pr", "diff", "402"]


def test_fetch_pr_diff_nonzero_exit_raises(bench):
    """gh failure (non-zero exit) raises RuntimeError, doesn't silently return ''."""

    def failing_runner(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=1,
            stdout="",
            stderr="gh: pr not found",
        )

    with pytest.raises(RuntimeError, match=r"gh pr diff"):
        bench._fetch_pr_diff(999999, runner=failing_runner)


# ---------------------------------------------------------------------------
# v2 — rubric loader
# ---------------------------------------------------------------------------


def test_rubric_loader_parses_pr402_fixture(bench):
    """The checked-in fixture loads and has >=8 issues with expected schema."""
    fixture = (
        Path(__file__).resolve().parent.parent
        / "bench"
        / "fixtures"
        / "pr402_rubric.json"
    )
    rubric = bench._load_rubric(fixture)
    assert rubric["pr_number"] == 402
    issues = rubric["issues"]
    assert len(issues) >= 8, f"expected >=8 issues, got {len(issues)}"
    for issue in issues:
        assert "id" in issue and isinstance(issue["id"], str)
        assert "title" in issue and isinstance(issue["title"], str)
        assert "description" in issue and isinstance(issue["description"], str)
        assert issue["category"] in (
            "correctness",
            "security",
            "edge_case",
            "style",
        )


def test_rubric_loader_missing_file_raises(bench, tmp_path):
    """Missing rubric file raises FileNotFoundError, not silent fallback."""
    with pytest.raises(FileNotFoundError):
        bench._load_rubric(tmp_path / "missing.json")


# ---------------------------------------------------------------------------
# v2 — recall judge parser + 5-arm aggregator + H4/H5/H6
# ---------------------------------------------------------------------------


def test_recall_judge_parse_ok(bench):
    """Well-formed Gemini JSON parses to {mentions, evidence}."""
    raw = '{"mentions": true, "evidence": "the response flags bare-except"}'
    parsed = bench._parse_recall_response(raw)
    assert parsed["mentions"] is True
    assert "bare-except" in parsed["evidence"]


def test_recall_judge_parse_malformed_returns_false(bench):
    """Malformed JSON falls back to {mentions: False, evidence: <parse_error>}."""
    parsed = bench._parse_recall_response("not-json{")
    assert parsed["mentions"] is False
    assert "parse_error" in parsed["evidence"].lower()


def test_aggregate_5_arms_smoke(bench):
    """5-arm aggregator returns per_arm shape for all 5 names."""
    raw_judge = []
    arm_names = ["zero", "minimal", "tight", "prod", "bloated"]
    # build 100 trials of TIE for every pair (10 pairs)
    from itertools import combinations

    for a, b in combinations(arm_names, 2):
        for t in range(100):
            order = "AB" if t < 50 else "BA"
            a_arm = a if order == "AB" else b
            b_arm = b if order == "AB" else a
            raw_judge.append(
                {
                    "trial": t,
                    "pair": f"{a}__vs__{b}",
                    "order": order,
                    "a_arm": a_arm,
                    "b_arm": b_arm,
                    "winner": "TIE",
                    "reasoning": "stub",
                }
            )
    agg = bench._aggregate(raw_judge, arm_names=arm_names)
    for name in arm_names:
        assert name in agg["per_arm"], f"missing {name} in per_arm"
        assert agg["per_arm"][name]["win_rate"] == pytest.approx(0.5, abs=1e-6)
    # 10 pairs in pairwise
    assert len(agg["pairwise"]) == 10


def test_hypothesis_h4_bloated_worse_passes_at_60_prod_wins(bench):
    """H4 PASS when prod beats bloated 60/100 (clears 60% threshold)."""
    raw_judge = []
    arm_names = ["zero", "minimal", "tight", "prod", "bloated"]
    from itertools import combinations

    for a, b in combinations(arm_names, 2):
        for t in range(100):
            order = "AB" if t < 50 else "BA"
            a_arm = a if order == "AB" else b
            b_arm = b if order == "AB" else a
            if (a, b) == ("prod", "bloated") or (b, a) == ("prod", "bloated"):
                # prod beats bloated 60 times, ties 0, bloated wins 40 times
                if t < 60:
                    winner = "B" if b_arm == "prod" else "A"  # prod wins
                else:
                    winner = "B" if b_arm == "bloated" else "A"  # bloated wins
            else:
                winner = "TIE"
            raw_judge.append(
                {
                    "trial": t,
                    "pair": f"{a}__vs__{b}",
                    "order": order,
                    "a_arm": a_arm,
                    "b_arm": b_arm,
                    "winner": winner,
                    "reasoning": "stub",
                }
            )
    agg = bench._aggregate(raw_judge, arm_names=arm_names)
    assert agg["hypotheses"]["H4"] == "PASS", (
        f"H4 should PASS at prod 60/100 vs bloated, got {agg['hypotheses']['H4']}"
    )


def test_hypothesis_h5_recall_ci_excludes_zero(bench):
    """H5 PASS when bootstrap CI for (prod-recall − zero-recall) excludes 0."""
    # Build raw_recall: prod=80% recall (8000/10000 mentions), zero=20% recall
    # Per arm: 100 trials × 10 issues = 1000 Bernoulli draws.
    raw_recall = []
    for arm, recall_rate in [("zero", 0.20), ("prod", 0.80)]:
        for t in range(100):
            for i in range(10):
                mentions = (t * 10 + i) % 100 < int(recall_rate * 100)
                raw_recall.append(
                    {
                        "arm": arm,
                        "trial": t,
                        "issue_id": f"issue_{i}",
                        "mentions": mentions,
                        "evidence": "",
                    }
                )
    # Also need other arms; give them median values to satisfy aggregator
    for arm in ("minimal", "tight", "bloated"):
        for t in range(100):
            for i in range(10):
                raw_recall.append(
                    {
                        "arm": arm,
                        "trial": t,
                        "issue_id": f"issue_{i}",
                        "mentions": False,
                        "evidence": "",
                    }
                )
    h5 = bench._evaluate_h5(raw_recall, prod_arm="prod", zero_arm="zero")
    assert h5["verdict"] == "PASS", (
        f"H5 should PASS at prod 80% > zero 20%, got {h5}"
    )
    # CI lower bound should exclude 0
    assert h5["ci95"][0] > 0, f"CI lower bound should be >0, got {h5['ci95']}"


def test_hypothesis_h6_bloated_recall_drop(bench):
    """H6 PASS when (prod - bloated) recall CI lower bound > 10pp."""
    # prod=80%, bloated=50% → diff=30pp, well above 10pp threshold
    raw_recall = []
    for arm, recall_rate in [("prod", 0.80), ("bloated", 0.50)]:
        for t in range(100):
            for i in range(10):
                mentions = (t * 10 + i) % 100 < int(recall_rate * 100)
                raw_recall.append(
                    {
                        "arm": arm,
                        "trial": t,
                        "issue_id": f"issue_{i}",
                        "mentions": mentions,
                        "evidence": "",
                    }
                )
    h6 = bench._evaluate_h6(raw_recall, prod_arm="prod", bloated_arm="bloated")
    assert h6["verdict"] == "PASS", (
        f"H6 should PASS at prod 80% / bloated 50% (30pp gap), got {h6}"
    )
    # The CI on (prod-bloated) lower bound should exceed 10pp (0.10)
    assert h6["ci95"][0] > 0.10, (
        f"CI lower should exceed 10pp threshold, got {h6['ci95']}"
    )


# ---------------------------------------------------------------------------
# v2 — pre-commit padding-leak audit script
# ---------------------------------------------------------------------------


def test_audit_blocks_filename_leak(bench, tmp_path):
    """Audit detects a padding filename appearing in a results file."""
    # Setup synthetic padding + results
    padding = tmp_path / "padding"
    padding.mkdir()
    leak_name = "secret_persona_atom.md"
    (padding / leak_name).write_text(
        "---\nkind: standard\nname: leak\n---\nsecret body\n", encoding="utf-8"
    )
    results = tmp_path / "results.json"
    results.write_text(json.dumps({"some": f"text mentioning {leak_name}"}))
    leaks = bench._scan_for_padding_leaks(
        padding_dir=padding, check_paths=[results]
    )
    assert len(leaks) >= 1, "expected at least one leak detected"
    assert any(leak_name in str(leak) for leak in leaks), (
        f"expected filename '{leak_name}' in leaks: {leaks}"
    )


def test_audit_no_leak_when_clean(bench, tmp_path):
    """Audit returns empty list when no padding content appears anywhere."""
    padding = tmp_path / "padding"
    padding.mkdir()
    (padding / "atom.md").write_text(
        "---\nkind: standard\n---\nzzqxprose\n", encoding="utf-8"
    )
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"data": "completely unrelated"}))
    leaks = bench._scan_for_padding_leaks(
        padding_dir=padding, check_paths=[clean]
    )
    assert leaks == [], f"expected no leaks for clean file, got {leaks}"
