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
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import code_search  # noqa: E402

_GIT = shutil.which("git")
_needs_git = pytest.mark.skipif(_GIT is None, reason="git not available")


def _git(cwd: Path, *args: str) -> None:
    """Run a git command with a hermetic identity (no global/system config)."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(
        [_GIT, *args], cwd=str(cwd), env=env,
        check=True, capture_output=True, text=True,
    )


def _make_main_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A real main repo (one commit) + a linked worktree. Returns resolved roots."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "f.txt").write_text("x")
    _git(main, "add", "f.txt")
    _git(main, "commit", "-q", "-m", "init")
    wt = tmp_path / "wt"
    _git(main, "worktree", "add", "-q", str(wt), "-b", "feature")
    return main.resolve(), wt.resolve()


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


# ── LIA-189: linked worktree normalizes to the canonical main-repo root ───────

@_needs_git
def test_worktree_keys_to_main_root(monkeypatch, tmp_path):
    monkeypatch.delenv("DEUS_CODE_SEARCH_DB", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    main, wt = _make_main_and_worktree(tmp_path)

    # the worktree resolves to the canonical main root, not its own path
    assert code_search._main_worktree_root(wt) == main
    assert code_search._project_root(wt) == main
    # → the per-project DB is keyed by the MAIN digest, not the worktree's
    assert code_search._resolve_db_path(wt) == _expected_per_project(home, main)
    assert code_search._resolve_db_path(wt) != _expected_per_project(home, wt)


@_needs_git
def test_main_worktree_root_noop(tmp_path):
    main, _wt = _make_main_and_worktree(tmp_path)
    # main worktree (.git is a dir) → unchanged
    assert code_search._main_worktree_root(main) == main
    # plain non-git dir → unchanged
    nogit = tmp_path / "plain"
    nogit.mkdir()
    assert code_search._main_worktree_root(nogit) == nogit


@_needs_git
def test_main_worktree_root_submodule_noop(tmp_path):
    """A submodule's .git is also a *file*, but its common dir is
    <super>/.git/modules/<name> (basename != '.git') — must NOT normalize to a
    parent, or it would key the submodule to a garbage path."""
    main, _wt = _make_main_and_worktree(tmp_path)
    sub_src = tmp_path / "sub_src"
    sub_src.mkdir()
    _git(sub_src, "init", "-q")
    (sub_src / "g.txt").write_text("y")
    _git(sub_src, "add", "g.txt")
    _git(sub_src, "commit", "-q", "-m", "sub init")
    # local-path submodule add needs protocol.file.allow=always on modern git
    _git(main, "-c", "protocol.file.allow=always", "submodule", "add", str(sub_src), "sub")
    submod = (main / "sub").resolve()
    assert (submod / ".git").is_file()  # confirm the submodule marker shape
    assert code_search._main_worktree_root(submod) == submod  # no-op, not normalized


@_needs_git
def test_read_path_from_worktree_cwd(monkeypatch, tmp_path):
    """The user-visible win: search()/status()/MCP read path call _resolve_db_path()
    with NO arg (cwd-based). From inside a worktree it must key to canonical."""
    monkeypatch.delenv("DEUS_CODE_SEARCH_DB", raising=False)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    main, wt = _make_main_and_worktree(tmp_path)
    sub = wt / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert code_search._resolve_db_path() == _expected_per_project(home, main)


# ── LIA-189: status() surfaces a stale (missing) indexed_directory ────────────

def _seed_status_db(db_path: Path, indexed_directory: str) -> None:
    db = code_search._init_db(db_path)
    db.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('indexed_directory', ?)",
        (indexed_directory,),
    )
    db.commit()
    db.close()


def test_status_stale_flag_when_directory_missing(monkeypatch, tmp_path):
    db_path = tmp_path / "cs.db"
    monkeypatch.setenv("DEUS_CODE_SEARCH_DB", str(db_path))
    _seed_status_db(db_path, str(tmp_path / "deleted-worktree"))
    result = code_search.status()
    assert result["indexed"] is True
    assert result["stale"] is True
    assert "message" in result


def test_status_not_stale_when_directory_exists(monkeypatch, tmp_path):
    db_path = tmp_path / "cs.db"
    monkeypatch.setenv("DEUS_CODE_SEARCH_DB", str(db_path))
    _seed_status_db(db_path, str(tmp_path))  # tmp_path exists
    result = code_search.status()
    assert result["stale"] is False
    assert "message" not in result
