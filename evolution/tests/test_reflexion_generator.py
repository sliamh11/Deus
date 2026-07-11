"""Tests for evolution/reflexion/generator.py category extraction."""
import pytest

from evolution.reflexion.generator import _extract_category, _extract_positive_category


@pytest.mark.parametrize(
    "text, expected",
    [
        ("- Category: style", "style"),
        ("- Category: tool_use", "tool_use"),
        ("- Category: safety", "safety"),
        ("- Category: reasoning", "reasoning"),
        # Body text mentioning other categories should not hijack the result
        (
            "- What went wrong: The model failed to reason about the user's style.\n"
            "- Next time: Follow formatting preferences.\n"
            "- Category: style",
            "style",
        ),
        # Multi-category LLM output -- regex picks the Category line value
        (
            "- What went wrong: tool_use and reasoning issues.\n"
            "- Category: style",
            "style",
        ),
        # Whitespace variations
        ("-  Category:  style", "style"),
        ("- Category:   tool_use", "tool_use"),
        # Missing category line falls back to reasoning
        ("No category line here at all", "reasoning"),
        # Unknown category value falls back to reasoning
        ("- Category: unknown_cat", "reasoning"),
    ],
)
def test_extract_category(text: str, expected: str) -> None:
    assert _extract_category(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("- Category: style", "style"),
        ("- Category: tool_use", "tool_use"),
        ("- Category: reasoning", "reasoning"),
        ("- Category: positive_pattern", "positive_pattern"),
        # Missing line falls back to positive_pattern
        ("No category line", "positive_pattern"),
        # Unknown value falls back to positive_pattern
        ("- Category: safety", "positive_pattern"),
    ],
)
def test_extract_positive_category(text: str, expected: str) -> None:
    assert _extract_positive_category(text) == expected


# ── Metrics in the prompt template ────────────────────────────────────────────


@pytest.fixture
def capture_prompt(monkeypatch):
    """Replace the LLM call with a capture that returns a fixed reflection."""
    captured = []

    def fake_generate(prompt, **kwargs):
        captured.append(prompt)
        return "- What went wrong: x\n- Next time: y\n- Category: reasoning"

    monkeypatch.setattr("evolution.reflexion.generator.generate", fake_generate)
    return captured


def test_generate_reflection_includes_metrics(capture_prompt):
    from evolution.reflexion.generator import generate_reflection

    generate_reflection(
        prompt="p", response="r", score=0.3,
        metrics={"tests_failed": 2, "breaks": ["regression"]},
    )
    assert 'Task metrics: {"tests_failed": 2, "breaks": ["regression"]}' in capture_prompt[0]


def test_generate_reflection_without_metrics_has_no_section(capture_prompt):
    from evolution.reflexion.generator import generate_reflection

    generate_reflection(prompt="p", response="r", score=0.3)
    assert "Task metrics" not in capture_prompt[0]


def test_generate_positive_reflection_includes_metrics(capture_prompt):
    from evolution.reflexion.generator import generate_positive_reflection

    generate_positive_reflection(
        prompt="p", response="r", score=0.9,
        metrics={"tests_passed": 12},
    )
    assert 'Task metrics: {"tests_passed": 12}' in capture_prompt[0]


# ── Model default pass-through (issue #1006) ─────────────────────────────────


@pytest.fixture
def capture_model(monkeypatch):
    """Replace the LLM call with a capture that records the model kwarg."""
    captured = []

    def fake_generate(prompt, **kwargs):
        captured.append(kwargs.get("model", "MISSING"))
        return "- What went wrong: x\n- Next time: y\n- Category: reasoning"

    monkeypatch.setattr("evolution.reflexion.generator.generate", fake_generate)
    return captured


def test_generate_reflection_default_model_is_none(capture_model):
    """Regression #1006: no Gemini model id may leak to non-Gemini providers."""
    from evolution.reflexion.generator import generate_reflection

    generate_reflection(prompt="p", response="r", score=0.3)
    assert capture_model == [None]


def test_generate_positive_reflection_default_model_is_none(capture_model):
    from evolution.reflexion.generator import generate_positive_reflection

    generate_positive_reflection(prompt="p", response="r", score=0.9)
    assert capture_model == [None]


def test_generate_reflection_explicit_model_passes_through(capture_model):
    from evolution.reflexion.generator import generate_reflection

    generate_reflection(prompt="p", response="r", score=0.3, model="my-model")
    assert capture_model == ["my-model"]


def test_extract_principles_default_model_is_none(monkeypatch):
    """Regression #1006: extract_principles shares the same generate() path and
    must not default to a Gemini model id either."""
    import evolution.reflexion.principles as principles_mod

    captured = []

    def fake_generate(prompt, **kwargs):
        captured.append(kwargs.get("model", "MISSING"))
        return "1. Always test the fallthrough path with a mocked provider chain."

    fake_row = {"prompt": "p", "response": "r", "judge_score": 0.9}
    monkeypatch.setattr(principles_mod, "generate", fake_generate)
    monkeypatch.setattr(principles_mod, "get_recent", lambda **kw: [dict(fake_row)] * 3)
    monkeypatch.setattr(principles_mod, "save_reflection", lambda **kw: None)
    monkeypatch.setattr(principles_mod, "_record_extraction", lambda *a, **kw: None)

    result = principles_mod.extract_principles(force=True)

    assert result is not None
    assert captured == [None]
