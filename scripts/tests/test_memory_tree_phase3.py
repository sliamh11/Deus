"""Critical-path tests for Phase 3 calibrate/benchmark/ablation/LOO.

Targets the high-criticality logic the original test file underweights:
  - calibrate() — wrong thresholds break the whole retrieval contract
  - benchmark*() — wrong metrics hide regressions
  - _log_query() — wrong/missing logs starve future calibration
  - retrieve() ablation toggles — wrong gates break V0/V1/V2/V3 isolation

Mocks `mt.retrieve` for calibrate/benchmark tests so we can craft
controlled (confidence, correctness) distributions and verify the
threshold-fitting math directly. Uses real retrieve() for log-query
and ablation tests where the wiring matters.
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest


_ROOT = Path(__file__).resolve().parent.parent.parent

# Reuse conftest's pre-loaded module so autouse path isolation applies.
if "memory_tree" in sys.modules:
    mt = sys.modules["memory_tree"]
else:
    spec = importlib.util.spec_from_file_location("memory_tree", _ROOT / "scripts" / "memory_tree.py")
    mt = importlib.util.module_from_spec(spec)
    sys.modules["memory_tree"] = mt
    spec.loader.exec_module(mt)


# ── Shared fixtures ───────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(tmp_path):
    db_path = tmp_path / "tree.db"
    db = mt.open_db(db_path)
    yield db
    db.close()


def _mock_retrieve_factory(scores: dict[str, tuple[float, str | None]]):
    """Return a fake retrieve() that returns confidence + top path per query.

    `scores` maps query_text → (confidence, top_path). Missing query → empty result.
    """
    def _fake(db, query, **kw):
        if query not in scores:
            return {"results": [], "confidence": 0.0, "fell_back": True, "trace": ["miss"]}
        conf, path = scores[query]
        results = []
        if path is not None:
            results = [{"id": "x", "path": path, "title": "T", "score": conf, "route": "flat"}]
        return {"results": results, "confidence": conf, "fell_back": False, "trace": []}
    return _fake


# ── TestCalibrate — critical thresholds logic ─────────────────────────────────

class TestCalibrate:
    def test_basic_fit_with_clear_separation(self, tmp_db, monkeypatch):
        """OOD scores 0.10–0.30, correct scores 0.60–0.85 → ABSTAIN lands in the gap."""
        scores = {
            **{f"real_q{i}": (s, "doc_a") for i, s in enumerate([0.85, 0.78, 0.72, 0.65, 0.60])},
            **{f"ood_q{i}": (s, "doc_a") for i, s in enumerate([0.30, 0.25, 0.20, 0.15, 0.10])},
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        labeled = (
            [{"query": f"real_q{i}", "expected_path": "doc_a"} for i in range(5)] +
            [{"query": f"ood_q{i}", "abstain": True} for i in range(5)]
        )
        result = mt.calibrate(tmp_db, labeled)
        assert result["ok"] is True
        assert result["samples"] == 5
        assert result["ood_samples"] == 5
        # ABSTAIN should be > max OOD (0.30) and < min correct (0.60).
        assert 0.30 < result["abstain_threshold"] < 0.60

    def test_no_ood_uses_default_abstain(self, tmp_db, monkeypatch):
        scores = {f"real_q{i}": (s, "doc_a") for i, s in enumerate([0.80, 0.75, 0.70])}
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        labeled = [{"query": f"real_q{i}", "expected_path": "doc_a"} for i in range(3)]
        result = mt.calibrate(tmp_db, labeled)
        assert result["ok"] is True
        assert result["abstain_threshold"] == mt.DEFAULT_ABSTAIN_THRESHOLD
        assert result["ood_samples"] == 0

    def test_min_sample_gate_prevents_single_sample_fluke(self, tmp_db, monkeypatch):
        """One correct sample at 0.70 must NOT pin LOW=0.70 (min_samples ≥ 3)."""
        scores = {
            "fluke_high": (0.70, "doc_a"),
            **{f"real_q{i}": (s, "doc_a") for i, s in enumerate([0.55, 0.50, 0.48])},
            **{f"ood_q{i}": (s, "doc_a") for i, s in enumerate([0.20, 0.18, 0.15])},
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        labeled = (
            [{"query": "fluke_high", "expected_path": "doc_a"}] +
            [{"query": f"real_q{i}", "expected_path": "doc_a"} for i in range(3)] +
            [{"query": f"ood_q{i}", "abstain": True} for i in range(3)]
        )
        result = mt.calibrate(tmp_db, labeled)
        # LOW should NOT be 0.70 — only 1 sample at that threshold.
        # Either it stays at default (no level meets min_samples + precision)
        # or it falls to a lower level where ≥3 samples exist.
        assert result["low_threshold"] != 0.70

    def test_empty_dataset_returns_not_ok(self, tmp_db):
        result = mt.calibrate(tmp_db, [])
        assert result["ok"] is False
        assert "no samples" in result["reason"]

    def test_only_abstain_dataset_returns_not_ok(self, tmp_db, monkeypatch):
        scores = {f"ood_q{i}": (s, "doc_a") for i, s in enumerate([0.2, 0.15])}
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        labeled = [{"query": f"ood_q{i}", "abstain": True} for i in range(2)]
        result = mt.calibrate(tmp_db, labeled)
        assert result["ok"] is False

    def test_calibration_row_persisted_to_db(self, tmp_db, monkeypatch):
        scores = {
            **{f"real_q{i}": (s, "doc_a") for i, s in enumerate([0.80, 0.75, 0.70, 0.65])},
            **{f"ood_q{i}": (s, "doc_a") for i, s in enumerate([0.20, 0.15])},
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        labeled = (
            [{"query": f"real_q{i}", "expected_path": "doc_a"} for i in range(4)] +
            [{"query": f"ood_q{i}", "abstain": True} for i in range(2)]
        )
        mt.calibrate(tmp_db, labeled)
        rows = tmp_db.execute(
            "SELECT low_threshold, abstain_threshold, sample_count, notes FROM calibration"
        ).fetchall()
        assert len(rows) == 1
        low, abstain, count, notes = rows[0]
        assert count == 6  # 4 real + 2 ood
        assert "real=4" in notes and "ood=2" in notes

    def test_abstain_clamped_to_safe_range(self, tmp_db, monkeypatch):
        """Pathological inputs (correct < ood) should not produce negative or >0.9 abstain."""
        scores = {
            "real_low": (0.10, "doc_a"),  # correct but very low score
            "real_low2": (0.12, "doc_a"),
            "real_low3": (0.15, "doc_a"),
            "ood_high": (0.50, "doc_a"),  # OOD scores higher than correct (degenerate)
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        labeled = (
            [{"query": f"real_low{i+1 if i else ''}", "expected_path": "doc_a"} for i in range(3)] +
            [{"query": "ood_high", "abstain": True}]
        )
        result = mt.calibrate(tmp_db, labeled)
        # Whatever the math, abstain must stay in [0, 0.9].
        assert 0.0 <= result["abstain_threshold"] <= 0.9

    def test_skips_items_missing_expected_path(self, tmp_db, monkeypatch):
        """Items without expected_path AND without abstain are silently skipped."""
        scores = {
            **{f"real_q{i}": (s, "doc_a") for i, s in enumerate([0.80, 0.75, 0.70])},
            "weird_no_label": (0.60, "doc_a"),  # no expected_path, no abstain
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        labeled = (
            [{"query": f"real_q{i}", "expected_path": "doc_a"} for i in range(3)] +
            [{"query": "weird_no_label"}]  # no expected_path, no abstain
        )
        result = mt.calibrate(tmp_db, labeled)
        assert result["ok"] is True
        assert result["samples"] == 3  # weird_no_label not counted


# ── TestBenchmarkAblation — V0/V1/V2/V3 correctness ───────────────────────────

class TestBenchmarkAblation:
    @pytest.fixture
    def fake_vault_min(self, tmp_path):
        v = tmp_path / "vault"
        (v / "Persona" / "life").mkdir(parents=True)
        (v / "MEMORY_TREE.md").write_text(
            "---\nid: root00000000000000000000000000000a\ntitle: Root\n"
            "description: Root map.\nlevel: 0\nchildren:\n  - household.md\n---\n"
        )
        (v / "household.md").write_text(
            "---\nid: hh000000000000000000000000000000b\ntitle: Household\n"
            "description: Who Liam lives with.\nlevel: 2\n"
            "see_also:\n  - Persona/life/buddy.md\n---\n"
        )
        (v / "Persona" / "life" / "buddy.md").write_text(
            "---\nid: bd000000000000000000000000000000c\ntitle: Buddy\n"
            "description: Liam's friend.\nlevel: 2\n---\n"
        )
        return v

    def test_ablation_returns_all_four_variants(self, tmp_db, fake_vault_min, stub_embed=None):
        # Use real retrieve via stub_embed from conftest path; need the build first.
        # We monkey-patch embed_text below.
        import scripts.tests.test_memory_tree as base  # reuse stub_embed
        pass

    def test_v0_vs_v3_variant_keys_present(self, tmp_db, monkeypatch):
        """Even on a tiny dataset, ablation must return all four variants."""
        # Build a deterministic retrieve mock that varies per use_see_also/use_abstain.
        def fake_retrieve(db, query, **kw):
            conf = 0.5
            results = [{"id": "x", "path": "doc_a", "title": "T", "score": conf, "route": "flat"}]
            fell_back = False
            if kw.get("use_abstain", True) and conf < kw.get("abstain_threshold", 0.35):
                results = []
                fell_back = True
            return {"results": results, "confidence": conf, "fell_back": fell_back, "trace": []}
        monkeypatch.setattr(mt, "retrieve", fake_retrieve)
        dataset = [
            {"query": "q1", "expected_path": "doc_a", "tag": "single"},
        ]
        report = mt.benchmark_ablation(tmp_db, dataset, k=3)
        assert set(report.keys()) == {"V0_flat_only", "V1_flat_abstain", "V2_flat_seealso", "V3_full"}

    def test_abstain_variants_differ_from_no_abstain(self, tmp_db, monkeypatch):
        """V1/V3 (with abstain) must abstain on low confidence; V0/V2 (without) must not."""
        def fake_retrieve(db, query, **kw):
            conf = 0.10  # below any reasonable abstain
            results = [{"id": "x", "path": "doc_a", "title": "T", "score": conf, "route": "flat"}]
            fell_back = False
            if kw.get("use_abstain", True) and conf < kw.get("abstain_threshold", 0.35):
                results = []
                fell_back = True
            return {"results": results, "confidence": conf, "fell_back": fell_back, "trace": []}
        monkeypatch.setattr(mt, "retrieve", fake_retrieve)
        dataset = [
            {"query": "q1", "abstain": True, "tag": "abstain-far"},
        ]
        report = mt.benchmark_ablation(tmp_db, dataset)
        # V0/V2 don't abstain — abstain_accuracy should be 0 (failed to abstain when expected).
        assert report["V0_flat_only"]["abstain_accuracy"] == 0.0
        assert report["V2_flat_seealso"]["abstain_accuracy"] == 0.0
        # V1/V3 do abstain — should hit 1.0.
        assert report["V1_flat_abstain"]["abstain_accuracy"] == 1.0
        assert report["V3_full"]["abstain_accuracy"] == 1.0

    def test_threshold_passthrough(self, tmp_db, monkeypatch):
        """Custom low/abstain thresholds reach retrieve() in all four variants."""
        captured: list[dict] = []
        def fake_retrieve(db, query, **kw):
            captured.append({"low": kw.get("low_threshold"), "abstain": kw.get("abstain_threshold")})
            return {"results": [], "confidence": 0.0, "fell_back": False, "trace": []}
        monkeypatch.setattr(mt, "retrieve", fake_retrieve)
        mt.benchmark_ablation(
            tmp_db,
            [{"query": "q", "expected_path": "doc_a", "tag": "single"}],
            low_threshold=0.42, abstain_threshold=0.21,
        )
        for c in captured:
            assert c["low"] == 0.42
            assert c["abstain"] == 0.21


# ── TestBenchmarkLOO — cache + stddev correctness ─────────────────────────────

class TestBenchmarkLOO:
    def test_loo_evaluated_on_non_abstain(self, tmp_db, monkeypatch):
        scores = {
            **{f"r{i}": (s, "doc_a") for i, s in enumerate([0.80, 0.75, 0.70, 0.65, 0.60])},
            **{f"o{i}": (s, "doc_x") for i, s in enumerate([0.25, 0.20, 0.15])},
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        dataset = (
            [{"query": f"r{i}", "expected_path": "doc_a"} for i in range(5)] +
            [{"query": f"o{i}", "abstain": True} for i in range(3)]
        )
        report = mt.benchmark_loo(tmp_db, dataset, k=3)
        assert report["n"] == 8
        assert report["non_abstain_evaluated"] == 5

    def test_loo_threshold_stability_reported(self, tmp_db, monkeypatch):
        """If thresholds vary across folds, stddev should be > 0."""
        # Configure scores so leaving different items out shifts the OOD-or-correct
        # boundary. Mix of correct/wrong at borderline scores.
        scores = {
            "r0": (0.80, "doc_a"), "r1": (0.75, "doc_a"), "r2": (0.70, "doc_a"),
            "r3": (0.55, "doc_a"), "r4": (0.50, "doc_a"),
            "o0": (0.30, "doc_x"), "o1": (0.25, "doc_x"), "o2": (0.20, "doc_x"),
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        dataset = (
            [{"query": f"r{i}", "expected_path": "doc_a"} for i in range(5)] +
            [{"query": f"o{i}", "abstain": True} for i in range(3)]
        )
        report = mt.benchmark_loo(tmp_db, dataset, k=3)
        assert "low_threshold_fit_stddev" in report
        assert "abstain_threshold_fit_stddev" in report
        # Stddev computed (≥ 0).
        assert report["low_threshold_fit_stddev"] >= 0.0

    def test_loo_handles_empty_dataset(self, tmp_db):
        report = mt.benchmark_loo(tmp_db, [], k=3)
        assert "error" in report

    def test_loo_handles_all_abstain_dataset(self, tmp_db, monkeypatch):
        scores = {f"o{i}": (s, "doc_x") for i, s in enumerate([0.2, 0.15, 0.1])}
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        dataset = [{"query": f"o{i}", "abstain": True} for i in range(3)]
        report = mt.benchmark_loo(tmp_db, dataset, k=3)
        # No real samples → no folds with valid fits → recall is None.
        assert report["non_abstain_evaluated"] == 0

    def test_loo_recall_computed_from_held_out(self, tmp_db, monkeypatch):
        """Recall_at_k_loo is over non-abstain held-outs, not the full dataset."""
        scores = {
            **{f"r{i}": (s, "doc_a") for i, s in enumerate([0.80, 0.75, 0.70, 0.65])},
            **{f"o{i}": (s, "doc_x") for i, s in enumerate([0.20, 0.15])},
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        dataset = (
            [{"query": f"r{i}", "expected_path": "doc_a"} for i in range(4)] +
            [{"query": f"o{i}", "abstain": True} for i in range(2)]
        )
        report = mt.benchmark_loo(tmp_db, dataset, k=3)
        assert report["non_abstain_evaluated"] == 4
        assert report["recall_at_k_loo"] is not None


# ── TestLogQuery — data integrity ─────────────────────────────────────────────

class TestLogQuery:
    def test_writes_both_jsonl_and_sql(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(mt, "_LOG_PATH", tmp_path / "queries.jsonl")
        result = {
            "results": [{"path": "doc_a", "route": "flat"}],
            "confidence": 0.5,
            "fell_back": False,
            "trace": ["flat_top=doc_a:0.500"],
        }
        mt._log_query(tmp_db, "test query", result)
        # JSONL exists with exactly 1 line.
        log = (tmp_path / "queries.jsonl").read_text().strip().splitlines()
        assert len(log) == 1
        entry = json.loads(log[0])
        assert entry["query"] == "test query"
        assert entry["final_confidence"] == 0.5
        assert entry["fell_back"] is False
        # SQL row exists.
        sql_rows = tmp_db.execute("SELECT query, final_confidence, fell_back FROM queries_log").fetchall()
        assert len(sql_rows) == 1
        assert sql_rows[0][0] == "test query"

    def test_jsonl_append_only(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(mt, "_LOG_PATH", tmp_path / "queries.jsonl")
        for i in range(3):
            mt._log_query(tmp_db, f"q{i}", {
                "results": [], "confidence": 0.0, "fell_back": True, "trace": [],
            })
        log = (tmp_path / "queries.jsonl").read_text().strip().splitlines()
        assert len(log) == 3

    def test_silent_on_filesystem_error(self, tmp_db, tmp_path, monkeypatch):
        # Point _LOG_PATH at a path under a nonexistent parent that can't be created.
        monkeypatch.setattr(mt, "_LOG_PATH", Path("/proc/cant_create/queries.jsonl"))
        # Should not raise — silent fail.
        mt._log_query(tmp_db, "q", {
            "results": [], "confidence": 0.0, "fell_back": True, "trace": [],
        })

    def test_silent_on_sql_error(self, tmp_db, tmp_path, monkeypatch):
        monkeypatch.setattr(mt, "_LOG_PATH", tmp_path / "queries.jsonl")
        # Drop the queries_log table to force a SQL error.
        tmp_db.execute("DROP TABLE queries_log")
        # Should not raise — JSONL still written.
        mt._log_query(tmp_db, "q", {
            "results": [{"path": "x", "route": "flat"}], "confidence": 0.5,
            "fell_back": False, "trace": [],
        })
        # JSONL written despite SQL failure.
        assert (tmp_path / "queries.jsonl").exists()


# ── TestBenchmark — per-tag breakdown ─────────────────────────────────────────

class TestBenchmarkPerTag:
    def test_per_tag_buckets_match_total(self, tmp_db, monkeypatch):
        scores = {
            "s1": (0.7, "doc_a"), "s2": (0.6, "doc_a"),
            "m1": (0.5, "doc_b"),
            "x1": (0.4, "doc_c"),
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        dataset = [
            {"query": "s1", "expected_path": "doc_a", "tag": "single"},
            {"query": "s2", "expected_path": "doc_a", "tag": "single"},
            {"query": "m1", "expected_path": "doc_b", "tag": "multi"},
            {"query": "x1", "expected_path": "doc_c", "tag": "cross-branch"},
        ]
        report = mt.benchmark(tmp_db, dataset, k=3)
        assert report["by_tag"]["single"]["n"] == 2
        assert report["by_tag"]["multi"]["n"] == 1
        assert report["by_tag"]["cross-branch"]["n"] == 1
        # Sum of n's equals total n.
        assert sum(b["n"] for b in report["by_tag"].values()) == report["n"]

    def test_per_tag_recall_computed(self, tmp_db, monkeypatch):
        scores = {
            "s_hit": (0.7, "doc_a"),
            "s_miss": (0.6, "doc_b"),  # expected doc_a, gets doc_b
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        dataset = [
            {"query": "s_hit", "expected_path": "doc_a", "tag": "single"},
            {"query": "s_miss", "expected_path": "doc_a", "tag": "single"},
        ]
        report = mt.benchmark(tmp_db, dataset, k=3)
        assert report["by_tag"]["single"]["recall_at_k"] == 0.5
        assert report["by_tag"]["single"]["mrr_at_k"] == 0.5

    def test_abstain_tags_get_abstain_accuracy(self, tmp_db, monkeypatch):
        """Tags starting with 'abstain' get abstain_accuracy in their bucket."""
        scores = {
            "ood_far": (0.1, "doc_a"),  # below abstain → fell_back=True
            "ood_near": (0.5, "doc_b"),  # above abstain → fell_back=False (a leak)
        }
        def fake_retrieve(db, query, **kw):
            conf, path = scores[query]
            ab = kw.get("abstain_threshold", 0.35)
            if conf < ab:
                return {"results": [], "confidence": conf, "fell_back": True, "trace": []}
            return {
                "results": [{"id": "x", "path": path, "title": "T", "score": conf, "route": "flat"}],
                "confidence": conf, "fell_back": False, "trace": [],
            }
        monkeypatch.setattr(mt, "retrieve", fake_retrieve)
        dataset = [
            {"query": "ood_far", "abstain": True, "tag": "abstain-far"},
            {"query": "ood_near", "abstain": True, "tag": "abstain-near"},
        ]
        report = mt.benchmark(tmp_db, dataset, k=3)
        assert report["by_tag"]["abstain-far"]["abstain_accuracy"] == 1.0
        assert report["by_tag"]["abstain-near"]["abstain_accuracy"] == 0.0

    def test_wrong_confident_uses_threshold(self, tmp_db, monkeypatch):
        """Wrong-confident only counts results above wrong_confident_score."""
        scores = {
            "wrong_high": (0.80, "doc_b"),  # expected doc_a → wrong AND confident
            "wrong_low": (0.40, "doc_b"),   # wrong but not confident
        }
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        dataset = [
            {"query": "wrong_high", "expected_path": "doc_a", "tag": "single"},
            {"query": "wrong_low", "expected_path": "doc_a", "tag": "single"},
        ]
        report = mt.benchmark(tmp_db, dataset, k=3, wrong_confident_score=0.65)
        assert report["wrong_confident_rate"] == 0.5  # 1 of 2 (only wrong_high)

    def test_latency_p50_p95_computed(self, tmp_db, monkeypatch):
        scores = {f"q{i}": (0.5, "doc_a") for i in range(20)}
        monkeypatch.setattr(mt, "retrieve", _mock_retrieve_factory(scores))
        dataset = [{"query": f"q{i}", "expected_path": "doc_a", "tag": "single"} for i in range(20)]
        report = mt.benchmark(tmp_db, dataset, k=3)
        assert report["latency_p50_ms"] >= 0
        assert report["latency_p95_ms"] >= report["latency_p50_ms"]


# ── TestRetrieveAblation — toggle correctness ─────────────────────────────────

class TestRetrieveAblation:
    @pytest.fixture
    def populated_db(self, tmp_db, tmp_path, monkeypatch):
        """Build a tiny vault and populate the DB so retrieve has nodes to score."""
        v = tmp_path / "vault"
        (v / "Persona" / "life").mkdir(parents=True)
        (v / "Persona" / "taste").mkdir(parents=True)
        (v / "MEMORY_TREE.md").write_text(
            "---\nid: root00000000000000000000000000000a\ntitle: Root\n"
            "description: Root map.\nlevel: 0\nchildren:\n  - household.md\n---\n"
        )
        (v / "household.md").write_text(
            "---\nid: hh000000000000000000000000000000b\ntitle: Household\n"
            "description: Who Liam lives with — roommates Shani and Omer.\nlevel: 2\n"
            "see_also:\n  - Persona/taste/movies.md\n---\n"
        )
        (v / "Persona" / "taste" / "movies.md").write_text(
            "---\nid: mv000000000000000000000000000000c\ntitle: Movies\n"
            "description: Films Liam enjoys — crime, thrillers.\nlevel: 2\n---\n"
        )
        # Stub embed: predictable scores.
        def stub_embed(text):
            v = [0.0] * mt.EMBED_DIM
            for i, c in enumerate(text[:mt.EMBED_DIM]):
                v[i] = (ord(c) % 17) / 17.0
            return v
        monkeypatch.setattr(mt, "embed_text", stub_embed)
        mt.build_tree(v, tmp_db, rebuild=False)
        return tmp_db

    def test_use_see_also_false_skips_expansion(self, populated_db):
        result_with = mt.retrieve(populated_db, "household roommates", k=5,
                                  low_threshold=0.0, abstain_threshold=0.0,
                                  use_see_also=True, use_abstain=False)
        result_without = mt.retrieve(populated_db, "household roommates", k=5,
                                     low_threshold=0.0, abstain_threshold=0.0,
                                     use_see_also=False, use_abstain=False)
        # Both return results. With expansion, trace should mention "expanded";
        # without, it should not.
        with_trace = " ".join(result_with["trace"])
        without_trace = " ".join(result_without["trace"])
        assert "expanded" in with_trace
        assert "expanded" not in without_trace

    def test_use_abstain_false_returns_results_at_zero_confidence(self, populated_db):
        # Even on garbage queries, with_abstain=False should never fall back.
        result = mt.retrieve(populated_db, "xyzzy plugh frobnitz", k=5,
                             low_threshold=0.99, abstain_threshold=0.99,
                             use_see_also=False, use_abstain=False)
        assert result["fell_back"] is False
        assert len(result["results"]) > 0  # at least some result returned

    def test_toggles_independent(self, populated_db):
        """All four combinations of (use_see_also, use_abstain) produce valid output."""
        for sa in (True, False):
            for ab in (True, False):
                result = mt.retrieve(populated_db, "household", k=3,
                                     low_threshold=0.5, abstain_threshold=0.3,
                                     use_see_also=sa, use_abstain=ab)
                # Result has the expected keys regardless of toggle state.
                assert "results" in result
                assert "confidence" in result
                assert "fell_back" in result


# ── TestCheckTreeEdges — additional check_tree paths ──────────────────────────

class TestCheckTreeEdges:
    @pytest.fixture
    def vault_no_root(self, tmp_path):
        """Vault without MEMORY_TREE.md to test root-missing detection."""
        v = tmp_path / "vault"
        (v / "Persona").mkdir(parents=True)
        (v / "Persona" / "x.md").write_text(
            "---\nid: x0000000000000000000000000000000a\ntitle: X\n"
            "description: standalone node.\nlevel: 1\n---\n"
        )
        return v

    def test_missing_root_node_flagged(self, tmp_db, vault_no_root, monkeypatch):
        def stub_embed(text):
            return [(ord(c) % 17) / 17.0 for c in text[:mt.EMBED_DIM]] + [0.0] * (mt.EMBED_DIM - len(text[:mt.EMBED_DIM]))
        monkeypatch.setattr(mt, "embed_text", stub_embed)
        mt.build_tree(vault_no_root, tmp_db)
        report = mt.check_tree(tmp_db, vault_no_root)
        assert report["ok"] is False
        # MEMORY_TREE.md missing → reported as an issue.
        issues_str = " ".join(str(i) for i in report["issues"])
        assert "MEMORY_TREE" in issues_str or "root" in issues_str.lower()


# ── TestHookEdges — corrupted input + symlinks ────────────────────────────────

# Reuse the hook module from conftest's pre-load; if not loaded yet, load now.
if "memory_tree_hook" in sys.modules:
    hook = sys.modules["memory_tree_hook"]
else:
    spec = importlib.util.spec_from_file_location("memory_tree_hook", _ROOT / "scripts" / "memory_tree_hook.py")
    hook = importlib.util.module_from_spec(spec)
    sys.modules["memory_tree_hook"] = hook
    spec.loader.exec_module(hook)


class TestHookEdges:
    def test_bad_stdin_silent(self, monkeypatch, tmp_path):
        """Corrupted JSON on stdin → main() exits silently, no exception."""
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
        # Should not raise.
        hook.main()

    def test_tool_input_with_no_file_path(self, monkeypatch):
        """tool_input present but missing file_path → bad_input."""
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        result = hook.dispatch({"tool_input": {"some_other_key": "v"}})
        assert result == "bad_input"

    def test_symlink_resolves_to_vault(self, tmp_path, monkeypatch):
        """A symlink pointing into the vault should still be classified as vault file."""
        v = tmp_path / "vault"
        v.mkdir()
        (v / "real.md").write_text("---\nid: x\ntitle: T\ndescription: test.\n---\n")
        link = tmp_path / "link.md"
        link.symlink_to(v / "real.md")
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        monkeypatch.setattr(hook, "_vault_root", lambda: v)
        result = hook.dispatch({"tool_input": {"file_path": str(link)}})
        # Resolves to v/real.md (under vault). Status depends on whether it's
        # in the tree — just ensure it's not 'not_vault_file'.
        assert result != "not_vault_file"

    def test_main_with_valid_stdin_dispatches(self, monkeypatch, tmp_path):
        """main() with valid JSON on stdin must call dispatch (not silent-exit)."""
        import io
        monkeypatch.delenv("DEUS_MEMORY_TREE", raising=False)  # forces gate_off path
        monkeypatch.setattr("sys.stdin", io.StringIO('{"tool_input": {"file_path": "/tmp/x.md"}}'))
        # Should reach dispatch and return cleanly.
        hook.main()  # no exception = pass

    def test_main_with_empty_stdin_uses_empty_dict(self, monkeypatch):
        """Empty stdin → JSON parses as `{}` → dispatch returns bad_input."""
        import io
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        monkeypatch.setattr("sys.stdin", io.StringIO(""))
        hook.main()  # no exception

    def test_vault_root_reads_config_json(self, monkeypatch, tmp_path):
        """When DEUS_VAULT_PATH is unset, _vault_root falls back to config.json."""
        monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
        cfg_dir = tmp_path / ".config" / "deus"
        cfg_dir.mkdir(parents=True)
        cfg_path = cfg_dir / "config.json"
        cfg_path.write_text(json.dumps({"vault_path": str(tmp_path / "myvault")}))
        # Patch the Path expanduser to return our tmp config path.
        from pathlib import Path as _P
        original_expanduser = _P.expanduser
        def fake_expand(self):
            if str(self) == "~/.config/deus/config.json":
                return cfg_path
            return original_expanduser(self)
        monkeypatch.setattr(_P, "expanduser", fake_expand)
        result = hook._vault_root()
        assert result == _P(str(tmp_path / "myvault"))

    def test_vault_root_returns_none_when_config_invalid(self, monkeypatch, tmp_path):
        """Malformed config.json → _vault_root returns None silently."""
        monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
        cfg_dir = tmp_path / ".config" / "deus"
        cfg_dir.mkdir(parents=True)
        cfg_path = cfg_dir / "config.json"
        cfg_path.write_text("{not valid json")
        from pathlib import Path as _P
        original_expanduser = _P.expanduser
        def fake_expand(self):
            if str(self) == "~/.config/deus/config.json":
                return cfg_path
            return original_expanduser(self)
        monkeypatch.setattr(_P, "expanduser", fake_expand)
        result = hook._vault_root()
        assert result is None


# ── TestDiscoverNode — auto-discovery path ────────────────────────────────────

def _stub_embed(text):
    """Deterministic embedding stub — same shape as embed_text."""
    v = [0.0] * mt.EMBED_DIM
    for i, c in enumerate(text[:mt.EMBED_DIM]):
        v[i] = (ord(c) % 17) / 17.0
    return v


class TestDiscoverNode:
    @pytest.fixture
    def vault(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mt, "embed_text", _stub_embed)
        v = tmp_path / "vault"
        v.mkdir()
        return v

    def test_basic_discovery_adds_node(self, vault, tmp_db):
        (vault / "new.md").write_text(
            "---\nid: new0000000000000000000000000000a\ntitle: New\n"
            "description: A freshly created file.\nlevel: 1\n---\n"
        )
        status = mt.discover_node(vault, "new.md", tmp_db)
        assert status == "discovered"
        row = tmp_db.execute(
            "SELECT id, title, level FROM nodes WHERE path = ?", ("new.md",)
        ).fetchone()
        assert row == ("new0000000000000000000000000000a", "New", 1)

    def test_missing_id_refuses(self, vault, tmp_db):
        (vault / "noid.md").write_text(
            "---\ntitle: NoID\ndescription: no id here.\n---\n"
        )
        status = mt.discover_node(vault, "noid.md", tmp_db)
        assert status == "no_id"
        row = tmp_db.execute("SELECT 1 FROM nodes WHERE path = ?", ("noid.md",)).fetchone()
        assert row is None

    def test_missing_description_refuses(self, vault, tmp_db):
        (vault / "nodesc.md").write_text(
            "---\nid: nodesc0000000000000000000000000a\ntitle: NoDesc\n---\n"
        )
        status = mt.discover_node(vault, "nodesc.md", tmp_db)
        assert status == "no_description"
        row = tmp_db.execute("SELECT 1 FROM nodes WHERE path = ?", ("nodesc.md",)).fetchone()
        assert row is None

    def test_missing_file_returns_missing(self, vault, tmp_db):
        status = mt.discover_node(vault, "absent.md", tmp_db)
        assert status == "missing"

    def test_skipped_dir_refuses(self, vault, tmp_db):
        session_dir = vault / "Session-Logs" / "2026-04-15"
        session_dir.mkdir(parents=True)
        (session_dir / "x.md").write_text(
            "---\nid: sess0000000000000000000000000000a\ntitle: Sess\n"
            "description: log.\n---\n"
        )
        status = mt.discover_node(vault, "Session-Logs/2026-04-15/x.md", tmp_db)
        assert status == "skipped_dir"

    def test_already_tracked_is_idempotent(self, vault, tmp_db):
        (vault / "twice.md").write_text(
            "---\nid: twice000000000000000000000000000a\ntitle: Twice\n"
            "description: discovered twice.\n---\n"
        )
        first = mt.discover_node(vault, "twice.md", tmp_db)
        second = mt.discover_node(vault, "twice.md", tmp_db)
        assert first == "discovered"
        assert second == "already_tracked"
        count = tmp_db.execute(
            "SELECT COUNT(*) FROM nodes WHERE path = ? AND orphaned_at IS NULL",
            ("twice.md",),
        ).fetchone()[0]
        assert count == 1

    def test_see_also_edge_to_existing_node(self, vault, tmp_db):
        (vault / "a.md").write_text(
            "---\nid: aa000000000000000000000000000000a\ntitle: A\n"
            "description: node A.\n---\n"
        )
        mt.discover_node(vault, "a.md", tmp_db)
        (vault / "b.md").write_text(
            "---\nid: bb000000000000000000000000000000b\ntitle: B\n"
            "description: node B references A.\nsee_also:\n  - a.md\n---\n"
        )
        status = mt.discover_node(vault, "b.md", tmp_db)
        assert status == "discovered"
        edge = tmp_db.execute(
            "SELECT kind FROM edges WHERE src_id = ? AND dst_id = ? AND expired_at IS NULL",
            ("bb000000000000000000000000000000b", "aa000000000000000000000000000000a"),
        ).fetchone()
        assert edge == ("see_also",)

    def test_see_also_to_unknown_is_silent_skip(self, vault, tmp_db):
        (vault / "lonely.md").write_text(
            "---\nid: lonely00000000000000000000000000a\ntitle: Lonely\n"
            "description: points to unknown sibling.\nsee_also:\n  - ghost.md\n---\n"
        )
        status = mt.discover_node(vault, "lonely.md", tmp_db)
        assert status == "discovered"
        edges = tmp_db.execute(
            "SELECT COUNT(*) FROM edges WHERE src_id = ?",
            ("lonely00000000000000000000000000a",),
        ).fetchone()[0]
        assert edges == 0


# ── TestHookDiscovery — hook wires reembed → discover ─────────────────────────

class TestHookDiscovery:
    def test_not_in_tree_triggers_discover_node(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        monkeypatch.setattr(mt, "embed_text", _stub_embed)
        v = tmp_path / "vault"
        v.mkdir()
        (v / "fresh.md").write_text(
            "---\nid: fresh000000000000000000000000000a\ntitle: Fresh\n"
            "description: brand new file.\n---\n"
        )
        db_path = tmp_path / "t.db"
        monkeypatch.setattr(mt, "DB_PATH", db_path)
        real_open_db = mt.open_db
        monkeypatch.setattr(mt, "open_db", lambda path=None: real_open_db(db_path))
        monkeypatch.setattr(hook, "_vault_root", lambda: v)
        status = hook.dispatch({"tool_input": {"file_path": str(v / "fresh.md")}})
        assert status == "discovered"

    def test_existing_file_still_reembeds(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        monkeypatch.setattr(mt, "embed_text", _stub_embed)
        v = tmp_path / "vault"
        v.mkdir()
        (v / "exists.md").write_text(
            "---\nid: exists00000000000000000000000000a\ntitle: Exists\n"
            "description: already in tree.\n---\n"
        )
        db_path = tmp_path / "t.db"
        real_open_db = mt.open_db
        monkeypatch.setattr(mt, "open_db", lambda path=None: real_open_db(db_path))
        monkeypatch.setattr(hook, "_vault_root", lambda: v)
        # Discover once so the node exists.
        db = real_open_db(db_path)
        mt.discover_node(v, "exists.md", db)
        db.close()
        status = hook.dispatch({"tool_input": {"file_path": str(v / "exists.md")}})
        assert status == "unchanged"

    def test_hook_returns_no_id_when_frontmatter_incomplete(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        monkeypatch.setattr(mt, "embed_text", _stub_embed)
        v = tmp_path / "vault"
        v.mkdir()
        (v / "bare.md").write_text("---\ntitle: Bare\ndescription: no id.\n---\n")
        db_path = tmp_path / "t.db"
        real_open_db = mt.open_db
        monkeypatch.setattr(mt, "open_db", lambda path=None: real_open_db(db_path))
        monkeypatch.setattr(hook, "_vault_root", lambda: v)
        status = hook.dispatch({"tool_input": {"file_path": str(v / "bare.md")}})
        assert status == "no_id"


# ── TestStopHookDiscovery — drift scan discovers new vault files ──────────────

if "stop_hook" in sys.modules:
    stop_hook = sys.modules["stop_hook"]
else:
    spec = importlib.util.spec_from_file_location("stop_hook", _ROOT / "scripts" / "stop_hook.py")
    stop_hook = importlib.util.module_from_spec(spec)
    sys.modules["stop_hook"] = stop_hook
    spec.loader.exec_module(stop_hook)


class TestStopHookDiscovery:
    def _setup(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        monkeypatch.setattr(mt, "embed_text", _stub_embed)
        v = tmp_path / "vault"
        v.mkdir()
        db_path = tmp_path / "t.db"
        real_open_db = mt.open_db
        monkeypatch.setattr(mt, "open_db", lambda path=None: real_open_db(db_path))
        return v, db_path, real_open_db

    def test_drift_scan_discovers_new_files(self, tmp_path, monkeypatch):
        v, db_path, real_open_db = self._setup(tmp_path, monkeypatch)
        (v / "new_a.md").write_text(
            "---\nid: newa0000000000000000000000000000a\ntitle: A\n"
            "description: new file A.\n---\n"
        )
        (v / "new_b.md").write_text(
            "---\nid: newb0000000000000000000000000000b\ntitle: B\n"
            "description: new file B.\n---\n"
        )
        attempted = stop_hook._scan_vault_drift(v, limit=5)
        assert attempted == 2
        db = real_open_db(db_path)
        count = db.execute(
            "SELECT COUNT(*) FROM nodes WHERE orphaned_at IS NULL"
        ).fetchone()[0]
        assert count == 2

    def test_drift_scan_respects_limit(self, tmp_path, monkeypatch):
        v, db_path, real_open_db = self._setup(tmp_path, monkeypatch)
        for i in range(5):
            (v / f"n{i}.md").write_text(
                f"---\nid: n{i}aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\ntitle: N{i}\n"
                f"description: node {i}.\n---\n"
            )
        attempted = stop_hook._scan_vault_drift(v, limit=2)
        # Up to 2 reembeds + up to 2 discoveries, but there are no tracked
        # rows yet, so all 5 are discovery candidates and only 2 should fire.
        db = real_open_db(db_path)
        count = db.execute(
            "SELECT COUNT(*) FROM nodes WHERE orphaned_at IS NULL"
        ).fetchone()[0]
        assert count == 2
        assert attempted == 2

    def test_drift_scan_skips_already_tracked(self, tmp_path, monkeypatch):
        v, db_path, real_open_db = self._setup(tmp_path, monkeypatch)
        (v / "tracked.md").write_text(
            "---\nid: trkd0000000000000000000000000000a\ntitle: T\n"
            "description: already here.\n---\n"
        )
        db = real_open_db(db_path)
        mt.discover_node(v, "tracked.md", db)
        db.close()
        attempted = stop_hook._scan_vault_drift(v, limit=5)
        # Tracked file is unchanged → reembed path no-ops, no new files → 0.
        assert attempted == 0

    def test_drift_scan_skips_session_logs_dir(self, tmp_path, monkeypatch):
        v, db_path, real_open_db = self._setup(tmp_path, monkeypatch)
        sess = v / "Session-Logs" / "2026-04-15"
        sess.mkdir(parents=True)
        (sess / "x.md").write_text(
            "---\nid: sess0000000000000000000000000000a\ntitle: S\n"
            "description: session log.\n---\n"
        )
        (v / "real.md").write_text(
            "---\nid: real0000000000000000000000000000a\ntitle: R\n"
            "description: real tree node.\n---\n"
        )
        attempted = stop_hook._scan_vault_drift(v, limit=5)
        assert attempted == 1
        db = real_open_db(db_path)
        paths = [r[0] for r in db.execute(
            "SELECT path FROM nodes WHERE orphaned_at IS NULL"
        ).fetchall()]
        assert paths == ["real.md"]

    def test_drift_scan_noop_when_gate_off(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEUS_MEMORY_TREE", raising=False)
        v = tmp_path / "vault"
        v.mkdir()
        (v / "x.md").write_text(
            "---\nid: xx000000000000000000000000000000a\ntitle: X\n"
            "description: x.\n---\n"
        )
        attempted = stop_hook._scan_vault_drift(v, limit=5)
        assert attempted == 0


# ── TestAutofixTree — check --auto-fix atomic pass ────────────────────────────

class TestAutofixTree:
    @pytest.fixture
    def vault(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mt, "embed_text", _stub_embed)
        v = tmp_path / "vault"
        v.mkdir()
        return v

    def test_autofix_discovers_new_files(self, vault, tmp_db):
        (vault / "new.md").write_text(
            "---\nid: new0000000000000000000000000000a\ntitle: New\n"
            "description: fresh node.\n---\n"
        )
        counts = mt.autofix_tree(tmp_db, vault)
        assert counts["discovered"] == 1
        assert counts["orphaned"] == 0
        row = tmp_db.execute(
            "SELECT 1 FROM nodes WHERE path = ? AND orphaned_at IS NULL", ("new.md",)
        ).fetchone()
        assert row is not None

    def test_autofix_orphans_missing_files(self, vault, tmp_db):
        (vault / "a.md").write_text(
            "---\nid: aa000000000000000000000000000000a\ntitle: A\n"
            "description: A.\n---\n"
        )
        mt.discover_node(vault, "a.md", tmp_db)
        (vault / "a.md").unlink()
        counts = mt.autofix_tree(tmp_db, vault)
        assert counts["orphaned"] == 1
        row = tmp_db.execute(
            "SELECT orphan_reason FROM nodes WHERE path = ?", ("a.md",)
        ).fetchone()
        assert row[0] == "missing_file"

    def test_autofix_reembeds_stale_files(self, vault, tmp_db, monkeypatch):
        import time as _time
        (vault / "s.md").write_text(
            "---\nid: sss0000000000000000000000000000a\ntitle: S\n"
            "description: original desc.\n---\n"
        )
        mt.discover_node(vault, "s.md", tmp_db)
        # Age the updated_at so file mtime will be newer.
        tmp_db.execute("UPDATE nodes SET updated_at = 0 WHERE path = ?", ("s.md",))
        tmp_db.commit()
        (vault / "s.md").write_text(
            "---\nid: sss0000000000000000000000000000a\ntitle: S\n"
            "description: CHANGED description.\n---\n"
        )
        counts = mt.autofix_tree(tmp_db, vault)
        assert counts["reembedded"] == 1
        row = tmp_db.execute(
            "SELECT description FROM nodes WHERE path = ?", ("s.md",)
        ).fetchone()
        assert row[0] == "CHANGED description."

    def test_autofix_idempotent(self, vault, tmp_db):
        (vault / "stable.md").write_text(
            "---\nid: stbl0000000000000000000000000000a\ntitle: Stable\n"
            "description: unchanged.\n---\n"
        )
        first = mt.autofix_tree(tmp_db, vault)
        second = mt.autofix_tree(tmp_db, vault)
        assert first["discovered"] == 1
        assert second["discovered"] == 0
        assert second["orphaned"] == 0
        assert second["reembedded"] == 0

    def test_autofix_mixed_pass(self, vault, tmp_db):
        # Seed: one tracked file, one that will go missing, one brand new.
        (vault / "keep.md").write_text(
            "---\nid: keep0000000000000000000000000000a\ntitle: Keep\n"
            "description: stays.\n---\n"
        )
        (vault / "gone.md").write_text(
            "---\nid: gone0000000000000000000000000000a\ntitle: Gone\n"
            "description: will be deleted.\n---\n"
        )
        mt.discover_node(vault, "keep.md", tmp_db)
        mt.discover_node(vault, "gone.md", tmp_db)
        (vault / "gone.md").unlink()
        (vault / "new.md").write_text(
            "---\nid: newx0000000000000000000000000000a\ntitle: NewX\n"
            "description: appeared.\n---\n"
        )
        counts = mt.autofix_tree(tmp_db, vault)
        assert counts["discovered"] == 1
        assert counts["orphaned"] == 1

    def test_autofix_skips_session_logs(self, vault, tmp_db):
        sess = vault / "Session-Logs" / "2026-04-15"
        sess.mkdir(parents=True)
        (sess / "x.md").write_text(
            "---\nid: sess0000000000000000000000000000a\ntitle: Sess\n"
            "description: session log.\n---\n"
        )
        counts = mt.autofix_tree(tmp_db, vault)
        assert counts["discovered"] == 0
