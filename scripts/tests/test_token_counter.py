"""Unit tests for evolution/token_counter.py."""
import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from evolution.token_counter import estimate_tokens, sum_tokens


def test_estimate_tokens_empty():
    assert estimate_tokens("") == 0


def test_estimate_tokens_basic():
    # 4 chars → 1 token (floor division)
    assert estimate_tokens("abcd") == 1


def test_estimate_tokens_longer():
    text = "a" * 100
    assert estimate_tokens(text) == 25


def test_estimate_tokens_unicode():
    # Each char counts as one regardless of byte length; heuristic is len()-based
    text = "שלום"  # 4 chars
    assert estimate_tokens(text) == 1


def test_sum_tokens_empty():
    assert sum_tokens() == 0


def test_sum_tokens_single():
    assert sum_tokens("abcd") == 1


def test_sum_tokens_multiple():
    # "aaaa" = 1, "bbbbbbbb" = 2
    assert sum_tokens("aaaa", "bbbbbbbb") == 3


def test_sum_tokens_with_empty_part():
    assert sum_tokens("abcd", "", "efgh") == 2
