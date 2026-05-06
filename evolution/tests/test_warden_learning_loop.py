"""
Tests for Phase 5: Generalized Warden Learning Loop.

Covers:
- dismiss_warden_finding with each valid warden type
- Invalid warden raises SystemExit
- dismiss_review_finding backwards compat (delegates to dismiss_warden_finding)
- Category filter on get_reflections_by_embedding (SQLite provider)
- No-category returns all reflections
"""
import json
import struct

import pytest

import evolution.config as config_mod
import evolution.db as db_mod
import evolution.providers.embeddings as embed_mod
from evolution.cli import (
    _VALID_WARDENS,
    cmd_dismiss_review_finding,
    cmd_dismiss_warden_finding,
)
from evolution.storage.provider import StorageRegistry
from evolution.storage.providers.sqlite import SQLiteStorageProvider, _migrated_paths

EMBED_DIM = 768
VECTOR_A = [1.0] + [0.0] * (EMBED_DIM - 1)
VECTOR_B = [0.0] * (EMBED_DIM - 1) + [1.0]


def _serialize_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def patch_db_and_embed(tmp_path, monkeypatch):
    """Redirect DB to temp dir and mock embeddings to avoid real API calls."""
    test_db = tmp_path / "test_warden.db"
    monkeypatch.setattr(db_mod, "EVOLUTION_DB_PATH", test_db)
    monkeypatch.setattr(config_mod, "EVOLUTION_DB_PATH", test_db)
    monkeypatch.setattr(config_mod, "DB_PATH", tmp_path / "nonexistent_legacy.db")
    monkeypatch.setattr(embed_mod, "_provider", None)
    monkeypatch.setattr(
        "evolution.reflexion.store._embed",
        lambda text: VECTOR_A,
    )
    # Clear migration cache so each test gets a fresh schema migration
    _migrated_paths.discard(str(test_db))
    # Ensure the SQLite provider is registered (other test files may reset the registry)
    registry = StorageRegistry.default()
    if "sqlite" not in registry.list_providers():
        registry.register(SQLiteStorageProvider())
    yield test_db
    _migrated_paths.discard(str(test_db))


@pytest.fixture
def sqlite_provider(tmp_path, monkeypatch):
    """Return a SQLiteStorageProvider backed by a temp DB."""
    test_db = tmp_path / "test_provider.db"
    monkeypatch.setattr(config_mod, "EVOLUTION_DB_PATH", test_db)
    monkeypatch.setattr(db_mod, "EVOLUTION_DB_PATH", test_db)
    monkeypatch.setattr(config_mod, "DB_PATH", tmp_path / "nonexistent_legacy.db")
    return SQLiteStorageProvider()


# ── dismiss_warden_finding tests ────────────────────────────────────────────


class TestDismissWardenFinding:
    @pytest.mark.parametrize("warden", sorted(_VALID_WARDENS))
    def test_valid_warden_creates_reflection(self, warden, capsys):
        payload = json.dumps({
            "warden": warden,
            "finding": "False alarm in test",
            "reason": "Not applicable here",
            "file": "src/test.ts",
            "line": 42,
        })
        cmd_dismiss_warden_finding(payload)
        raw = capsys.readouterr().out
        out = json.loads(raw)
        assert out.get("status") == "ok", f"Unexpected output for {warden}: {raw}"
        assert out["id"] is not None
        assert warden in out["content"]

    def test_invalid_warden_exits(self):
        payload = json.dumps({
            "warden": "nonexistent_warden",
            "finding": "test",
            "reason": "test",
        })
        with pytest.raises(SystemExit):
            cmd_dismiss_warden_finding(payload)

    def test_missing_finding_returns_error(self, capsys):
        payload = json.dumps({
            "warden": "code_review",
            "finding": "",
            "reason": "some reason",
        })
        cmd_dismiss_warden_finding(payload)
        out = json.loads(capsys.readouterr().out)
        assert "error" in out
        assert "finding" in out["error"]

    def test_missing_reason_returns_error(self, capsys):
        payload = json.dumps({
            "warden": "code_review",
            "finding": "some finding",
            "reason": "",
        })
        cmd_dismiss_warden_finding(payload)
        out = json.loads(capsys.readouterr().out)
        assert "error" in out
        assert "reason" in out["error"]

    def test_invalid_json_returns_error(self, capsys):
        cmd_dismiss_warden_finding("not json")
        out = json.loads(capsys.readouterr().out)
        assert "error" in out

    def test_reflection_content_includes_warden_name(self, capsys):
        payload = json.dumps({
            "warden": "plan_review",
            "finding": "Missing rollback step",
            "reason": "Rollback is handled upstream",
            "file": "docs/plan.md",
            "line": 10,
        })
        cmd_dismiss_warden_finding(payload)
        out = json.loads(capsys.readouterr().out)
        assert "plan_review" in out["content"]
        assert "Missing rollback step" in out["content"]
        assert "docs/plan.md:10" in out["content"]

    def test_group_folder_passed_through(self, capsys):
        payload = json.dumps({
            "warden": "threat_modeling",
            "finding": "Unencrypted channel",
            "reason": "Internal-only service",
            "file": "src/api.ts",
            "group_folder": "test-group",
        })
        cmd_dismiss_warden_finding(payload)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"


# ── dismiss_review_finding backwards compat ─────────────────────────────────


class TestDismissReviewFindingCompat:
    def test_delegates_to_warden_with_code_review(self, capsys):
        """dismiss_review_finding should produce a code_review reflection."""
        payload = json.dumps({
            "finding": "Unused import",
            "reason": "Required for type-only usage",
            "file": "src/index.ts",
            "line": 5,
        })
        cmd_dismiss_review_finding(payload)
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert "code_review" in out["content"]

    def test_invalid_json_returns_error(self, capsys):
        cmd_dismiss_review_finding("not json")
        out = json.loads(capsys.readouterr().out)
        assert "error" in out


# ── Category filter on get_reflections_by_embedding ─────────────────────────


class TestCategoryFilter:
    def test_category_filter_returns_only_matching(self, sqlite_provider):
        """When category is set, only reflections of that category are returned."""
        # Save two reflections with different categories
        sqlite_provider.save_reflection(
            reflection_id="ref_cr",
            content="Code review lesson",
            category="code_review",
            score_at_gen=0.3,
            timestamp="2024-01-01T00:00:00Z",
            embedding=_serialize_vec(VECTOR_A),
            group_folder="g",
        )
        sqlite_provider.save_reflection(
            reflection_id="ref_pr",
            content="Plan review lesson",
            category="plan_review",
            score_at_gen=0.3,
            timestamp="2024-01-02T00:00:00Z",
            embedding=_serialize_vec(VECTOR_A),
            group_folder="g",
        )

        # Filter by code_review
        results = sqlite_provider.get_reflections_by_embedding(
            embedding=_serialize_vec(VECTOR_A),
            top_k=10,
            group_folder="g",
            category="code_review",
        )
        assert len(results) == 1
        assert results[0]["category"] == "code_review"
        assert results[0]["id"] == "ref_cr"

    def test_no_category_returns_all(self, sqlite_provider):
        """When category is None, all reflections are returned regardless of category."""
        for i, cat in enumerate(["code_review", "plan_review", "threat_modeling"]):
            sqlite_provider.save_reflection(
                reflection_id=f"ref_{i}",
                content=f"Lesson for {cat}",
                category=cat,
                score_at_gen=0.3,
                timestamp=f"2024-01-0{i+1}T00:00:00Z",
                embedding=_serialize_vec(VECTOR_A),
                group_folder="g",
            )

        results = sqlite_provider.get_reflections_by_embedding(
            embedding=_serialize_vec(VECTOR_A),
            top_k=10,
            group_folder="g",
            category=None,
        )
        assert len(results) == 3

    def test_category_filter_with_no_matches(self, sqlite_provider):
        """Category filter with no matching reflections returns empty list."""
        sqlite_provider.save_reflection(
            reflection_id="ref_only",
            content="Only code review",
            category="code_review",
            score_at_gen=0.3,
            timestamp="2024-01-01T00:00:00Z",
            embedding=_serialize_vec(VECTOR_A),
            group_folder="g",
        )

        results = sqlite_provider.get_reflections_by_embedding(
            embedding=_serialize_vec(VECTOR_A),
            top_k=10,
            group_folder="g",
            category="threat_modeling",
        )
        assert len(results) == 0

    def test_category_filter_threat_modeling(self, sqlite_provider):
        """Verify threat_modeling category filter works correctly."""
        sqlite_provider.save_reflection(
            reflection_id="ref_tm",
            content="Threat modeling false positive",
            category="threat_modeling",
            score_at_gen=0.3,
            timestamp="2024-01-01T00:00:00Z",
            embedding=_serialize_vec(VECTOR_A),
            group_folder="g",
        )
        sqlite_provider.save_reflection(
            reflection_id="ref_other",
            content="Reasoning lesson",
            category="reasoning",
            score_at_gen=0.4,
            timestamp="2024-01-02T00:00:00Z",
            embedding=_serialize_vec(VECTOR_A),
            group_folder="g",
        )

        results = sqlite_provider.get_reflections_by_embedding(
            embedding=_serialize_vec(VECTOR_A),
            top_k=10,
            group_folder="g",
            category="threat_modeling",
        )
        assert len(results) == 1
        assert results[0]["id"] == "ref_tm"
