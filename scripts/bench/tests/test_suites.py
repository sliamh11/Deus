"""Tests for the memory, token, and hygiene suite adapters (no real benchmarks)."""
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
        "local_recall": {
            "hits": 8,
            "total": 10,
            "rate": 0.8,
            "mrr": 0.65,
            "ranks": [1, 2, None, 1, 3, 1, 2, 1, None, 1],
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
    assert "local_recall_mrr" in case_ids
    assert "token_efficiency" in case_ids
    assert "pending_accuracy" not in case_ids


def test_memory_internal_mrr_case(patched_mb):
    from scripts.bench.suites.memory import run_memory
    result = run_memory(["--mode", "internal"])
    mrr_case = next(c for c in result.cases if c.case_id == "local_recall_mrr")
    assert abs(mrr_case.score - 0.65) < 1e-6
    assert "ranks" in mrr_case.meta


def test_memory_internal_meta_has_mrr(patched_mb):
    from scripts.bench.suites.memory import run_memory
    result = run_memory(["--mode", "internal"])
    assert "mrr" in result.meta
    assert abs(result.meta["mrr"] - 0.65) < 1e-6


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


def test_token_score_under_budget(patched_harness):
    """tokens_in < budget → score 1.0."""
    from scripts.bench.suites.token import _score
    assert _score(100, 500) == 1.0


def test_token_score_at_budget(patched_harness):
    """tokens_in == budget → score 1.0."""
    from scripts.bench.suites.token import _score
    assert _score(500, 500) == 1.0


def test_token_score_double_budget(patched_harness):
    """tokens_in == 2× budget → score 0.5."""
    from scripts.bench.suites.token import _score
    assert abs(_score(1000, 500) - 0.5) < 1e-9


def test_token_case_meta_includes_budget_and_over_by(patched_harness):
    """Each case meta has 'budget' and 'over_by' keys."""
    from scripts.bench.suites.token import run_token
    result = run_token([])
    for c in result.cases:
        assert "budget" in c.meta, f"case {c.case_id!r} missing 'budget'"
        assert "over_by" in c.meta, f"case {c.case_id!r} missing 'over_by'"
        assert c.meta["over_by"] >= 0


def test_token_case_over_budget_score_fractional(monkeypatch):
    """A scenario over its per-scenario budget scores < 1.0."""
    import scripts.bench.suites.token as tok_suite

    h = _make_harness_mock()
    # Make est_tokens return a value larger than the per-scenario budget.
    # host_cc_session budget = 700; return 1400 → score should be 0.5
    h.SCENARIOS = {"host_cc_session": ["host_claude_md"]}
    h.STATIC_CONTEXT_TARGETS = [("host_claude_md", "CLAUDE.md")]
    h.file_info.side_effect = lambda p: {
        "path": str(p),
        "exists": True,
        "chars": 1400 * 3,  # chars don't matter; est_tokens mocked below
        "lines": 50,
        "est_tokens": 1400,
        "sha256_8": "abcd1234",
    }
    h.est_tokens.side_effect = lambda chars: 1400

    monkeypatch.setitem(sys.modules, "token_bench_harness", h)
    monkeypatch.setattr(tok_suite, "_load_harness", lambda: h)

    result = tok_suite.run_token([])
    assert len(result.cases) == 1
    c = result.cases[0]
    # budget=700, tokens_in=1400 → score=0.5
    assert abs(c.score - 0.5) < 1e-6
    assert c.meta["over_by"] == 700


# ── MRR calculation ───────────────────────────────────────────────────────────

def test_mrr_known_ranks():
    """MRR for [1, 2, None] = (1/1 + 1/2 + 0) / 3."""
    from scripts.bench.suites.hygiene import run_claude_md_hygiene  # noqa: F401 (import check)
    # Verify formula directly against known values
    ranks = [1, 2, None]
    total = len(ranks)
    mrr = sum(1.0 / r for r in ranks if r is not None) / max(1, total)
    assert abs(mrr - (1.0 + 0.5) / 3) < 1e-9


def test_mrr_all_hits_rank_one():
    ranks = [1, 1, 1]
    mrr = sum(1.0 / r for r in ranks if r is not None) / max(1, len(ranks))
    assert abs(mrr - 1.0) < 1e-9


def test_mrr_all_none():
    ranks = [None, None]
    mrr = sum(1.0 / r for r in ranks if r is not None) / max(1, len(ranks))
    assert abs(mrr - 0.0) < 1e-9


# ── Hygiene suite ─────────────────────────────────────────────────────────────

def _make_hygiene_mock(within_limit: bool = True, all_checkbox: bool = True) -> MagicMock:
    mb = MagicMock()
    mb._load_vault_root.return_value = None
    return mb


@pytest.fixture()
def patched_hygiene_mb(monkeypatch, tmp_path):
    mb = MagicMock()
    mb._load_vault_root.return_value = None
    monkeypatch.setitem(sys.modules, "memory_benchmark", mb)
    import scripts.bench.suites.hygiene as hygiene_suite
    monkeypatch.setattr(hygiene_suite, "_load_mb", lambda: mb)

    claude_md = tmp_path / "CLAUDE.md"
    # Valid CLAUDE.md: pending section with checkbox items within limit
    claude_md.write_text(
        "# Main\n\n"
        "## Pending\n"
        "- [x] item one\n"
        "- [ ] item two\n"
    )
    monkeypatch.setattr(
        "scripts.bench.suites.hygiene.run_claude_md_hygiene",
        lambda: {
            "items": 2,
            "within_limit": True,
            "all_checkbox_format": True,
            "issues": [],
        },
    )
    return mb


def test_hygiene_suite_returns_expected_cases(patched_hygiene_mb):
    from scripts.bench.suites.hygiene import run_hygiene
    result = run_hygiene([])
    assert isinstance(result, RunResult)
    assert result.suite == "hygiene"
    case_ids = {c.case_id for c in result.cases}
    assert "pending_items_within_limit" in case_ids
    assert "pending_all_checkbox_format" in case_ids


def test_hygiene_suite_score_both_passed(patched_hygiene_mb):
    from scripts.bench.suites.hygiene import run_hygiene
    result = run_hygiene([])
    assert result.score == 1.0
    for c in result.cases:
        assert c.passed is True


def test_hygiene_suite_score_fails_when_over_limit(monkeypatch):
    import scripts.bench.suites.hygiene as hygiene_suite
    monkeypatch.setattr(
        hygiene_suite,
        "run_claude_md_hygiene",
        lambda: {
            "items": 15,
            "within_limit": False,
            "all_checkbox_format": True,
            "issues": [],
        },
    )
    result = hygiene_suite.run_hygiene([])
    assert result.score == 0.0
    limit_case = next(c for c in result.cases if c.case_id == "pending_items_within_limit")
    assert limit_case.passed is False
    assert limit_case.score == 0.0


def test_hygiene_suite_score_fails_when_bad_format(monkeypatch):
    import scripts.bench.suites.hygiene as hygiene_suite
    monkeypatch.setattr(
        hygiene_suite,
        "run_claude_md_hygiene",
        lambda: {
            "items": 3,
            "within_limit": True,
            "all_checkbox_format": False,
            "issues": ["bad item here"],
        },
    )
    result = hygiene_suite.run_hygiene([])
    assert result.score == 0.0
    fmt_case = next(c for c in result.cases if c.case_id == "pending_all_checkbox_format")
    assert fmt_case.passed is False


def test_hygiene_meta_includes_item_count_and_issues(monkeypatch):
    import scripts.bench.suites.hygiene as hygiene_suite
    monkeypatch.setattr(
        hygiene_suite,
        "run_claude_md_hygiene",
        lambda: {
            "items": 4,
            "within_limit": True,
            "all_checkbox_format": True,
            "issues": [],
        },
    )
    result = hygiene_suite.run_hygiene([])
    assert result.meta["items"] == 4
    assert "issues" in result.meta
