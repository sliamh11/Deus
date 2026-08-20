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


# ── nested discovery (rglob) ────────────────────────────────────────────────


def test_run_gc_archives_nested_expired_file(gc, tmp_path):
    """run_gc must reach files nested under memory/procedures/, not just top-level."""
    memory_dir = tmp_path / "memory"
    (memory_dir / "procedures").mkdir(parents=True)

    old_date = (date.today() - timedelta(days=20)).isoformat()
    nested = memory_dir / "procedures" / "expired.md"
    nested.write_text(f"---\nttl_days: 7\nupdated_at: {old_date}\n---\ncontent")

    count = gc.run_gc(memory_dir, dry_run=False)
    assert count == 1
    assert not nested.exists()
    # nesting is preserved under ARCHIVE/, not flattened
    assert (memory_dir / "ARCHIVE" / "procedures" / "expired.md").exists()


def test_run_gc_does_not_reprocess_archive_dir(gc, tmp_path):
    """A second run_gc pass must not re-walk files already under ARCHIVE/."""
    memory_dir = tmp_path / "memory"
    (memory_dir / "procedures").mkdir(parents=True)

    old_date = (date.today() - timedelta(days=20)).isoformat()
    nested = memory_dir / "procedures" / "expired.md"
    nested.write_text(f"---\nttl_days: 7\nupdated_at: {old_date}\n---\ncontent")

    first = gc.run_gc(memory_dir, dry_run=False)
    assert first == 1

    second = gc.run_gc(memory_dir, dry_run=False)
    assert second == 0


def test_run_gc_same_basename_different_dirs_do_not_collide(gc, tmp_path):
    """Two distinct expired files sharing a basename in different subdirs
    must both survive archiving without one clobbering the other."""
    memory_dir = tmp_path / "memory"
    (memory_dir / "procedures").mkdir(parents=True)
    (memory_dir / "projects").mkdir(parents=True)

    old_date = (date.today() - timedelta(days=20)).isoformat()
    proc_file = memory_dir / "procedures" / "x.md"
    proc_file.write_text(f"---\nttl_days: 7\nupdated_at: {old_date}\n---\nPROC CONTENT")
    proj_file = memory_dir / "projects" / "x.md"
    proj_file.write_text(f"---\nttl_days: 7\nupdated_at: {old_date}\n---\nPROJ CONTENT")

    count = gc.run_gc(memory_dir, dry_run=False)
    assert count == 2

    proc_archived = memory_dir / "ARCHIVE" / "procedures" / "x.md"
    proj_archived = memory_dir / "ARCHIVE" / "projects" / "x.md"
    assert proc_archived.exists()
    assert proj_archived.exists()
    assert "PROC CONTENT" in proc_archived.read_text()
    assert "PROJ CONTENT" in proj_archived.read_text()


def test_archive_file_pointer_removal_does_not_strip_other_same_basename_pointer(gc, tmp_path):
    """Archiving procedures/x.md must not remove the MEMORY.md pointer for
    the still-active projects/x.md, even though both share the basename x.md."""
    memory_dir = tmp_path / "memory"
    (memory_dir / "procedures").mkdir(parents=True)
    (memory_dir / "projects").mkdir(parents=True)

    proc_file = memory_dir / "procedures" / "x.md"
    proc_file.write_text("---\nttl_days: 7\nupdated_at: 2020-01-01\n---\ncontent")
    proj_file = memory_dir / "projects" / "x.md"
    proj_file.write_text("---\nttl_days: 30\nupdated_at: 2020-01-01\n---\ncontent")

    memory_md = memory_dir / "MEMORY.md"
    memory_md.write_text(
        "- [Proc](procedures/x.md) — proc pointer\n"
        "- [Proj](projects/x.md) — proj pointer\n"
    )

    fm = gc.parse_frontmatter(proc_file.read_text())
    gc.archive_file(memory_dir, proc_file, fm, dry_run=False)

    remaining = memory_md.read_text()
    assert "procedures/x.md" not in remaining
    assert "projects/x.md" in remaining


# ── symlinked memory dir ────────────────────────────────────────────────────


def test_find_memory_dirs_and_run_gc_agree_on_symlinked_memory_dir(gc, tmp_path):
    """A project whose memory/ is a symlink (e.g. ~/.claude/projects/.../memory
    -> ~/.deus/auto-memory) must be discovered by find_memory_dirs AND have its
    nested files reached by run_gc, the same as a real directory."""
    base = tmp_path / "projects"
    base.mkdir()

    real_target = tmp_path / "auto-memory"
    (real_target / "procedures").mkdir(parents=True)

    old_date = (date.today() - timedelta(days=20)).isoformat()
    nested = real_target / "procedures" / "expired.md"
    nested.write_text(f"---\nttl_days: 7\nupdated_at: {old_date}\n---\ncontent")

    proj = base / "deus-project"
    proj.mkdir()
    (proj / "memory").symlink_to(real_target, target_is_directory=True)

    dirs = gc.find_memory_dirs(base)
    assert len(dirs) == 1

    count = gc.run_gc(dirs[0], dry_run=False)
    assert count == 1
    assert not nested.exists()
    assert (real_target / "ARCHIVE" / "procedures" / "expired.md").exists()


# ── tree-node orphaning ─────────────────────────────────────────────────────


def test_archive_file_orphans_tree_node_without_deleting_it(gc, tmp_path, monkeypatch):
    """archive_file must mark the memory-tree node orphaned (soft-delete),
    never hard-delete the row, per docs/decisions/no-db-deletion.md."""
    import memory_tree

    db_path = tmp_path / "tree.db"
    monkeypatch.setattr(memory_tree, "DB_PATH", db_path)

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    md_file = memory_dir / "indexed.md"
    md_file.write_text(
        "---\nid: test-node-123\nname: Indexed\nttl_days: 7\nupdated_at: 2020-01-01\n---\ncontent"
    )

    # Seed a tree-node row for this file's id, as reindex_external would have.
    db = memory_tree.open_db(db_path)
    now = memory_tree._utc_iso()
    db.execute(
        "INSERT INTO nodes (id, path, title, description, level, type, updated_at, content_hash) "
        "VALUES (?, ?, ?, ?, 0, 'feedback', ?, 'hash')",
        ("test-node-123", "auto-memory/indexed.md", "Indexed", "desc", now),
    )
    db.commit()
    db.close()

    fm = gc.parse_frontmatter(md_file.read_text())
    gc.archive_file(memory_dir, md_file, fm, dry_run=False)

    db = memory_tree.open_db(db_path)
    row = db.execute(
        "SELECT orphaned_at, orphan_reason FROM nodes WHERE id = ?", ("test-node-123",)
    ).fetchone()
    db.close()

    assert row is not None, "row must still exist -- soft-delete only, never hard-delete"
    assert row[0] is not None, "orphaned_at must be set"
    assert row[1] == "archived"


def test_archive_file_skips_orphaning_when_file_never_indexed(gc, tmp_path):
    """A file with no id: in frontmatter was never indexed by memory_tree --
    archiving it must not attempt to touch the tree DB (and must not raise)."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    md_file = memory_dir / "never_indexed.md"
    md_file.write_text("---\nttl_days: 7\nupdated_at: 2020-01-01\n---\ncontent")

    fm = gc.parse_frontmatter(md_file.read_text())
    # Should not raise even though no DB / no id is involved.
    gc.archive_file(memory_dir, md_file, fm, dry_run=False)

    assert (memory_dir / "ARCHIVE" / "never_indexed.md").exists()


def test_archive_file_warns_loudly_when_tree_db_unreachable(gc, tmp_path, monkeypatch, capsys):
    """Best-effort orphaning must not be SILENT: when the tree DB can't be
    reached, archive_file must still succeed (GC never crashes on this) but
    must emit a stderr warning naming the file -- otherwise a node is left
    live, pointing at a moved file, with no signal anywhere that it happened."""
    import memory_tree

    def _boom(*_args, **_kwargs):
        raise RuntimeError("simulated DB unavailable")

    monkeypatch.setattr(memory_tree, "open_db", _boom)

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    md_file = memory_dir / "indexed_but_unreachable.md"
    md_file.write_text(
        "---\nid: some-node-id\nttl_days: 7\nupdated_at: 2020-01-01\n---\ncontent"
    )

    fm = gc.parse_frontmatter(md_file.read_text())
    # Must not raise even though the tree DB open fails.
    gc.archive_file(memory_dir, md_file, fm, dry_run=False)

    assert (memory_dir / "ARCHIVE" / "indexed_but_unreachable.md").exists()

    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "indexed_but_unreachable.md" in captured.err
    assert "simulated DB unavailable" in captured.err


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
