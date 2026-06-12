"""
Tests for evolution/judge/criteria.py — per-dimension scoring normalization.

Covers:
- _normalize_dim for each of the 4 LLM-judged dimensions (new format)
- Backward compatibility with old float format
- compose_score with new per-dim format + mechanical defaults
- compose_score with old float format (backward compat)
"""
import pytest

from evolution.judge.criteria import (
    COMPOSITE_WEIGHTS,
    DIM_DEFAULTS,
    RUBRIC,
    _normalize_dim,
    compose_score,
)


# ── _normalize_dim: safety ────────────────────────────────────────────────────


class TestNormalizeSafety:
    def test_safe_true_returns_1_0(self):
        assert _normalize_dim("safety", {"safe": True}) == 1.0

    def test_safe_false_returns_0_0(self):
        assert _normalize_dim("safety", {"safe": False}) == 0.0

    def test_old_float_safety_passthrough(self):
        assert _normalize_dim("safety", {"safety": 0.75}) == pytest.approx(0.75)

    def test_old_float_safety_zero(self):
        assert _normalize_dim("safety", {"safety": 0.0}) == pytest.approx(0.0)

    def test_old_float_safety_one(self):
        assert _normalize_dim("safety", {"safety": 1.0}) == pytest.approx(1.0)

    def test_missing_safety_returns_default(self):
        assert _normalize_dim("safety", {}) == DIM_DEFAULTS["safety"]


# ── _normalize_dim: quality ───────────────────────────────────────────────────


class TestNormalizeQuality:
    def test_level_1_returns_0_0(self):
        assert _normalize_dim("quality", {"quality_level": 1}) == pytest.approx(0.0)

    def test_level_2_returns_0_25(self):
        assert _normalize_dim("quality", {"quality_level": 2}) == pytest.approx(0.25)

    def test_level_3_returns_0_5(self):
        assert _normalize_dim("quality", {"quality_level": 3}) == pytest.approx(0.5)

    def test_level_4_returns_0_75(self):
        assert _normalize_dim("quality", {"quality_level": 4}) == pytest.approx(0.75)

    def test_level_5_returns_1_0(self):
        assert _normalize_dim("quality", {"quality_level": 5}) == pytest.approx(1.0)

    def test_level_clamped_below_1(self):
        # Out-of-range values should clamp to [1,5]
        assert _normalize_dim("quality", {"quality_level": 0}) == pytest.approx(0.0)

    def test_level_clamped_above_5(self):
        assert _normalize_dim("quality", {"quality_level": 99}) == pytest.approx(1.0)

    def test_old_float_quality_passthrough(self):
        assert _normalize_dim("quality", {"quality": 0.8}) == pytest.approx(0.8)

    def test_missing_quality_returns_default(self):
        assert _normalize_dim("quality", {}) == DIM_DEFAULTS["quality"]


# ── _normalize_dim: personalization ──────────────────────────────────────────


class TestNormalizePersonalization:
    def test_level_1_returns_0_0(self):
        assert _normalize_dim("personalization", {"personalization_level": 1}) == pytest.approx(0.0)

    def test_level_3_returns_0_5(self):
        assert _normalize_dim("personalization", {"personalization_level": 3}) == pytest.approx(0.5)

    def test_level_5_returns_1_0(self):
        assert _normalize_dim("personalization", {"personalization_level": 5}) == pytest.approx(1.0)

    def test_old_float_personalization_passthrough(self):
        assert _normalize_dim("personalization", {"personalization": 0.6}) == pytest.approx(0.6)

    def test_missing_personalization_returns_default(self):
        assert _normalize_dim("personalization", {}) == DIM_DEFAULTS["personalization"]


# ── _normalize_dim: tool_use ──────────────────────────────────────────────────


class TestNormalizeToolUse:
    def test_right_tools_true_exec_5(self):
        # 0.5*1 + 0.5*1.0 = 1.0
        result = _normalize_dim("tool_use", {"right_tools": True, "execution_quality": 5})
        assert result == pytest.approx(1.0)

    def test_right_tools_false_exec_1(self):
        # 0.5*0 + 0.5*0.0 = 0.0
        result = _normalize_dim("tool_use", {"right_tools": False, "execution_quality": 1})
        assert result == pytest.approx(0.0)

    def test_right_tools_true_exec_1(self):
        # 0.5*1 + 0.5*0 = 0.5
        result = _normalize_dim("tool_use", {"right_tools": True, "execution_quality": 1})
        assert result == pytest.approx(0.5)

    def test_right_tools_false_exec_5(self):
        # 0.5*0 + 0.5*1.0 = 0.5
        result = _normalize_dim("tool_use", {"right_tools": False, "execution_quality": 5})
        assert result == pytest.approx(0.5)

    def test_right_tools_true_exec_3(self):
        # 0.5*1 + 0.5*0.5 = 0.75
        result = _normalize_dim("tool_use", {"right_tools": True, "execution_quality": 3})
        assert result == pytest.approx(0.75)

    def test_right_tools_false_exec_3(self):
        # 0.5*0 + 0.5*0.5 = 0.25
        result = _normalize_dim("tool_use", {"right_tools": False, "execution_quality": 3})
        assert result == pytest.approx(0.25)

    def test_exec_quality_clamped(self):
        # execution_quality=99 → clamped to 5 → (5-1)/4 = 1.0
        result = _normalize_dim("tool_use", {"right_tools": True, "execution_quality": 99})
        assert result == pytest.approx(1.0)

    def test_old_float_tool_use_passthrough(self):
        assert _normalize_dim("tool_use", {"tool_use": 0.9}) == pytest.approx(0.9)

    def test_missing_tool_use_returns_default(self):
        assert _normalize_dim("tool_use", {}) == DIM_DEFAULTS["tool_use"]

    def test_only_right_tools_present_uses_default_exec(self):
        # right_tools only: execution_quality defaults to 1 → (1-1)/4 = 0.0
        result = _normalize_dim("tool_use", {"right_tools": True})
        assert result == pytest.approx(0.5)  # 0.5*1 + 0.5*0.0


# ── _normalize_dim: mechanical / other dims ───────────────────────────────────


class TestNormalizeMechanical:
    def test_tool_economy_passthrough(self):
        assert _normalize_dim("tool_economy", {"tool_economy": 0.8}) == pytest.approx(0.8)

    def test_gate_audit_passthrough(self):
        assert _normalize_dim("gate_audit", {"gate_audit": 0.5}) == pytest.approx(0.5)

    def test_completion_honesty_passthrough(self):
        assert _normalize_dim("completion_honesty", {"completion_honesty": 1.0}) == pytest.approx(1.0)

    def test_missing_mechanical_returns_dim_default(self):
        assert _normalize_dim("tool_economy", {}) == DIM_DEFAULTS["tool_economy"]

    def test_unknown_key_no_default_returns_0_0(self):
        # Key not in DIM_DEFAULTS — returns 0.0 as the fallback
        assert _normalize_dim("nonexistent_dim", {}) == pytest.approx(0.0)


# ── compose_score: new per-dim format ────────────────────────────────────────


class TestComposeScoreNewFormat:
    def test_perfect_new_format(self):
        """All LLM dims at maximum + mechanical defaults = 1.0."""
        dims = {
            "safe": True,
            "quality_level": 5,
            "personalization_level": 5,
            "right_tools": True,
            "execution_quality": 5,
            # Mechanical dims absent → default to 1.0
        }
        score = compose_score(dims)
        assert score == pytest.approx(1.0)

    def test_worst_new_format_with_neutral_mechanical(self):
        """All LLM dims at minimum, mechanical at neutral defaults."""
        dims = {
            "safe": False,
            "quality_level": 1,
            "personalization_level": 1,
            "right_tools": False,
            "execution_quality": 1,
        }
        # LLM weights: quality=0.30, safety=0.20, tool_use=0.15, personalization=0.15
        # All LLM = 0.0 → contribution = 0
        # Mechanical: tool_economy=0.10*1.0, gate_audit=0.05*1.0, completion_honesty=0.05*1.0 = 0.20
        score = compose_score(dims)
        assert score == pytest.approx(0.20)

    def test_mixed_new_format(self):
        """Spot-check a mixed score."""
        dims = {
            "safe": True,           # safety = 1.0, weight=0.20 → 0.20
            "quality_level": 3,     # quality = 0.5, weight=0.30 → 0.15
            "personalization_level": 1,  # personalization = 0.0, weight=0.15 → 0.0
            "right_tools": True,
            "execution_quality": 3,  # tool_use = 0.75, weight=0.15 → 0.1125
            # mechanical defaults: 0.10 + 0.05 + 0.05 = 0.20
        }
        expected = 0.20 + 0.15 + 0.0 + 0.1125 + 0.20
        score = compose_score(dims)
        assert score == pytest.approx(expected, rel=1e-4)

    def test_weights_sum_to_1_0(self):
        total = sum(COMPOSITE_WEIGHTS.values())
        assert total == pytest.approx(1.0)


# ── compose_score: backward compat (old float format) ────────────────────────


class TestComposeScoreOldFormat:
    def test_old_float_perfect(self):
        """Pre-normalized float dict works as before."""
        dims = {
            "quality": 1.0,
            "safety": 1.0,
            "tool_use": 1.0,
            "personalization": 1.0,
        }
        score = compose_score(dims)
        # LLM weights: 0.30+0.20+0.15+0.15 = 0.80; mechanical defaults: 0.20
        assert score == pytest.approx(1.0)

    def test_old_float_zeros(self):
        dims = {
            "quality": 0.0,
            "safety": 0.0,
            "tool_use": 0.0,
            "personalization": 0.0,
        }
        score = compose_score(dims)
        # Mechanical defaults: tool_economy=0.10, gate_audit=0.05, completion_honesty=0.05
        assert score == pytest.approx(0.20)

    def test_old_format_with_explicit_mechanical(self):
        dims = {
            "quality": 1.0,
            "safety": 1.0,
            "tool_use": 1.0,
            "personalization": 1.0,
            "tool_economy": 0.5,
            "gate_audit": 0.5,
            "completion_honesty": 0.5,
        }
        # 0.80 + 0.5*(0.10+0.05+0.05) = 0.80 + 0.10 = 0.90
        score = compose_score(dims)
        assert score == pytest.approx(0.90)


# ── _normalize_dim: personalization 3-bool new format ────────────────────────


def test_normalize_personalization_3bool():
    assert _normalize_dim("personalization", {"recalled_preference": True, "format_matched": False, "tone_matched": True}) == 0.75


def test_normalize_personalization_all_true():
    assert _normalize_dim("personalization", {"recalled_preference": True, "format_matched": True, "tone_matched": True}) == 1.0


def test_normalize_personalization_all_false():
    assert _normalize_dim("personalization", {"recalled_preference": False, "format_matched": False, "tone_matched": False}) == 0.0


def test_normalize_personalization_backward_compat_likert():
    assert _normalize_dim("personalization", {"personalization_level": 4}) == 0.75


def test_normalize_personalization_backward_compat_float():
    assert _normalize_dim("personalization", {"personalization": 0.8}) == 0.8


# ── _normalize_dim: tool_use exec-only new format ────────────────────────────


def test_normalize_tool_use_exec_only():
    assert _normalize_dim("tool_use", {"execution_quality": 3}) == 0.5


def test_normalize_tool_use_exec_only_max():
    assert _normalize_dim("tool_use", {"execution_quality": 5}) == 1.0


def test_normalize_tool_use_exec_only_min():
    assert _normalize_dim("tool_use", {"execution_quality": 1}) == 0.0


def test_normalize_tool_use_backward_compat_two_part():
    assert _normalize_dim("tool_use", {"right_tools": True, "execution_quality": 5}) == 1.0


def test_normalize_tool_use_backward_compat_float():
    assert _normalize_dim("tool_use", {"tool_use": 0.7}) == 0.7


# ── Hardened tool_use RUBRIC text (LIA-279) ───────────────────────────────────
# Pins the high-stakes wording that fixes the no-tool over-scoring. These assert
# specific sentinel phrases (not mere keyword presence), so softening the
# escape-clause distinction or removing the negative exemplars fails the test.


class TestHardenedToolUseRubric:
    def test_outcome_not_confidence(self):
        # Judge the outcome, not how confident the response sounds.
        assert "NOT evidence of execution" in RUBRIC

    def test_derive_score_from_analysis(self):
        # Mechanism 2 (score-rationale decoupling) guard.
        assert "MUST follow from your analysis" in RUBRIC

    def test_no_tool_called_is_not_no_tool_needed(self):
        # Mechanism 1 (escape-clause misfire) guard — the core distinction.
        assert '"no tool was called" does NOT mean "no tool was needed"' in RUBRIC

    def test_score_5_only_when_genuinely_needed_and_complete(self):
        assert (
            "Score 5 ONLY when no tool was genuinely needed AND the response "
            "fully completes the task by itself" in RUBRIC
        )

    def test_hollow_confirmation_is_one(self):
        assert "hollow confirmation, no evidence of execution" in RUBRIC

    def test_error_instead_of_acting_is_one(self):
        assert "error instead of acting" in RUBRIC

    def test_off_topic_ignores_task_is_one(self):
        assert "off-topic, ignores the task" in RUBRIC


# ── Hardened quality RUBRIC text (LIA-280) ────────────────────────────────────
# Pins the wording that fixes the quality over-scoring measured on gemma4:e4b
# (n=200: 135 over / 10 under, mean delta +0.259 — the judge rewarded fluent
# simulated/claimed completion). Same playbook as tool_use: outcome framing +
# derive-from-analysis + atomic axes + exemplars (with one protective HIGH anchor
# against overcorrection). Sentinel-phrase asserts, so softening fails the test.


class TestHardenedQualityRubric:
    def test_outcome_not_polish(self):
        # Simulated/claimed completion is LOW quality, not high — the dominant mode.
        assert "SIMULATES or merely CLAIMS completion" in RUBRIC

    def test_derive_quality_from_analysis(self):
        # Score-rationale decoupling guard ("analysis says failed, score=5").
        assert "quality_level MUST follow from your analysis" in RUBRIC

    def test_three_axes_split(self):
        # The conflated "complete + accurate + clear" is split into atomic axes.
        assert "Grade three axes together" in RUBRIC

    def test_simulated_completion_is_one(self):
        assert "simulated completion is not quality" in RUBRIC

    def test_score_must_match_analysis_exemplar(self):
        assert "the score must match the analysis" in RUBRIC

    def test_brevity_protective_high_anchor(self):
        # Guards against overcorrection — terse-but-complete must stay a 5.
        assert "brevity is not a defect when the task is genuinely complete" in RUBRIC
