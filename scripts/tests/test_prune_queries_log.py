"""Tests for LIA-128 queries_log retention (scripts/maintenance/prune_queries_log.py).

Hermetic: every test builds a throwaway SQLite DB under pytest's tmp_path, so nothing
touches the real memory_tree.db. Rows mimic the production `queries_log` shape written
by `_log_query()` in scripts/memory_tree.py.
"""
from __future__ import annotations

import datetime as _dt
import gzip
import importlib.util
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

_MOD_PATH = (
    Path(__file__).resolve().parents[1] / "maintenance" / "prune_queries_log.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("prune_queries_log", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["prune_queries_log"] = mod
    spec.loader.exec_module(mod)
    return mod


pql = _load()

# Mirrors scripts/memory_tree.py:866 exactly -- if that schema changes, these tests
# should start failing rather than silently testing a shape that no longer exists.
_SCHEMA = """
CREATE TABLE queries_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    query            TEXT NOT NULL,
    trace            TEXT NOT NULL,
    final_confidence REAL NOT NULL,
    route            TEXT NOT NULL,
    fell_back        INTEGER NOT NULL DEFAULT 0
)
"""


def _iso_days_ago(days: int) -> str:
    """A naive-UTC ISO timestamp, matching what _log_query writes."""
    return (
        _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days)
    ).replace(tzinfo=None).isoformat()


def _make_db(path: Path, rows: list[tuple[str, str]]) -> Path:
    """Build a queries_log DB. rows = [(ts, query), ...], inserted in order."""
    db = sqlite3.connect(str(path))
    db.execute(_SCHEMA)
    db.executemany(
        "INSERT INTO queries_log (ts, query, trace, final_confidence, route, fell_back)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(ts, q, "[]", 0.5, "vector", 0) for ts, q in rows],
    )
    db.commit()
    db.close()
    return path


def _recent(n: int, day_offset: int = 0) -> list[tuple[str, str]]:
    return [(_iso_days_ago(day_offset), f"query number {i}") for i in range(n)]


def _count(path: Path) -> int:
    db = sqlite3.connect(str(path))
    try:
        return db.execute("SELECT COUNT(*) FROM queries_log").fetchone()[0]
    finally:
        db.close()


def _archives(path: Path) -> list[Path]:
    return sorted((path.parent / "archive").glob("queries_log-*.jsonl.gz"))


def _archived_rows(gz_path: Path) -> list[dict]:
    with gzip.open(gz_path, "rt", encoding="utf-8") as gz:
        return [json.loads(line) for line in gz if line.strip()]


def test_within_bounds_is_a_noop(tmp_path):
    db_path = _make_db(tmp_path / "t.db", _recent(10))
    deleted, kept = pql.prune(db_path, 50, 90, 365, False, False, False)
    assert (deleted, kept) == (0, 10)
    assert _count(db_path) == 10
    assert _archives(db_path) == []


def test_empty_table_is_a_noop(tmp_path):
    db_path = _make_db(tmp_path / "t.db", [])
    deleted, kept = pql.prune(db_path, 50, 90, 365, False, False, False)
    assert (deleted, kept) == (0, 0)
    assert _archives(db_path) == []


def test_missing_db_is_a_noop(tmp_path):
    deleted, kept = pql.prune(tmp_path / "nope.db", 50, 90, 365, False, False, False)
    assert (deleted, kept) == (0, 0)


def test_db_without_queries_log_table_is_a_noop(tmp_path):
    """A memory.db-shaped sibling must not crash the daily maintenance run."""
    db_path = tmp_path / "other.db"
    db = sqlite3.connect(str(db_path))
    db.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
    db.commit()
    db.close()
    deleted, kept = pql.prune(db_path, 50, 90, 365, False, False, False)
    assert (deleted, kept) == (0, 0)


def test_row_cap_keeps_the_newest_rows(tmp_path):
    db_path = _make_db(tmp_path / "t.db", _recent(100))
    deleted, kept = pql.prune(db_path, 30, 0, 365, False, False, False)
    assert (deleted, kept) == (70, 30)
    assert _count(db_path) == 30

    db = sqlite3.connect(str(db_path))
    try:
        surviving = {r[0] for r in db.execute("SELECT query FROM queries_log")}
    finally:
        db.close()
    # Inserted in order, so the newest 30 are queries 70..99.
    assert surviving == {f"query number {i}" for i in range(70, 100)}


def test_age_bound_drops_old_rows(tmp_path):
    db_path = _make_db(
        tmp_path / "t.db",
        [(_iso_days_ago(200), "ancient"), (_iso_days_ago(120), "old")] + _recent(5),
    )
    deleted, kept = pql.prune(db_path, 0, 90, 365, False, False, False)
    assert (deleted, kept) == (2, 5)

    db = sqlite3.connect(str(db_path))
    try:
        surviving = {r[0] for r in db.execute("SELECT query FROM queries_log")}
    finally:
        db.close()
    assert "ancient" not in surviving and "old" not in surviving


def test_bounds_are_ored_so_a_row_survives_only_if_both_accept(tmp_path):
    """An OLD row inside the row cap is still deleted by the age bound.

    This is the case a naive AND would get wrong: the row is one of the newest 10,
    so the cap keeps it, but it is 200 days old so the age bound must still drop it.
    """
    db_path = _make_db(tmp_path / "t.db", [(_iso_days_ago(200), "old_but_recent_id")]
                       + _recent(5))
    deleted, kept = pql.prune(db_path, 10, 90, 365, False, False, False)
    assert (deleted, kept) == (1, 5)

    db = sqlite3.connect(str(db_path))
    try:
        surviving = {r[0] for r in db.execute("SELECT query FROM queries_log")}
    finally:
        db.close()
    assert "old_but_recent_id" not in surviving


def test_dry_run_mutates_nothing(tmp_path):
    db_path = _make_db(tmp_path / "t.db", _recent(100))
    deleted, kept = pql.prune(db_path, 30, 90, 365, False, True, False)
    assert (deleted, kept) == (70, 30)
    # Reported, not performed.
    assert _count(db_path) == 100
    assert _archives(db_path) == []


def test_archive_round_trips_every_deleted_row(tmp_path):
    db_path = _make_db(tmp_path / "t.db", _recent(100))
    pql.prune(db_path, 30, 0, 365, False, False, False)

    archives = _archives(db_path)
    assert len(archives) == 1
    rows = _archived_rows(archives[0])
    assert len(rows) == 70
    assert {r["query"] for r in rows} == {f"query number {i}" for i in range(70)}
    # Every production column survives the round trip, not just the query text.
    assert set(rows[0]) == set(pql._COLUMNS)
    assert rows[0]["final_confidence"] == 0.5
    assert rows[0]["route"] == "vector"


def test_second_run_is_idempotent(tmp_path):
    db_path = _make_db(tmp_path / "t.db", _recent(100))
    pql.prune(db_path, 30, 0, 365, False, False, False)
    deleted, kept = pql.prune(db_path, 30, 0, 365, False, False, False)
    assert (deleted, kept) == (0, 30)
    # No duplicate archive from the no-op run.
    assert len(_archives(db_path)) == 1


def test_same_day_archive_is_not_clobbered(tmp_path):
    db_path = _make_db(tmp_path / "t.db", _recent(100))
    pql.prune(db_path, 30, 0, 365, False, False, False)
    pql.prune(db_path, 10, 0, 365, False, False, False)
    archives = _archives(db_path)
    assert len(archives) == 2
    # Both archives together still hold every deleted row -- nothing was overwritten.
    total = sum(len(_archived_rows(a)) for a in archives)
    assert total == 90


def test_a_failed_archive_aborts_before_any_delete(tmp_path, monkeypatch):
    """The delete must never run if archiving raised. This is what Rule 9 rests on."""
    db_path = _make_db(tmp_path / "t.db", _recent(100))

    def _boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(pql, "_archive_doomed", _boom)
    with pytest.raises(OSError):
        pql.prune(db_path, 30, 0, 365, False, False, False)
    # Every row still present.
    assert _count(db_path) == 100


def test_unverifiable_archive_aborts_before_any_delete(tmp_path, monkeypatch):
    """A short/corrupt archive must abort too, not just an outright exception."""
    db_path = _make_db(tmp_path / "t.db", _recent(100))

    monkeypatch.setattr(pql, "_verify_archive", lambda p: 1)
    with pytest.raises(RuntimeError, match="refusing to delete"):
        pql.prune(db_path, 30, 0, 365, False, False, False)
    assert _count(db_path) == 100
    # The unverifiable archive was never renamed into the archive dir.
    assert _archives(db_path) == []


def test_archive_is_fsynced_before_the_delete_commits(tmp_path, monkeypatch):
    """Rule 9 needs the archive DURABLE before the DELETE, not merely renamed.

    Records the order of (fsync of the archive, fsync of the archive dir, DELETE)
    and asserts both fsyncs land first. Without them a crash between rename and
    commit persists the deletion and loses the archive.
    """
    db_path = _make_db(tmp_path / "t.db", _recent(100))
    order = []

    real_fsync_path = pql._fsync_path
    real_fsync_dir = pql._fsync_dir

    def spy_path(p):
        order.append("fsync_file")
        return real_fsync_path(p)

    def spy_dir(p):
        order.append("fsync_dir")
        return real_fsync_dir(p)

    monkeypatch.setattr(pql, "_fsync_path", spy_path)
    monkeypatch.setattr(pql, "_fsync_dir", spy_dir)

    # Connection.execute is read-only, so the spy has to be a subclass supplied
    # via connect(factory=...) rather than an attribute patched onto an instance.
    class _SpyConn(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.strip().upper().startswith("DELETE"):
                order.append("delete")
            return super().execute(sql, *args, **kwargs)

    real_connect = sqlite3.connect
    monkeypatch.setattr(
        sqlite3, "connect",
        lambda *a, **kw: real_connect(*a, **{**kw, "factory": _SpyConn}),
    )
    pql.prune(db_path, 30, 0, 365, False, False, False)

    assert "delete" in order, "the delete never ran"
    assert order.index("fsync_file") < order.index("delete")
    assert order.index("fsync_dir") < order.index("delete")
    assert _count(db_path) == 30


def test_verify_archive_rejects_a_truncated_gzip(tmp_path):
    good = tmp_path / "a.jsonl.gz"
    with gzip.open(good, "wt", encoding="utf-8") as gz:
        gz.write(json.dumps({"id": 1, "ts": "2026-01-01T00:00:00"}) + "\n")
    assert pql._verify_archive(good) == 1

    truncated = tmp_path / "b.jsonl.gz"
    truncated.write_bytes(good.read_bytes()[:-5])
    with pytest.raises(Exception):
        pql._verify_archive(truncated)


def test_vacuum_reclaims_file_size(tmp_path):
    # Rows big enough that 900 of them span many pages, so the reclaim is visible.
    rows = [(_iso_days_ago(0), "x" * 2000) for _ in range(1000)]
    db_path = _make_db(tmp_path / "t.db", rows)
    before = db_path.stat().st_size

    pql.prune(db_path, 10, 0, 365, True, False, False)
    after = db_path.stat().st_size
    assert after < before
    assert _count(db_path) == 10


def test_no_vacuum_leaves_file_size_alone(tmp_path):
    rows = [(_iso_days_ago(0), "x" * 2000) for _ in range(1000)]
    db_path = _make_db(tmp_path / "t.db", rows)
    before = db_path.stat().st_size

    pql.prune(db_path, 10, 0, 365, False, False, False)
    # Pages are freed for reuse but not returned to the filesystem.
    assert db_path.stat().st_size >= before
    assert _count(db_path) == 10


def test_old_archives_are_pruned_by_age(tmp_path):
    db_path = _make_db(tmp_path / "t.db", _recent(10))
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    stale = archive_dir / "queries_log-2020-01-01_to_2020-01-02.jsonl.gz"
    with gzip.open(stale, "wt", encoding="utf-8") as gz:
        gz.write(json.dumps({"id": 1}) + "\n")
    old = time.time() - 400 * 86400
    os.utime(stale, (old, old))

    fresh = archive_dir / "queries_log-2026-01-01_to_2026-01-02.jsonl.gz"
    with gzip.open(fresh, "wt", encoding="utf-8") as gz:
        gz.write(json.dumps({"id": 2}) + "\n")

    pql.prune(db_path, 50, 90, 365, False, False, False)
    assert not stale.exists()
    assert fresh.exists()


def test_archive_keep_days_zero_disables_archive_pruning(tmp_path):
    db_path = _make_db(tmp_path / "t.db", _recent(10))
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    stale = archive_dir / "queries_log-2020-01-01_to_2020-01-02.jsonl.gz"
    with gzip.open(stale, "wt", encoding="utf-8") as gz:
        gz.write(json.dumps({"id": 1}) + "\n")
    old = time.time() - 400 * 86400
    os.utime(stale, (old, old))

    pql.prune(db_path, 50, 90, 0, False, False, False)
    assert stale.exists()


def test_cli_rejects_both_bounds_disabled(tmp_path):
    """Both bounds off would delete the whole table on the next run."""
    assert pql.main(["--db", str(tmp_path / "t.db"), "--max-rows", "0",
                     "--max-age-days", "0"]) == 1


def test_cli_rejects_negative_bounds(tmp_path):
    assert pql.main(["--db", str(tmp_path / "t.db"), "--max-rows", "-1"]) == 1
    assert pql.main(["--db", str(tmp_path / "t.db"), "--max-age-days", "-1"]) == 1


def test_cli_dry_run_end_to_end(tmp_path):
    db_path = _make_db(tmp_path / "t.db", _recent(100))
    assert pql.main(["--db", str(db_path), "--max-rows", "30", "--dry-run"]) == 0
    assert _count(db_path) == 100
