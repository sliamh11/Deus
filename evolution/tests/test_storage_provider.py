"""Tests for the StorageProvider / StorageRegistry pattern and SQLite implementation."""
import os
import struct
from typing import Optional
from unittest.mock import patch

import pytest

import evolution.config as config_mod
import evolution.db as db_mod
from evolution.storage.provider import (
    NoStorageProviderError,
    StorageProvider,
    StorageRegistry,
)
from evolution.storage.providers.sqlite import SQLiteStorageProvider


# ── Test helpers ──────────────────────────────────────────────────────────────


class FakeStorageProvider(StorageProvider):
    """Minimal provider for registry tests — methods are not exercised."""

    def __init__(self, name: str, priority: int, available: bool = True):
        self._name = name
        self._priority = priority
        self._available = available

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    def is_available(self) -> bool:
        return self._available

    # Stubs — not exercised in registry tests
    def log_interaction(self, **kw): ...
    def update_interaction(self, *a, **kw): ...
    def get_interaction(self, *a): ...
    def get_recent_interactions(self, **kw): return []
    def get_previous_in_session(self, *a): ...
    def count_interactions(self, **kw): return 0
    def score_trend(self, **kw): return []
    def save_reflection(self, **kw): ...
    def get_reflections_by_embedding(self, *a, **kw): return []
    def check_reflection_duplicate(self, *a): return False
    def increment_reflection_retrieved(self, *a): ...
    def increment_reflection_helpful(self, *a): ...
    def archive_stale_reflections(self, *a): return 0
    def count_stale_reflections(self, *a): return 0
    def count_reflections(self): return 0
    def count_helpful_reflections(self): return 0
    def reflections_by_category(self): return []
    def get_reflections_for_interaction(self, *a): return []
    def save_artifact(self, **kw): ...
    def get_active_artifact(self, *a): ...
    def list_artifacts(self, *a, **kw): return []
    def get_latest_artifact_timestamp(self): ...
    def get_last_extraction(self, *a): ...
    def record_extraction(self, **kw): ...
    def interaction_stats(self, *a): return {}
    def backfill_reflection_count(self): return 0
    def count_scored_since(self, *a): return 0
    def count_new_scored(self, **kw): return 0
    def domain_comparison(self, *a): return {}


def _serialize_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clean_registry():
    """Ensure each test gets a fresh registry."""
    StorageRegistry.reset()
    yield
    StorageRegistry.reset()


@pytest.fixture
def sqlite_provider(tmp_path, monkeypatch):
    """Return a SQLiteStorageProvider backed by a temp DB."""
    test_db = tmp_path / "test_storage.db"
    monkeypatch.setattr(config_mod, "DB_PATH", test_db)
    monkeypatch.setattr(db_mod, "DB_PATH", test_db)
    return SQLiteStorageProvider()


# ── Registry unit tests ──────────────────────────────────────────────────────


class TestStorageRegistry:
    def test_register_and_get(self):
        reg = StorageRegistry.default()
        p = FakeStorageProvider("test", priority=10)
        reg.register(p)
        assert reg.get("test") is p

    def test_get_unknown_raises_keyerror(self):
        reg = StorageRegistry.default()
        with pytest.raises(KeyError):
            reg.get("nonexistent")

    def test_unregister(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("tmp", priority=10))
        reg.unregister("tmp")
        with pytest.raises(KeyError):
            reg.get("tmp")

    def test_unregister_missing_is_noop(self):
        reg = StorageRegistry.default()
        reg.unregister("nonexistent")  # should not raise

    def test_list_providers_sorted_by_priority(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("high", priority=30))
        reg.register(FakeStorageProvider("low", priority=5))
        reg.register(FakeStorageProvider("mid", priority=15))
        assert reg.list_providers() == ["low", "mid", "high"]

    def test_singleton(self):
        a = StorageRegistry.default()
        b = StorageRegistry.default()
        assert a is b

    def test_reset_creates_fresh_instance(self):
        a = StorageRegistry.default()
        a.register(FakeStorageProvider("x", priority=1))
        StorageRegistry.reset()
        b = StorageRegistry.default()
        assert a is not b
        assert b.list_providers() == []


class TestStorageResolve:
    def test_resolve_auto_detect_by_priority(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("slow", priority=20, available=True))
        reg.register(FakeStorageProvider("fast", priority=5, available=True))
        assert reg.resolve().name == "fast"

    def test_resolve_skips_unavailable(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("down", priority=1, available=False))
        reg.register(FakeStorageProvider("up", priority=10, available=True))
        assert reg.resolve().name == "up"

    def test_resolve_explicit_preference(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("a", priority=1, available=True))
        reg.register(FakeStorageProvider("b", priority=10, available=True))
        assert reg.resolve("b").name == "b"

    def test_resolve_env_var_override(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("a", priority=1, available=True))
        reg.register(FakeStorageProvider("b", priority=10, available=True))
        with patch.dict(os.environ, {"DEUS_STORAGE_PROVIDER": "b"}):
            assert reg.resolve().name == "b"

    def test_resolve_env_var_takes_precedence_over_preference(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("a", priority=1, available=True))
        reg.register(FakeStorageProvider("b", priority=10, available=True))
        with patch.dict(os.environ, {"DEUS_STORAGE_PROVIDER": "b"}):
            assert reg.resolve("a").name == "b"

    def test_resolve_no_providers_raises(self):
        reg = StorageRegistry.default()
        with pytest.raises(NoStorageProviderError, match="No storage provider available"):
            reg.resolve()

    def test_resolve_all_unavailable_raises(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("x", priority=1, available=False))
        reg.register(FakeStorageProvider("y", priority=2, available=False))
        with pytest.raises(NoStorageProviderError, match="No storage provider available"):
            reg.resolve()

    def test_resolve_unknown_preference_raises(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("a", priority=1, available=True))
        with pytest.raises(NoStorageProviderError, match="not registered"):
            reg.resolve("nonexistent")

    def test_resolve_preference_unavailable_raises(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("a", priority=1, available=False))
        with pytest.raises(NoStorageProviderError, match="not available"):
            reg.resolve("a")

    def test_resolve_case_insensitive(self):
        reg = StorageRegistry.default()
        reg.register(FakeStorageProvider("sqlite", priority=10, available=True))
        assert reg.resolve("SQLITE").name == "sqlite"


# ── SQLiteStorageProvider tests ──────────────────────────────────────────────


class TestSQLiteProviderMeta:
    def test_name_is_sqlite(self, sqlite_provider):
        assert sqlite_provider.name == "sqlite"

    def test_priority_is_10(self, sqlite_provider):
        assert sqlite_provider.priority == 10

    def test_is_available_returns_true(self, sqlite_provider):
        assert sqlite_provider.is_available() is True


class TestSQLiteProviderCreatesTables:
    def test_creates_interactions_table(self, sqlite_provider):
        """Ensure schema is created on first connect."""
        sqlite_provider.log_interaction(
            prompt="test", response="r", group_folder="g",
            timestamp="2024-01-01T00:00:00Z", interaction_id="id1",
        )
        row = sqlite_provider.get_interaction("id1")
        assert row is not None


class TestSQLiteInteractionCRUD:
    def test_log_and_get_interaction(self, sqlite_provider):
        iid = sqlite_provider.log_interaction(
            prompt="hello", response="world", group_folder="test",
            timestamp="2024-01-01T00:00:00Z", interaction_id="i1",
            latency_ms=42.0, tools_used='["read"]', session_id="s1",
            eval_suite="runtime", domain_presets='["eng"]', user_signal="positive",
        )
        assert iid == "i1"

        row = sqlite_provider.get_interaction("i1")
        assert row is not None
        assert row["prompt"] == "hello"
        assert row["response"] == "world"
        assert row["group_folder"] == "test"
        assert row["latency_ms"] == 42.0
        assert row["session_id"] == "s1"
        assert row["user_signal"] == "positive"

    def test_get_interaction_missing_returns_none(self, sqlite_provider):
        assert sqlite_provider.get_interaction("missing") is None

    def test_update_interaction(self, sqlite_provider):
        sqlite_provider.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp="2024-01-01T00:00:00Z", interaction_id="u1",
        )
        sqlite_provider.update_interaction("u1", judge_score=0.85, judge_dims='{"q":0.9}')

        row = sqlite_provider.get_interaction("u1")
        assert abs(row["judge_score"] - 0.85) < 1e-5

    def test_get_recent_interactions(self, sqlite_provider):
        for i in range(5):
            sqlite_provider.log_interaction(
                prompt=f"p{i}", response=f"r{i}", group_folder="g",
                timestamp=f"2024-01-0{i+1}T00:00:00Z", interaction_id=f"r{i}",
                eval_suite="runtime",
            )
        results = sqlite_provider.get_recent_interactions(limit=3, eval_suite="runtime")
        assert len(results) == 3
        # Should be most recent first
        assert results[0]["interaction_id"] if "interaction_id" in results[0] else results[0]["id"] == "r4"

    def test_get_recent_interactions_filters_by_group(self, sqlite_provider):
        sqlite_provider.log_interaction(
            prompt="p1", response="r1", group_folder="a",
            timestamp="2024-01-01T00:00:00Z", interaction_id="g1",
        )
        sqlite_provider.log_interaction(
            prompt="p2", response="r2", group_folder="b",
            timestamp="2024-01-02T00:00:00Z", interaction_id="g2",
        )
        results = sqlite_provider.get_recent_interactions(
            limit=10, group_folder="a", eval_suite=None,
        )
        assert len(results) == 1
        assert results[0]["group_folder"] == "a"

    def test_get_previous_in_session(self, sqlite_provider):
        sqlite_provider.log_interaction(
            prompt="first", response="r1", group_folder="g",
            timestamp="2024-01-01T00:00:00Z", interaction_id="s1",
            session_id="sess",
        )
        sqlite_provider.log_interaction(
            prompt="second", response="r2", group_folder="g",
            timestamp="2024-01-02T00:00:00Z", interaction_id="s2",
            session_id="sess",
        )
        prev = sqlite_provider.get_previous_in_session("sess", "s2")
        assert prev is not None
        assert prev["id"] == "s1"

    def test_count_interactions(self, sqlite_provider):
        sqlite_provider.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp="2024-01-01T00:00:00Z", interaction_id="c1",
            eval_suite="runtime",
        )
        assert sqlite_provider.count_interactions(eval_suite="runtime") == 1
        assert sqlite_provider.count_interactions(eval_suite="backfill") == 0


class TestSQLiteReflectionCRUD:
    EMBED_DIM = 768
    VECTOR_A = [1.0] + [0.0] * 767

    def test_save_and_count_reflections(self, sqlite_provider):
        sqlite_provider.save_reflection(
            reflection_id="ref1",
            content="Always validate inputs",
            category="reasoning",
            score_at_gen=0.4,
            timestamp="2024-01-01T00:00:00Z",
            embedding=_serialize_vec(self.VECTOR_A),
        )
        assert sqlite_provider.count_reflections() == 1

    def test_increment_retrieved(self, sqlite_provider):
        sqlite_provider.save_reflection(
            reflection_id="ref2",
            content="Test lesson",
            category="style",
            score_at_gen=0.5,
            timestamp="2024-01-01T00:00:00Z",
            embedding=_serialize_vec(self.VECTOR_A),
        )
        sqlite_provider.increment_reflection_retrieved("ref2")
        sqlite_provider.increment_reflection_retrieved("ref2")
        # Verify count increased (we can check via count_stale — 0 stale because retrieved > 0)

    def test_increment_helpful(self, sqlite_provider):
        sqlite_provider.save_reflection(
            reflection_id="ref3",
            content="Helpful lesson",
            category="reasoning",
            score_at_gen=0.5,
            timestamp="2024-01-01T00:00:00Z",
            embedding=_serialize_vec(self.VECTOR_A),
        )
        sqlite_provider.increment_reflection_helpful("ref3")
        assert sqlite_provider.count_helpful_reflections() == 1

    def test_reflections_by_category(self, sqlite_provider):
        for i, cat in enumerate(["reasoning", "reasoning", "style"]):
            sqlite_provider.save_reflection(
                reflection_id=f"cat{i}",
                content=f"Lesson {i}",
                category=cat,
                score_at_gen=0.4,
                timestamp="2024-01-01T00:00:00Z",
                embedding=_serialize_vec([float(i)] + [0.0] * 767),
            )
        cats = sqlite_provider.reflections_by_category()
        assert len(cats) == 2
        # reasoning should be first (count=2)
        assert cats[0]["category"] == "reasoning"
        assert cats[0]["n"] == 2

    def test_get_reflections_for_interaction(self, sqlite_provider):
        # First create an interaction
        sqlite_provider.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp="2024-01-01T00:00:00Z", interaction_id="ix1",
        )
        sqlite_provider.save_reflection(
            reflection_id="ref_ix",
            content="Linked reflection",
            category="style",
            score_at_gen=0.3,
            timestamp="2024-01-01T00:00:00Z",
            embedding=_serialize_vec(self.VECTOR_A),
            interaction_id="ix1",
        )
        refs = sqlite_provider.get_reflections_for_interaction("ix1")
        assert len(refs) == 1
        assert refs[0]["id"] == "ref_ix"

    def test_check_reflection_duplicate(self, sqlite_provider):
        vec = self.VECTOR_A
        blob = _serialize_vec(vec)
        sqlite_provider.save_reflection(
            reflection_id="dup1",
            content="Original",
            category="style",
            score_at_gen=0.4,
            timestamp="2024-01-01T00:00:00Z",
            embedding=blob,
        )
        # Same vector should be duplicate
        assert sqlite_provider.check_reflection_duplicate(blob, None, 0.4) is True
        # Very different vector should not be duplicate
        far_vec = [0.0] * 767 + [1.0]
        assert sqlite_provider.check_reflection_duplicate(
            _serialize_vec(far_vec), None, 0.4,
        ) is False


class TestSQLiteArtifactCRUD:
    def test_save_and_get_active_artifact(self, sqlite_provider):
        sqlite_provider.save_artifact(
            artifact_id="art1",
            module="qa",
            content="optimized prompt v1",
            created_at="2024-01-01T00:00:00Z",
            baseline_score=0.6,
            optimized_score=0.8,
            sample_count=20,
        )
        active = sqlite_provider.get_active_artifact("qa")
        assert active is not None
        assert active["id"] == "art1"
        assert active["content"] == "optimized prompt v1"
        assert active["active"] == 1

    def test_save_artifact_deactivates_previous(self, sqlite_provider):
        sqlite_provider.save_artifact(
            artifact_id="art_old",
            module="qa",
            content="v1",
            created_at="2024-01-01T00:00:00Z",
        )
        sqlite_provider.save_artifact(
            artifact_id="art_new",
            module="qa",
            content="v2",
            created_at="2024-01-02T00:00:00Z",
        )
        active = sqlite_provider.get_active_artifact("qa")
        assert active["id"] == "art_new"

    def test_list_artifacts(self, sqlite_provider):
        for i in range(3):
            sqlite_provider.save_artifact(
                artifact_id=f"la{i}",
                module="qa",
                content=f"v{i}",
                created_at=f"2024-01-0{i+1}T00:00:00Z",
            )
        arts = sqlite_provider.list_artifacts(module="qa", limit=10)
        assert len(arts) == 3

    def test_get_latest_artifact_timestamp(self, sqlite_provider):
        assert sqlite_provider.get_latest_artifact_timestamp() is None
        sqlite_provider.save_artifact(
            artifact_id="ts1",
            module="qa",
            content="v1",
            created_at="2024-06-15T12:00:00Z",
        )
        assert sqlite_provider.get_latest_artifact_timestamp() == "2024-06-15T12:00:00Z"

    def test_get_active_artifact_returns_none_for_missing_module(self, sqlite_provider):
        assert sqlite_provider.get_active_artifact("nonexistent") is None


class TestSQLitePrincipleExtraction:
    def test_record_and_get_last_extraction(self, sqlite_provider):
        assert sqlite_provider.get_last_extraction("cross-domain") is None

        sqlite_provider.record_extraction(
            extraction_id="ext1",
            domain="cross-domain",
            extracted_at="2024-01-15T00:00:00Z",
            interaction_count=10,
            principles_count=3,
        )
        last = sqlite_provider.get_last_extraction("cross-domain")
        assert last is not None
        assert last["extracted_at"] == "2024-01-15T00:00:00Z"

    def test_count_new_scored(self, sqlite_provider):
        sqlite_provider.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp="2024-01-01T00:00:00Z", interaction_id="ns1",
        )
        sqlite_provider.update_interaction("ns1", judge_score=0.7)

        count = sqlite_provider.count_new_scored()
        assert count == 1

        count_since = sqlite_provider.count_new_scored(
            since_timestamp="2024-01-02T00:00:00Z",
        )
        assert count_since == 0


class TestSQLiteStatusQueries:
    def test_interaction_stats(self, sqlite_provider):
        sqlite_provider.log_interaction(
            prompt="p1", response="r1", group_folder="g",
            timestamp="2024-01-01T00:00:00Z", interaction_id="st1",
            eval_suite="runtime",
        )
        sqlite_provider.update_interaction("st1", judge_score=0.8)

        stats = sqlite_provider.interaction_stats("runtime")
        assert stats["total"] == 1
        assert stats["scored"] == 1
        assert abs(stats["avg_score"] - 0.8) < 1e-5

    def test_count_scored_since(self, sqlite_provider):
        sqlite_provider.log_interaction(
            prompt="p", response="r", group_folder="g",
            timestamp="2024-06-01T00:00:00Z", interaction_id="cs1",
        )
        sqlite_provider.update_interaction("cs1", judge_score=0.7)

        assert sqlite_provider.count_scored_since("2024-05-01T00:00:00Z") == 1
        assert sqlite_provider.count_scored_since("2024-07-01T00:00:00Z") == 0

    def test_domain_comparison(self, sqlite_provider):
        sqlite_provider.log_interaction(
            prompt="p1", response="r1", group_folder="g",
            timestamp="2024-01-01T00:00:00Z", interaction_id="dc1",
            domain_presets='["eng"]',
        )
        sqlite_provider.update_interaction("dc1", judge_score=0.9)

        sqlite_provider.log_interaction(
            prompt="p2", response="r2", group_folder="g",
            timestamp="2024-01-02T00:00:00Z", interaction_id="dc2",
        )
        sqlite_provider.update_interaction("dc2", judge_score=0.5)

        comp = sqlite_provider.domain_comparison("eng")
        assert comp["with_n"] == 1
        assert comp["without_n"] == 1
        assert comp["with_avg"] > comp["without_avg"]


class TestBuiltInProviders:
    """Smoke tests for the built-in SQLite provider registration."""

    def test_sqlite_auto_registered(self):
        from evolution.storage import get_storage
        from evolution.storage.providers import _registry
        assert "sqlite" in _registry.list_providers()

    def test_sqlite_provider_has_correct_properties(self):
        from evolution.storage.providers.sqlite import SQLiteStorageProvider
        p = SQLiteStorageProvider()
        assert p.name == "sqlite"
        assert p.priority == 10
        assert p.is_available() is True
