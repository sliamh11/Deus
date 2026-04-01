"""
Tests for scripts/memory_gc.py.

memory_gc.py calls _load_vault_atoms() at import time, which may exit(1) if no vault.
We set DEUS_VAULT_PATH before importing via a monkeypatched env variable.
"""
import importlib
import sys
import types
from datetime import date, timedelta
from pathlib import Path

import pytest

# Ensure project root and scripts/ are importable
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
for p in [_PROJECT_ROOT, _SCRIPTS_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(autouse=True)
def fresh_vault(tmp_path, monkeypatch):
    """Set up a temp vault and ensure a clean module import."""
    vault = tmp_path / "vault"
    atoms = vault / "Atoms"
    atoms.mkdir(parents=True)
    monkeypatch.setenv("DEUS_VAULT_PATH", str(vault))

    if "memory_gc" in sys.modules:
        del sys.modules["memory_gc"]

    yield vault, atoms


@pytest.fixture
def gc(tmp_path, fresh_vault, monkeypatch):
    """Import memory_gc with the temp vault already configured."""
    mod = importlib.import_module("memory_gc")
    return mod


# ── parse_frontmatter ─────────────────────────────────────────────────────


def test_parse_frontmatter_empty_for_no_yaml(gc):
    result = gc.parse_frontmatter("# Just a header\nno frontmatter")
    assert result == {}


def test_parse_frontmatter_extracts_ttl_days(gc):
    content = "---\nttl_days: 30\nupdated_at: 2024-01-01\n---\nbody"
    result = gc.parse_frontmatter(content)
    assert result.get("ttl_days") == "30"


def test_parse_frontmatter_extracts_updated_at(gc):
    content = "---\nttl_days: 60\nupdated_at: 2023-06-15\n---\nbody"
    result = gc.parse_frontmatter(content)
    assert result.get("updated_at") == "2023-06-15"


# ── set_frontmatter_field ─────────────────────────────────────────────────


def test_set_frontmatter_field_updates_existing_key(gc):
    content = "---\nttl_days: 30\nstatus: active\n---\nbody text"
    result = gc.set_frontmatter_field(content, "status", "archived")
    assert "status: archived" in result
    assert "status: active" not in result


def test_set_frontmatter_field_adds_new_key(gc):
    content = "---\nttl_days: 30\n---\nbody text"
    result = gc.set_frontmatter_field(content, "status", "archived")
    assert "status: archived" in result


# ── archive_file ──────────────────────────────────────────────────────────


def test_archive_file_dry_run_does_not_modify_files(gc, tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    md_file = memory_dir / "test.md"
    md_file.write_text("---\nttl_days: 7\nupdated_at: 2020-01-01\n---\ncontent")

    fm = gc.parse_frontmatter(md_file.read_text())
    gc.archive_file(memory_dir, md_file, fm, dry_run=True)

    # File should still exist (not moved)
    assert md_file.exists()


def test_archive_file_moves_file_to_archive_dir(gc, tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    md_file = memory_dir / "old_memory.md"
    md_file.write_text(
        "---\nname: Old Memory\nttl_days: 7\nupdated_at: 2020-01-01\n---\ncontent"
    )

    fm = gc.parse_frontmatter(md_file.read_text())
    gc.archive_file(memory_dir, md_file, fm, dry_run=False)

    # Original file should be gone
    assert not md_file.exists()
    # Archive file should exist
    archived = memory_dir / "ARCHIVE" / "old_memory.md"
    assert archived.exists()


def test_archive_file_writes_archived_status(gc, tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    md_file = memory_dir / "mem.md"
    md_file.write_text("---\nttl_days: 7\nupdated_at: 2020-01-01\n---\ncontent")

    fm = gc.parse_frontmatter(md_file.read_text())
    gc.archive_file(memory_dir, md_file, fm, dry_run=False)

    archived = memory_dir / "ARCHIVE" / "mem.md"
    assert "status: archived" in archived.read_text()


# ── run_gc ────────────────────────────────────────────────────────────────


def test_run_gc_skips_memory_md_file(gc, tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    # MEMORY.md should never be archived
    memory_md = memory_dir / "MEMORY.md"
    memory_md.write_text("---\nttl_days: 1\nupdated_at: 2020-01-01\n---\ncontent")

    count = gc.run_gc(memory_dir, dry_run=False)
    assert count == 0
    assert memory_md.exists()


def test_run_gc_archives_expired_files(gc, tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    # File expired 10 days ago (ttl=7, updated 20 days ago)
    old_date = (date.today() - timedelta(days=20)).isoformat()
    expired = memory_dir / "expired.md"
    expired.write_text(f"---\nttl_days: 7\nupdated_at: {old_date}\n---\ncontent")

    count = gc.run_gc(memory_dir, dry_run=False)
    assert count == 1
    assert not expired.exists()


def test_run_gc_does_not_archive_valid_files(gc, tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    # File updated today, TTL=30 — not expired
    recent_date = date.today().isoformat()
    fresh = memory_dir / "fresh.md"
    fresh.write_text(f"---\nttl_days: 30\nupdated_at: {recent_date}\n---\ncontent")

    count = gc.run_gc(memory_dir, dry_run=False)
    assert count == 0
    assert fresh.exists()


def test_run_gc_skips_files_without_ttl(gc, tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    no_ttl = memory_dir / "no_ttl.md"
    no_ttl.write_text("---\nupdated_at: 2020-01-01\n---\ncontent")

    count = gc.run_gc(memory_dir, dry_run=False)
    assert count == 0
    assert no_ttl.exists()


def test_run_gc_dry_run_returns_count_without_archiving(gc, tmp_path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    old_date = (date.today() - timedelta(days=20)).isoformat()
    expired = memory_dir / "will_expire.md"
    expired.write_text(f"---\nttl_days: 7\nupdated_at: {old_date}\n---\ncontent")

    count = gc.run_gc(memory_dir, dry_run=True)
    assert count == 1
    # File should still exist (dry run)
    assert expired.exists()


# ── find_memory_dirs ──────────────────────────────────────────────────────


def test_find_memory_dirs_finds_nested_memory_dirs(gc, tmp_path):
    base = tmp_path / "projects"
    base.mkdir()

    proj1 = base / "proj1"
    (proj1 / "memory").mkdir(parents=True)

    proj2 = base / "proj2"
    (proj2 / "memory").mkdir(parents=True)

    dirs = gc.find_memory_dirs(base)
    assert len(dirs) == 2


def test_find_memory_dirs_ignores_projects_without_memory(gc, tmp_path):
    base = tmp_path / "projects"
    base.mkdir()

    no_memory = base / "no_memory_proj"
    no_memory.mkdir()

    dirs = gc.find_memory_dirs(base)
    assert len(dirs) == 0


# ── run_atoms_gc ──────────────────────────────────────────────────────────


def test_run_atoms_gc_deletes_expired_atoms(gc, tmp_path, fresh_vault, monkeypatch):
    _vault, atoms = fresh_vault
    old_date = (date.today() - timedelta(days=20)).isoformat()
    atom = atoms / "old_atom.md"
    atom.write_text(f"---\nttl_days: 7\nupdated_at: {old_date}\n---\ncontent")

    # Patch VAULT_ATOMS to point to our temp atoms dir
    import memory_gc as _gc_mod
    monkeypatch.setattr(_gc_mod, "VAULT_ATOMS", atoms)

    count = gc.run_atoms_gc(dry_run=False)
    assert count >= 1
    assert not atom.exists()


def test_run_atoms_gc_keeps_atoms_without_ttl(gc, tmp_path, fresh_vault, monkeypatch):
    _vault, atoms = fresh_vault
    permanent_atom = atoms / "permanent.md"
    permanent_atom.write_text("---\nttl_days: null\n---\ncontent")

    import memory_gc as _gc_mod
    monkeypatch.setattr(_gc_mod, "VAULT_ATOMS", atoms)

    count = gc.run_atoms_gc(dry_run=False)
    assert count == 0
    assert permanent_atom.exists()
