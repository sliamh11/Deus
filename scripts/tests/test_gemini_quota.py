"""Tests for scripts/_gemini_quota.py — the shared Gemini quota-error predicate.

Extracted from duplicated `"429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)`
checks across drift_check.py, trec_atom_benchmark.py, memory_indexer.py, and
the two bench judge scripts. This test only covers the predicate itself.
"""
from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _gemini_quota import is_quota_error


def test_matches_429():
    assert is_quota_error(Exception("429 Too Many Requests")) is True


def test_matches_resource_exhausted():
    assert is_quota_error(Exception("RESOURCE_EXHAUSTED: quota exceeded")) is True


def test_matches_both_markers_present():
    assert is_quota_error(Exception("429 RESOURCE_EXHAUSTED PerDay limit")) is True


def test_does_not_match_unrelated_error():
    assert is_quota_error(Exception("500 Internal Server Error")) is False


def test_does_not_match_empty_message():
    assert is_quota_error(Exception("")) is False


def test_does_not_match_connection_error():
    assert is_quota_error(ConnectionError("connection reset by peer")) is False
