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
