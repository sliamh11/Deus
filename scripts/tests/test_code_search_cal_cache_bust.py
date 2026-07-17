"""Test that reindex() always busts the module-level _cal_cache (LIA-207).

A long-running code_search server loads calibration_distances once into
_cal_cache and then serves stale retrieval_confidence after a
DELETE+reindex cycle.  The fix: reindex() sets _cal_cache = None
unconditionally after db.commit(), so the next search() reloads from DB.

Hermetic — no Ollama / embedding required.  We assert the cache-reset
invariant directly, covering both the "cal_row existed" branch (the bug
scenario) and the "no cal_row" branch (auto-calibration path).
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import code_search  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_db(db_path: Path, cal_distances: list[float] | None = None) -> None:
    """Initialise a minimal code_search DB, optionally pre-seeding calibration."""
    db = sqlite3.connect(str(db_path))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            file_path TEXT NOT NULL,
            chunk_type TEXT NOT NULL,
            chunk_name TEXT,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            mtime REAL NOT NULL,
            embedded_at TEXT,
            orphaned_at TEXT DEFAULT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    if cal_distances is not None:
        db.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('calibration_distances', ?)",
            [json.dumps(cal_distances)],
        )
    db.commit()
    db.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_reindex_busts_cache_when_calibration_already_present(
    monkeypatch, tmp_path
):
    """Bug scenario (LIA-207): calibration_distances already in DB.

    reindex() hit the `if not cal_row` guard and skipped _cal_cache = None,
    leaving a stale cache in a long-running process.
    """
    # Arrange: a dir with one Python file to index
    src = tmp_path / "src"
    src.mkdir()
    (src / "hello.py").write_text("def hello():\n    return 'world'\n")

    # DB pre-seeded with calibration data (simulates a prior generate-fixture run)
    db_path = tmp_path / "code_search.db"
    sentinel_cal = [0.30, 0.40, 0.50, 0.60, 0.70]
    _seed_db(db_path, cal_distances=sentinel_cal)

    # Simulate a long-running server that already loaded the cache
    monkeypatch.setattr(code_search, "_cal_cache", [0.99, 0.98])  # stale value
    monkeypatch.setenv("DEUS_CODE_SEARCH_DB", str(db_path))

    # Act
    result = code_search.reindex(str(src))

    # Assert: cache busted regardless of whether reindex wrote new calibration
    assert code_search._cal_cache is None, (
        "reindex() must set _cal_cache = None so next search() reloads from DB; "
        f"got {code_search._cal_cache!r}"
    )
    assert "error" not in result


def test_reindex_busts_cache_when_no_calibration_and_embed_unavailable(
    monkeypatch, tmp_path
):
    """Edge case: no calibration row, embed unavailable (Ollama down).

    Auto-calibration is skipped entirely, but _cal_cache must still be None
    after reindex() so the server doesn't continue serving a cached value from
    a previous run.
    """
    src = tmp_path / "src"
    src.mkdir()
    (src / "util.py").write_text("def add(a, b):\n    return a + b\n")

    db_path = tmp_path / "code_search.db"
    _seed_db(db_path, cal_distances=None)  # no calibration pre-seeded

    # Simulate stale cache from a previous successful calibration
    monkeypatch.setattr(code_search, "_cal_cache", [0.50, 0.55, 0.60])
    monkeypatch.setenv("DEUS_CODE_SEARCH_DB", str(db_path))

    result = code_search.reindex(str(src))

    assert code_search._cal_cache is None, (
        "reindex() must bust _cal_cache even when calibration is absent/skipped; "
        f"got {code_search._cal_cache!r}"
    )
    assert "error" not in result


# ---------------------------------------------------------------------------
# LIA-368: orphaned rowids must be swept from the derived index tables
# ---------------------------------------------------------------------------

def test_reindex_sweeps_orphaned_fts_rowids(monkeypatch, tmp_path):
    """Reindexing a changed file must DELETE the old chunks' rowids from
    chunks_fts (derived), not just soft-delete the chunks rows. Pre-fix, the
    stale fts rowids lingered and polluted the candidate pool."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "mod.py"
    f.write_text("def alpha():\n    return 1\n")
    db_path = tmp_path / "code_search.db"
    _seed_db(db_path)
    monkeypatch.setenv("DEUS_CODE_SEARCH_DB", str(db_path))

    code_search.reindex(str(src))
    db = sqlite3.connect(str(db_path))
    fts_ok = code_search._fts_available(db)
    if not fts_ok:
        db.close()
        pytest.skip("chunks_fts not available")
    old_rowids = [
        r[0]
        for r in db.execute(
            "SELECT rowid FROM chunks WHERE orphaned_at IS NULL"
        ).fetchall()
    ]
    assert old_rowids
    assert (
        db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == len(old_rowids)
    )
    db.close()

    # Change the file so reindex re-chunks and soft-deletes the old chunks.
    f.write_text("def beta():\n    return 2\n\ndef gamma():\n    return 3\n")
    code_search.reindex(str(src))

    db = sqlite3.connect(str(db_path))
    orphaned = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE orphaned_at IS NOT NULL"
    ).fetchone()[0]
    assert orphaned == len(old_rowids)  # chunks rows preserved (soft-deleted)
    placeholders = ",".join("?" * len(old_rowids))
    stale_in_fts = db.execute(
        f"SELECT COUNT(*) FROM chunks_fts WHERE rowid IN ({placeholders})", old_rowids
    ).fetchone()[0]
    assert stale_in_fts == 0, "orphaned rowids must be swept from chunks_fts"
    live = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE orphaned_at IS NULL"
    ).fetchone()[0]
    assert db.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == live
    db.close()


def test_gc_index_sweeps_preexisting_orphan_rows(monkeypatch, tmp_path):
    """The --gc-index backfill removes EXISTING orphan derived rowids and is
    idempotent + backs up first."""
    db_path = tmp_path / "code_search.db"
    _seed_db(db_path)
    monkeypatch.setenv("DEUS_CODE_SEARCH_DB", str(db_path))

    db = code_search._init_db(db_path)
    if not code_search._fts_available(db):
        db.close()
        pytest.skip("chunks_fts not available")
    db.execute(
        "INSERT INTO chunks (id, file_path, chunk_type, chunk_name, chunk_index, "
        "content, content_hash, mtime) VALUES ('a','f.py','func','a',0,'x','h',0)"
    )
    live_rowid = db.execute("SELECT rowid FROM chunks WHERE id='a'").fetchone()[0]
    db.execute(
        "INSERT INTO chunks (id, file_path, chunk_type, chunk_name, chunk_index, "
        "content, content_hash, mtime, orphaned_at) "
        "VALUES ('b','f.py','func','b',0,'y','h2',0,'2020-01-01')"
    )
    dead_rowid = db.execute("SELECT rowid FROM chunks WHERE id='b'").fetchone()[0]
    db.execute(
        "INSERT INTO chunks_fts (rowid, chunk_name, content) VALUES (?,?,?)",
        (live_rowid, "a", "x"),
    )
    db.execute(
        "INSERT INTO chunks_fts (rowid, chunk_name, content) VALUES (?,?,?)",
        (dead_rowid, "b", "y"),
    )
    db.commit()
    db.close()

    result = code_search.gc_index()
    assert result["ok"]
    assert Path(result["backup"]).exists()  # backup written first
    assert result["fts_deleted"] == 1

    db = sqlite3.connect(str(db_path))
    rowids = [r[0] for r in db.execute("SELECT rowid FROM chunks_fts").fetchall()]
    assert live_rowid in rowids and dead_rowid not in rowids
    db.close()

    # Idempotent: a clean index sweeps nothing.
    assert code_search.gc_index()["fts_deleted"] == 0
