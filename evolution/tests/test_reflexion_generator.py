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
