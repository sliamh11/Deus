"""Tests for LIA-151: excluding the noisy tool_selection judge objective.

The exclusion guard sits at the top of optimize(), ABOVE _require_dspy(), so an
excluded module short-circuits to None without importing dspy. That placement is
what makes these tests runnable in CI (which has no dspy) — they assert the
excluded path returns None and the non-excluded path proceeds far enough to hit
the dspy requirement and raise ImportError.
"""
import pytest

from evolution.optimizer.dspy_optimizer import _JUDGE_OPTIMIZATION_EXCLUDED, optimize


def test_tool_selection_is_excluded():
    """tool_selection is in the denylist with a non-empty reason."""
    assert "tool_selection" in _JUDGE_OPTIMIZATION_EXCLUDED
    assert _JUDGE_OPTIMIZATION_EXCLUDED["tool_selection"].strip()


def test_valid_modules_not_excluded():
    """qa and summarization have real objectives and must NOT be excluded."""
    assert "qa" not in _JUDGE_OPTIMIZATION_EXCLUDED
    assert "summarization" not in _JUDGE_OPTIMIZATION_EXCLUDED


def test_optimize_tool_selection_returns_none_without_dspy():
    """The guard short-circuits before _require_dspy(), so optimize() returns
    None for tool_selection even though dspy is not installed. If the guard were
    placed below _require_dspy(), this would raise ImportError instead."""
    assert optimize(module="tool_selection") is None


def test_optimize_tool_selection_skips_require_dspy(monkeypatch):
    """Prove the chokepoint: an excluded module never reaches _require_dspy()."""
    import evolution.optimizer.dspy_optimizer as opt

    def _boom():
        raise AssertionError("_require_dspy() must not be called for an excluded module")

    monkeypatch.setattr(opt, "_require_dspy", _boom)
    assert optimize(module="tool_selection") is None


def test_optimize_qa_is_not_short_circuited():
    """A non-excluded module proceeds past the guard to _require_dspy(), which
    raises ImportError when dspy is absent (verified: _require_dspy raises
    ImportError exclusively, modules.py). Asserting the exact class — not a bare
    'raises' — keeps the test precise against future _require_dspy changes.

    Skipped if dspy IS installed (then qa would proceed further and likely fail
    on missing samples/judge instead)."""
    try:
        import dspy  # noqa: F401
        pytest.skip("dspy installed — qa would proceed past _require_dspy()")
    except ImportError:
        pass
    with pytest.raises(ImportError):
        optimize(module="qa")
