"""Tests for code_search's per-project DB resolver + lazy legacy migration.

These tests self-isolate (the repo conftest only patches `memory_tree`, not
`code_search`):
  - the env-override and per-project cases use `monkeypatch.setenv` + a tmp HOME;
  - the legacy-fallback and migration cases use
    `monkeypatch.setattr(code_search, "DB_PATH", ...)` — DB_PATH is read at
    import time, so mutating the env after import would not reach the tier-3
    fallback.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import code_search  # noqa: E402


def _expected_per_project(home: Path, root: Path) -> Path:
    digest = hashlib.md5(str(root.resolve()).encode()).hexdigest()
    return home / ".config" / "deus" / "projects" / digest / "code_search.db"


# ── tier 1: explicit env override ────────────────────────────────────────────

def test_env_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "custom.db"
    monkeypatch.setenv("DEUS_CODE_SEARCH_DB", str(target))
    assert code_search._resolve_db_path() == target
    # env wins even when a project dir is passed
    assert code_search._resolve_db_path(tmp_path) == target


# ── tier 2: per-project, keyed by md5(realpath) ──────────────────────────────

def test_per_project_path_from_cwd(monkeypatch, tmp_path):
    monkeypatch.delenv("DEUS_CODE_SEARCH_DB", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    monkeypatch.chdir(proj)
    assert code_search._resolve_db_path() == _expected_per_project(home, proj)


def test_walk_up_from_subdir(monkeypatch, tmp_path):
    monkeypatch.delenv("DEUS_CODE_SEARCH_DB", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    sub = proj / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    # query from a deep subdir resolves to the SAME per-project DB as the root
    assert code_search._resolve_db_path() == _expected_per_project(home, proj)


def test_dotdeus_marker_also_roots(monkeypatch, tmp_path):
    monkeypatch.delenv("DEUS_CODE_SEARCH_DB", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    proj = tmp_path / "proj"
    (proj / ".deus").mkdir(parents=True)  # no .git, only .deus
    assert code_search._resolve_db_path(proj) == _expected_per_project(home, proj)


# ── tier 3: legacy fallback (patch DB_PATH directly, not env) ────────────────

def test_no_root_falls_back_to_legacy(monkeypatch, tmp_path):
    monkeypatch.delenv("DEUS_CODE_SEARCH_DB", raising=False)
    legacy = tmp_path / "legacy.db"
    monkeypatch.setattr(code_search, "DB_PATH", legacy)
    nogit = tmp_path / "nogit"
    nogit.mkdir()
    assert code_search._resolve_db_path(nogit) == legacy


# ── lazy migration: copy-on-match, skip-on-mismatch, idempotent ──────────────

def _seed_legacy(db_path: Path, indexed_directory: Path) -> None:
    db = code_search._init_db(db_path)
    db.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('indexed_directory', ?)",
        (str(indexed_directory.resolve()),),
    )
    db.commit()
    db.close()


def test_legacy_migration_copies_on_match(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    legacy = tmp_path / "legacy.db"
    monkeypatch.setattr(code_search, "DB_PATH", legacy)
    _seed_legacy(legacy, proj)

    dbp = tmp_path / "per-project" / "code_search.db"
    assert not dbp.exists()
    code_search._migrate_legacy_if_match(proj.resolve(), dbp)
    assert dbp.exists()       # copied into the per-project location
    assert legacy.exists()    # legacy kept (no-db-deletion)

    # idempotent: a second call is a no-op (dbp already exists)
    before = dbp.stat().st_mtime_ns
    code_search._migrate_legacy_if_match(proj.resolve(), dbp)
    assert dbp.stat().st_mtime_ns == before


def test_legacy_migration_skips_on_mismatch(monkeypatch, tmp_path):
    proj = tmp_path / "proj"
    (proj / ".git").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    legacy = tmp_path / "legacy.db"
    monkeypatch.setattr(code_search, "DB_PATH", legacy)
    _seed_legacy(legacy, other)  # legacy indexed a DIFFERENT project

    dbp = tmp_path / "pp" / "code_search.db"
    code_search._migrate_legacy_if_match(proj.resolve(), dbp)
    assert not dbp.exists()


def test_legacy_migration_noop_when_no_legacy(monkeypatch, tmp_path):
    monkeypatch.setattr(code_search, "DB_PATH", tmp_path / "absent-legacy.db")
    dbp = tmp_path / "pp" / "code_search.db"
    code_search._migrate_legacy_if_match((tmp_path / "proj").resolve(), dbp)
    assert not dbp.exists()
