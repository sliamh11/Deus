"""Tests for the memory and token suite adapters (no real benchmarks)."""
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from scripts.bench.types import RunResult


# ── Memory adapter ────────────────────────────────────────────────────────────

def _make_mb_mock() -> MagicMock:
    mb = MagicMock()
    mb.run_outbound.return_value = {
        "mode": "outbound",
        "n": 10,
        "ks": [1, 3, 5, 10],
        "recall": {1: 0.3, 3: 0.6, 5: 0.8, 10: 0.9},
        "mrr": 0.55,
        "total_time_s": 5.0,
        "per_example_s": 0.5,
    }
    mb.run_internal.return_value = {
        "mode": "internal",
        "token_efficiency": {
            "full_chars": 1000,
            "compact_chars": 700,
            "reduction_pct": 30.0,
            "sessions": 5,
        },
        "local_recall": {"hits": 8, "total": 10, "rate": 0.8},
        "pending_accuracy": {
            "items": 3,
            "within_limit": True,
            "all_checkbox_format": True,
            "issues": [],
        },
    }
    return mb


@pytest.fixture()
def patched_mb(monkeypatch):
    mb = _make_mb_mock()
    monkeypatch.setitem(sys.modules, "memory_benchmark", mb)
    # Force reload of the suite module so it picks up the mock
    import scripts.bench.suites.memory as mem_suite
    monkeypatch.setattr(mem_suite, "_load_mb", lambda: mb)
    return mb


def test_memory_outbound_score(patched_mb):
    from scripts.bench.suites.memory import run_memory
    result = run_memory(["--mode", "outbound", "--limit", "10"])
    assert isinstance(result, RunResult)
    assert result.suite == "memory"
    assert abs(result.score - 0.8) < 1e-6   # recall@5


def test_memory_outbound_cases(patched_mb):
    from scripts.bench.suites.memory import run_memory
    result = run_memory(["--mode", "outbound", "--limit", "10"])
    case_ids = {c.case_id for c in result.cases}
    assert "recall_at_5" in case_ids


def test_memory_internal_score(patched_mb):
    from scripts.bench.suites.memory import run_memory
    result = run_memory(["--mode", "internal"])
    assert isinstance(result, RunResult)
    assert abs(result.score - 0.8) < 1e-6   # local_recall.rate


def test_memory_internal_cases(patched_mb):
    from scripts.bench.suites.memory import run_memory
    result = run_memory(["--mode", "internal"])
    case_ids = {c.case_id for c in result.cases}
    assert "local_recall" in case_ids
    assert "token_efficiency" in case_ids
    assert "pending_accuracy" in case_ids


# ── Token adapter ─────────────────────────────────────────────────────────────

def _make_harness_mock() -> MagicMock:
    h = MagicMock()
    h.CHARS_PER_TOKEN = 3.7
    h.REPO = Path("/fake/repo")
    h.STATIC_CONTEXT_TARGETS = [
        ("host_claude_md", "CLAUDE.md"),
        ("global_template", "groups/global/CLAUDE.md.template"),
    ]
    h.SCENARIOS = {
        "host_cc_session": ["host_claude_md"],
        "container_whatsapp_main_turn1": ["global_template"],
    }
    h.file_info.side_effect = lambda p: {
        "path": str(p),
        "exists": True,
        "chars": 1000,
        "lines": 50,
        "est_tokens": 270,
        "sha256_8": "abcd1234",
    }
    h.est_tokens.side_effect = lambda chars: round(chars / 3.7)
    return h


@pytest.fixture()
def patched_harness(monkeypatch):
    h = _make_harness_mock()
    monkeypatch.setitem(sys.modules, "token_bench_harness", h)
    import scripts.bench.suites.token as tok_suite
    monkeypatch.setattr(tok_suite, "_load_harness", lambda: h)
    return h


def test_token_suite_score(patched_harness):
    from scripts.bench.suites.token import run_token
    result = run_token([])
    assert isinstance(result, RunResult)
    assert result.suite == "token"
    assert result.score == 1.0


def test_token_suite_cases(patched_harness):
    from scripts.bench.suites.token import run_token
    result = run_token([])
    assert len(result.cases) == 2
    case_ids = {c.case_id for c in result.cases}
    assert "host_cc_session" in case_ids
    assert "container_whatsapp_main_turn1" in case_ids


def test_token_suite_tokens_in(patched_harness):
    from scripts.bench.suites.token import run_token
    result = run_token([])
    assert result.tokens_in > 0
    # Each scenario: 1000 chars / 3.7 ≈ 270 tokens; 2 scenarios = 540
    assert result.tokens_in == sum(c.tokens_in for c in result.cases)


def test_token_suite_case_meta(patched_harness):
    from scripts.bench.suites.token import run_token
    result = run_token([])
    for c in result.cases:
        assert "chars" in c.meta
        assert "components" in c.meta
