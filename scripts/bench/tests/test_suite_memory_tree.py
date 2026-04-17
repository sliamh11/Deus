"""Tests for scripts/bench/suites/memory_tree.py adapter."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts.bench.types import RunResult


# ── Helpers ───────────────────────────────────────────────────────────────────

_FIXTURE_ITEMS: list[dict] = [
    {
        "query": "who do I live with",
        "expected_path": "Persona/life/household.md",
        "expected_paths": ["Persona/life/household.md"],
        "tag": "single",
    },
    {
        "query": "my favorite directors and movies",
        "expected_path": "Persona/taste/movies.md",
        "expected_paths": ["Persona/taste/movies.md"],
        "tag": "single",
    },
    {
        "query": "how to cook a chocolate souffle",
        "abstain": True,
        "tag": "abstain-far",
    },
]


def _make_mt_mock(
    policy_outcomes: dict[str, dict],
    raw_outcomes: dict[str, dict] | None = None,
) -> MagicMock:
    """Return a mock memory_tree module.

    policy_outcomes and raw_outcomes map query → {results, fell_back, confidence}.
    """
    raw_outcomes = raw_outcomes or policy_outcomes

    def _make_result(outcomes: dict, query: str) -> dict:
        o = outcomes.get(query, {"results": [], "fell_back": True, "confidence": 0.0})
        return {
            "results": o.get("results", []),
            "fell_back": o.get("fell_back", False),
            "confidence": o.get("confidence", 0.0),
            "trace": ["stub"],
            "policy_trace": ["stub"],
        }

    mt = MagicMock()
    mt.open_db.return_value = MagicMock()
    mt.retrieve_with_policy.side_effect = lambda db, q, **kw: _make_result(policy_outcomes, q)
    mt.retrieve.side_effect = lambda db, q, **kw: _make_result(raw_outcomes, q)
    return mt


def _inject_mt(monkeypatch, mt: MagicMock) -> None:
    monkeypatch.setitem(sys.modules, "memory_tree", mt)
    import scripts.bench.suites.memory_tree as suite_mod
    monkeypatch.setattr(suite_mod, "_load_mt", lambda: mt)


def _write_dataset(tmp_path: Path, items: list[dict]) -> Path:
    p = tmp_path / "dataset.jsonl"
    p.write_text("\n".join(json.dumps(i) for i in items))
    return p


# ── Tests — RunResult shape ───────────────────────────────────────────────────

class TestRunResultShape:
    def test_returns_run_result(self, tmp_path, monkeypatch):
        mt = _make_mt_mock({
            "who do I live with": {"results": [{"path": "Persona/life/household.md"}], "fell_back": False, "confidence": 0.8},
            "my favorite directors and movies": {"results": [{"path": "Persona/taste/movies.md"}], "fell_back": False, "confidence": 0.7},
            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        assert isinstance(result, RunResult)
        assert result.suite == "memory_tree"
        assert 0.0 <= result.score <= 1.0

    def test_score_matches_hit_rate(self, tmp_path, monkeypatch):
        """All three items hit → score 1.0."""
        mt = _make_mt_mock({
            "who do I live with": {"results": [{"path": "Persona/life/household.md"}], "fell_back": False, "confidence": 0.8},
            "my favorite directors and movies": {"results": [{"path": "Persona/taste/movies.md"}], "fell_back": False, "confidence": 0.7},
            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        assert abs(result.score - 1.0) < 1e-9

    def test_score_partial_hits(self, tmp_path, monkeypatch):
        """First item misses, second hits, abstain item hits → 2/3."""
        mt = _make_mt_mock({
            "who do I live with": {"results": [{"path": "wrong.md"}], "fell_back": False, "confidence": 0.6},
            "my favorite directors and movies": {"results": [{"path": "Persona/taste/movies.md"}], "fell_back": False, "confidence": 0.7},
            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        assert abs(result.score - 2 / 3) < 1e-9

    def test_one_case_per_item(self, tmp_path, monkeypatch):
        mt = _make_mt_mock({
            "who do I live with": {"results": [], "fell_back": True, "confidence": 0.1},
            "my favorite directors and movies": {"results": [], "fell_back": True, "confidence": 0.1},
            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        assert len(result.cases) == 3

    def test_case_meta_fields(self, tmp_path, monkeypatch):
        mt = _make_mt_mock({
            "who do I live with": {
                "results": [{"path": "Persona/life/household.md"}],
                "fell_back": False, "confidence": 0.8,
            },
            "my favorite directors and movies": {"results": [], "fell_back": True, "confidence": 0.1},
            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        for c in result.cases:
            assert "abstained" in c.meta
            assert "top_path" in c.meta
            assert "expected_path" in c.meta

    def test_suite_meta_fields(self, tmp_path, monkeypatch):
        mt = _make_mt_mock({
            "who do I live with": {"results": [], "fell_back": True, "confidence": 0.1},
            "my favorite directors and movies": {"results": [], "fell_back": True, "confidence": 0.1},
            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        assert "policy" in result.meta
        assert "n" in result.meta
        assert "abstain_count" in result.meta
        assert result.meta["n"] == 3


# ── Tests — policy vs no-policy dispatch ─────────────────────────────────────

class TestPolicyDispatch:
    def test_policy_calls_retrieve_with_policy(self, tmp_path, monkeypatch):
        mt = _make_mt_mock({
            "who do I live with": {"results": [], "fell_back": True, "confidence": 0.1},
            "my favorite directors and movies": {"results": [], "fell_back": True, "confidence": 0.1},
            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        run_memory_tree(["--policy", "--dataset", str(dataset_path)])

        assert mt.retrieve_with_policy.called
        assert not mt.retrieve.called

    def test_no_policy_calls_retrieve(self, tmp_path, monkeypatch):
        mt = _make_mt_mock(
            policy_outcomes={},
            raw_outcomes={
                "who do I live with": {"results": [], "fell_back": True, "confidence": 0.1},
                "my favorite directors and movies": {"results": [], "fell_back": True, "confidence": 0.1},
                "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
            },
        )
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        run_memory_tree(["--no-policy", "--dataset", str(dataset_path)])

        assert mt.retrieve.called
        assert not mt.retrieve_with_policy.called

    def test_policy_flag_stored_in_meta(self, tmp_path, monkeypatch):
        mt = _make_mt_mock({"who do I live with": {"results": [], "fell_back": True, "confidence": 0.1},
                            "my favorite directors and movies": {"results": [], "fell_back": True, "confidence": 0.1},
                            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1}})
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        r_policy = run_memory_tree(["--policy", "--dataset", str(dataset_path)])
        r_raw = run_memory_tree(["--no-policy", "--dataset", str(dataset_path)])

        assert r_policy.meta["policy"] is True
        assert r_raw.meta["policy"] is False

    def test_default_is_no_policy(self, tmp_path, monkeypatch):
        mt = _make_mt_mock(
            policy_outcomes={},
            raw_outcomes={
                "who do I live with": {"results": [], "fell_back": True, "confidence": 0.1},
                "my favorite directors and movies": {"results": [], "fell_back": True, "confidence": 0.1},
                "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
            },
        )
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        assert mt.retrieve.called
        assert not mt.retrieve_with_policy.called
        assert result.meta["policy"] is False


# ── Tests — abstentions ───────────────────────────────────────────────────────

class TestAbstentions:
    def test_abstain_item_scores_1_when_fell_back(self, tmp_path, monkeypatch):
        mt = _make_mt_mock({
            "who do I live with": {"results": [{"path": "Persona/life/household.md"}], "fell_back": False, "confidence": 0.8},
            "my favorite directors and movies": {"results": [{"path": "Persona/taste/movies.md"}], "fell_back": False, "confidence": 0.7},
            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        abstain_case = next(
            c for c in result.cases
            if c.meta.get("expected_path") is None
        )
        assert abstain_case.score == 1.0
        assert abstain_case.passed is True

    def test_abstain_item_scores_0_when_not_fell_back(self, tmp_path, monkeypatch):
        """Abstain item where model returned results (leak) scores 0."""
        mt = _make_mt_mock({
            "who do I live with": {"results": [{"path": "Persona/life/household.md"}], "fell_back": False, "confidence": 0.8},
            "my favorite directors and movies": {"results": [{"path": "Persona/taste/movies.md"}], "fell_back": False, "confidence": 0.7},
            "how to cook a chocolate souffle": {
                "results": [{"path": "some.md"}], "fell_back": False, "confidence": 0.6
            },
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        abstain_case = next(
            c for c in result.cases
            if c.meta.get("expected_path") is None
        )
        assert abstain_case.score == 0.0
        assert abstain_case.passed is False

    def test_non_abstain_miss_records_abstained_true(self, tmp_path, monkeypatch):
        """Non-abstain item where retriever fell_back → abstained=True in meta."""
        mt = _make_mt_mock({
            "who do I live with": {"results": [], "fell_back": True, "confidence": 0.1},
            "my favorite directors and movies": {"results": [{"path": "Persona/taste/movies.md"}], "fell_back": False, "confidence": 0.7},
            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        household_case = next(
            c for c in result.cases
            if c.meta.get("expected_path") == "Persona/life/household.md"
        )
        assert household_case.meta["abstained"] is True
        assert result.meta["abstain_count"] == 1

    def test_abstain_count_in_meta(self, tmp_path, monkeypatch):
        """abstain_count counts unexpected abstentions (non-abstain items that fell_back)."""
        mt = _make_mt_mock({
            "who do I live with": {"results": [], "fell_back": True, "confidence": 0.1},
            "my favorite directors and movies": {"results": [], "fell_back": True, "confidence": 0.1},
            "how to cook a chocolate souffle": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--dataset", str(dataset_path)])

        assert result.meta["abstain_count"] == 2


# ── Tests — limit arg ─────────────────────────────────────────────────────────

class TestLimitArg:
    def test_limit_truncates_dataset(self, tmp_path, monkeypatch):
        mt = _make_mt_mock({
            "who do I live with": {"results": [], "fell_back": True, "confidence": 0.1},
        })
        _inject_mt(monkeypatch, mt)
        dataset_path = _write_dataset(tmp_path, _FIXTURE_ITEMS)

        from scripts.bench.suites.memory_tree import run_memory_tree
        result = run_memory_tree(["--limit", "1", "--dataset", str(dataset_path)])

        assert len(result.cases) == 1
        assert result.meta["n"] == 1
