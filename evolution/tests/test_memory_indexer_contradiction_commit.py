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
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

# Import the scripts/ module without permanently polluting sys.path — otherwise
# scripts/ would shadow names while OTHER evolution tests are collected. The
# sibling helpers (_time, _exit_codes, _agent_io) get cached during this import.
_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")
_added = _SCRIPTS not in sys.path
if _added:
    sys.path.insert(0, _SCRIPTS)
try:
    import memory_indexer as mi  # noqa: E402
finally:
    if _added:
        sys.path.remove(_SCRIPTS)


def _unit_vec() -> list[float]:
    vec = [0.0] * mi.EMBED_DIM
    vec[0] = 1.0
    return vec


def test_detected_conflict_persists_across_connection_close(tmp_path, monkeypatch):
    monkeypatch.setattr(mi, "DB_PATH", tmp_path / "mem.db")

    # Force a CONTRADICT verdict without any network call.
    monkeypatch.setattr(
        mi,
        "_generate_with_fallback",
        lambda *a, **k: types.SimpleNamespace(text="CONTRADICT"),
    )

    vec = _unit_vec()

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
