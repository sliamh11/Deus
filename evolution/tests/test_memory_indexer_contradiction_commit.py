"""Regression test: detected contradictions must PERSIST to pending_conflicts.

Guards the 2026-06-09 bug where ``detect_contradictions`` INSERTed a row into
``pending_conflicts`` but no commit followed — its only caller (``cmd_extract``)
does its last ``db.commit()`` BEFORE contradiction detection runs. The
connection is opened with the default deferred isolation level, so the
uncommitted INSERT rolled back on connection close and ``--resolve-conflicts``
was permanently empty (the contradiction-review feature silently never worked).

The LLM contradiction check is mocked; only the persistence path is exercised.
``google-genai`` is import-only here (the real client is never constructed), so
it must be installed in CI — see the evolution-deps step in
``.github/workflows/ci.yml``.

Import note: ``memory_indexer`` resolves the memory vault LAZILY (first
``_vault_root()`` call), so importing it needs no vault — CI has none. The
import is still deferred into the test only to keep ``scripts/`` off
``sys.path`` during collection of sibling tests. The vault is never touched: the
test redirects ``DB_PATH`` and ``detect_contradictions`` only uses the DB
connection. ``import_module`` returns the cached module on a second import, so
if a future test adds a module-level ``memory_indexer`` import this becomes
order-sensitive; today this is the only importer.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")


def _import_memory_indexer():
    """Import scripts/memory_indexer.py without leaving scripts/ on sys.path
    (it would shadow names while OTHER evolution tests are collected)."""
    added = _SCRIPTS not in sys.path
    if added:
        sys.path.insert(0, _SCRIPTS)
    try:
        return importlib.import_module("memory_indexer")
    finally:
        if added:
            sys.path.remove(_SCRIPTS)


def test_detected_conflict_persists_across_connection_close(tmp_path, monkeypatch):
    # Vault resolution is lazy and never triggered here (the test redirects
    # DB_PATH and only touches the DB connection), so no DEUS_VAULT_PATH needed.
    mi = _import_memory_indexer()
    monkeypatch.setattr(mi, "DB_PATH", tmp_path / "mem.db")

    # Force a CONTRADICT verdict without any network call.
    monkeypatch.setattr(
        mi,
        "_generate_with_fallback",
        lambda *a, **k: types.SimpleNamespace(text="CONTRADICT"),
    )

    vec = [0.0] * mi.EMBED_DIM
    vec[0] = 1.0

    # Seed one existing atom + its embedding so the KNN MATCH returns a candidate.
    db = mi.open_db()
    db.execute(
        "INSERT INTO entries (id, path, date, chunk, type) "
        "VALUES (1, 'existing.md', '2026-06-09', 'Existing atom text.', 'atom')"
    )
    db.execute(
        "INSERT INTO embeddings (rowid, embedding) VALUES (1, ?)",
        [mi.serialize(vec)],
    )
    db.commit()

    conflicts = mi.detect_contradictions(db, 2, "New contradicting atom text.", vec)
    assert len(conflicts) == 1, "detection should flag exactly one conflict"
    db.close()

    # Reopen a fresh connection: the row must survive. Without the write-site
    # commit this returns [] (the regression).
    db2 = mi.open_db()
    rows = db2.execute(
        "SELECT older_id, newer_id FROM pending_conflicts WHERE resolved = 0"
    ).fetchall()
    db2.close()

    assert rows == [(1, 2)], (
        "pending_conflicts row must persist after the connection closes — "
        "detect_contradictions must commit at the write site"
    )


# ── LIA-338: resolution-model completeness (resolved_at + 3-way --newer) ──────


def _seed_conflict(mi, tmp_path, older_id=1, newer_id=2):
    """Seed two atoms + one unresolved pending_conflicts row. Returns the
    conflict id. Atom paths point under tmp_path and do NOT exist, so
    invalidate_atom's file-rewrite branch is skipped (DB-only, no fixture files).
    """
    db = mi.open_db()
    for aid, when, text in (
        (older_id, "2026-06-01", "Older atom text."),
        (newer_id, "2026-06-25", "Newer atom text."),
    ):
        db.execute(
            "INSERT INTO entries (id, path, date, chunk, type) VALUES (?, ?, ?, ?, 'atom')",
            [aid, str(tmp_path / f"atom_{aid}.md"), when, text],
        )
    db.execute(
        "INSERT INTO pending_conflicts (older_id, newer_id, older_text, newer_text, created_at) "
        "VALUES (?, ?, ?, ?, '2026-06-25')",
        [older_id, newer_id, "Older atom text.", "Newer atom text."],
    )
    cid = db.execute(
        "SELECT id FROM pending_conflicts WHERE older_id = ? AND newer_id = ?",
        [older_id, newer_id],
    ).fetchone()[0]
    db.commit()
    db.close()
    return cid


def _conflict_row(mi, cid):
    db = mi.open_db()
    row = db.execute(
        "SELECT resolved, resolution, resolved_at FROM pending_conflicts WHERE id = ?",
        [cid],
    ).fetchone()
    db.close()
    return row  # (resolved, resolution, resolved_at)


def _expired(mi, atom_id):
    db = mi.open_db()
    row = db.execute("SELECT expired_at FROM entries WHERE id = ?", [atom_id]).fetchone()
    db.close()
    return row[0]


def test_resolved_at_null_until_invalidate_older(tmp_path, monkeypatch):
    """Default --invalidate-conflict expires the OLDER atom, stamps resolved_at,
    and leaves the newer atom intact. resolved_at is NULL until resolution."""
    mi = _import_memory_indexer()
    monkeypatch.setattr(mi, "DB_PATH", tmp_path / "mem.db")
    cid = _seed_conflict(mi, tmp_path)

    assert _conflict_row(mi, cid) == (0, None, None), "fresh conflict: unresolved, resolved_at NULL"

    mi.cmd_invalidate_conflict(cid)

    resolved, resolution, resolved_at = _conflict_row(mi, cid)
    assert resolved == 1 and resolution == "invalidated"
    assert resolved_at is not None, "resolved_at must be stamped on invalidate"
    assert _expired(mi, 1) is not None, "older atom must be soft-deleted"
    assert _expired(mi, 2) is None, "newer atom must be untouched"


def test_invalidate_conflict_newer_branch(tmp_path, monkeypatch):
    """--newer expires the NEWER atom, records resolution=invalidated_newer, and
    leaves the older atom intact (closes the one-directional gap, LIA-338)."""
    mi = _import_memory_indexer()
    monkeypatch.setattr(mi, "DB_PATH", tmp_path / "mem.db")
    cid = _seed_conflict(mi, tmp_path)

    mi.cmd_invalidate_conflict(cid, newer=True)

    resolved, resolution, resolved_at = _conflict_row(mi, cid)
    assert resolved == 1 and resolution == "invalidated_newer"
    assert resolved_at is not None
    assert _expired(mi, 2) is not None, "newer atom must be soft-deleted"
    assert _expired(mi, 1) is None, "older atom must be untouched"


def test_dismiss_sets_resolved_at(tmp_path, monkeypatch):
    """Dismiss stamps resolved_at and touches no atom."""
    mi = _import_memory_indexer()
    monkeypatch.setattr(mi, "DB_PATH", tmp_path / "mem.db")
    cid = _seed_conflict(mi, tmp_path)

    mi.cmd_dismiss_conflict(cid)

    resolved, resolution, resolved_at = _conflict_row(mi, cid)
    assert resolved == 1 and resolution == "dismissed" and resolved_at is not None
    assert _expired(mi, 1) is None and _expired(mi, 2) is None


def test_dismiss_is_noop_on_already_resolved(tmp_path, monkeypatch):
    """Re-dismissing an already-resolved row must NOT overwrite resolved_at —
    guards the migration invariant that historical rows keep their value
    (here: NULL) instead of being stamped with today's date."""
    mi = _import_memory_indexer()
    monkeypatch.setattr(mi, "DB_PATH", tmp_path / "mem.db")
    cid = _seed_conflict(mi, tmp_path)

    # Simulate a historical resolved row: resolved=1 but resolved_at NULL.
    db = mi.open_db()
    db.execute(
        "UPDATE pending_conflicts SET resolved = 1, resolution = 'dismissed', resolved_at = NULL "
        "WHERE id = ?",
        [cid],
    )
    db.commit()
    db.close()

    mi.cmd_dismiss_conflict(cid)  # must be a no-op

    resolved, resolution, resolved_at = _conflict_row(mi, cid)
    assert resolved == 1 and resolution == "dismissed"
    assert resolved_at is None, "re-dismiss must not stamp a historical NULL row"


def test_cli_dispatch_invalidate_conflict_newer(tmp_path, monkeypatch):
    """End-to-end through main(): argv --invalidate-conflict <id> --newer wires
    into cmd_invalidate_conflict(id, newer=True)."""
    mi = _import_memory_indexer()
    monkeypatch.setattr(mi, "DB_PATH", tmp_path / "mem.db")
    cid = _seed_conflict(mi, tmp_path)

    monkeypatch.setattr(
        sys, "argv",
        ["memory_indexer.py", "--invalidate-conflict", str(cid), "--newer"],
    )
    mi.main()

    resolved, resolution, _ = _conflict_row(mi, cid)
    assert resolved == 1 and resolution == "invalidated_newer"
    assert _expired(mi, 2) is not None and _expired(mi, 1) is None


def test_newer_without_invalidate_conflict_errors(tmp_path, monkeypatch):
    """--newer alone is rejected (parser.error → SystemExit), never silently
    ignored."""
    import pytest

    mi = _import_memory_indexer()
    monkeypatch.setattr(mi, "DB_PATH", tmp_path / "mem.db")
    monkeypatch.setattr(sys, "argv", ["memory_indexer.py", "--newer"])
    with pytest.raises(SystemExit):
        mi.main()


def test_migration_adds_resolved_at_to_old_table(tmp_path, monkeypatch):
    """An existing pending_conflicts table without resolved_at gains it on
    open_db(), and the pre-existing resolved row keeps resolved_at NULL (no
    fabricated timestamp)."""
    import sqlite3

    db_path = tmp_path / "mem.db"
    monkeypatch.setattr(_import_memory_indexer(), "DB_PATH", db_path)

    # Simulate a pre-LIA-338 DB: old schema, one already-resolved row.
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE pending_conflicts ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, older_id INTEGER NOT NULL, "
        "newer_id INTEGER NOT NULL, older_text TEXT, newer_text TEXT, "
        "created_at TEXT NOT NULL, resolved INTEGER DEFAULT 0, resolution TEXT, "
        "UNIQUE(older_id, newer_id))"
    )
    raw.execute(
        "INSERT INTO pending_conflicts (older_id, newer_id, created_at, resolved, resolution) "
        "VALUES (10, 11, '2026-06-10', 1, 'dismissed')"
    )
    raw.commit()
    raw.close()

    mi = _import_memory_indexer()
    db = mi.open_db()  # runs the idempotent ALTER migration
    cols = [r[1] for r in db.execute("PRAGMA table_info(pending_conflicts)").fetchall()]
    assert "resolved_at" in cols, "migration must add resolved_at to an old table"
    val = db.execute(
        "SELECT resolved_at FROM pending_conflicts WHERE older_id = 10"
    ).fetchone()[0]
    assert val is None, "historical resolved row must stay NULL (no fabricated timestamp)"
    db.close()

    # Idempotent: a second open_db() (re-running the ALTER) must not raise.
    mi.open_db().close()
