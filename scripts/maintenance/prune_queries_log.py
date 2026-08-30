#!/usr/bin/env python3
"""
Prune the memory-tree SQLite query log (LIA-128).

`_log_query()` in scripts/memory_tree.py writes every memory-tree query to TWO
places: the `queries_log` table in `~/.deus/memory_tree.db` (override: $DEUS_TREE_DB)
and a JSONL twin at `~/.deus/memory_tree_queries.jsonl`. The JSONL half has been
bounded since LIA-218 by scripts/maintenance/rotate_query_log.py. The SQLite half
never was, so it grew to 98% of the database file by size (measured 2026-08-26:
914,145 rows / 164 MB of a 172 MB DB, against 378 nodes of actual memory content).

Every open_db(), migration, backup and VACUUM pays for that.

Who reads `queries_log`? Nothing in production. `git grep -na "FROM queries_log"`
returns two hits, both tests asserting that a write happened; neither depends on
history. `calibrate()` / `calibrate_sweep()` take a caller-supplied labeled dataset
and call `retrieve()` live. `mine_implicit_feedback.py` reads the JSONL twin, not
this table -- rotate_query_log.py's own docstring calls the SQLite copy a
"secondary copy". So the durable record of query history is the JSONL plus its
gzip archives, and this table is a bounded convenience cache.

Retention policy -- a row is doomed when EITHER bound rejects it:
  * it is older than --max-age-days (default 90), or
  * it is outside the newest --max-rows rows by id (default 50000).

The row cap is the load-bearing bound, not the age window. Real usage is roughly
100-800 rows/day, but a single benchmark or calibration sweep writes hundreds of
thousands in an afternoon (measured: 237,517 rows on one day). Against that shape
a 30-day window removed only a third of the backlog while a 50,000-row cap removes
94% of it and still holds several months of ordinary use.

Design -- archive before delete, mirroring the sibling JSONL rotator:
  1. Resolve the doomed id set from both bounds in one query.
  2. Stream those rows out as gzipped JSONL under `~/.deus/archive/`, in the same
     one-object-per-line shape mine_implicit_feedback.py already understands, so
     the history stays greppable after it leaves the DB.
  3. Only once that archive is durably renamed into place, DELETE the rows in a
     single transaction keyed on id.
  4. Optionally VACUUM (off by default; needed once after the first large trim to
     return the freed pages to the filesystem -- steady-state runs free a handful
     of pages that SQLite reuses, so a nightly VACUUM would rewrite the whole file
     for nothing).

Step 3 never runs if step 2 raised, so a failed archive leaves the table intact.
That ordering is what lets this hard-delete at all: docs/decisions/no-db-deletion.md
Rule 9 permits it for this table ONLY because the rows are preserved off-DB first.

Archives are pruned only after --archive-keep-days (default 365), so nothing is
silently lost within a year.

Run with --help for flags. Safe to run repeatedly: at or under both bounds it is a
no-op, so a second run neither deletes nor writes a duplicate archive.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import gzip
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# Same default + env override as scripts/memory_tree.py, so test isolation via
# $DEUS_TREE_DB works against both writer and pruner.
_DEFAULT_DB = os.environ.get("DEUS_TREE_DB", "~/.deus/memory_tree.db")  # LIA-128

# Columns are listed explicitly rather than SELECT *, so a future ALTER TABLE
# cannot silently drop a new column from the archive while the delete still
# removes the row that held it.
_COLUMNS = ("id", "ts", "query", "trace", "final_confidence", "route", "fell_back")


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _cutoff_iso(max_age_days: int) -> str:
    """The ISO-8601 timestamp `max_age_days` before now, matching `ts`'s format.

    `_log_query` writes a naive UTC ISO string, so the cutoff is rendered the same
    way (no offset suffix) for a correct lexicographic comparison in SQL.
    """
    return (_utc_now() - _dt.timedelta(days=max_age_days)).replace(tzinfo=None).isoformat()


def _date_of(ts: str) -> str:
    """The YYYY-MM-DD prefix of a `ts` value, or 'unknown'.

    Only the date (not the full ISO timestamp) is used in archive filenames -- the
    ISO `ts` contains ':' which is not a legal filename character on Windows, and
    day granularity is enough to identify an archive.
    """
    return ts[:10] if isinstance(ts, str) and len(ts) >= 10 else "unknown"


def _prune_archives(archive_dir: Path, keep_days: int, dry_run: bool, verbose: bool) -> int:
    """Delete archive gzips older than keep_days (by mtime). Best-effort."""
    if keep_days <= 0 or not archive_dir.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for gz in archive_dir.glob("queries_log-*.jsonl.gz"):
        try:
            if gz.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        if dry_run:
            print(f"  would prune archive {gz.name}")
            removed += 1
            continue
        try:
            gz.unlink()
            removed += 1
            if verbose:
                print(f"  pruned archive {gz.name}")
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"  WARN could not prune {gz}: {e}", file=sys.stderr)
    return removed


def _doomed_bounds(
    db: sqlite3.Connection, max_rows: int, max_age_days: int
) -> tuple[int | None, str | None, int]:
    """Resolve both bounds to (keep_min_id, cutoff_iso, doomed_count).

    keep_min_id is the lowest id the row cap would keep, or None when the table
    holds at most max_rows rows. cutoff_iso is None when max_age_days <= 0.
    """
    total = db.execute("SELECT COUNT(*) FROM queries_log").fetchone()[0]

    keep_min_id: int | None = None
    if max_rows > 0 and total > max_rows:
        # The id of the oldest row the cap keeps. AUTOINCREMENT ids are monotonic,
        # so ordering by id is ordering by insertion -- and unlike ordering by ts
        # it is total, so ties within the same second cannot straddle the boundary.
        row = db.execute(
            "SELECT id FROM queries_log ORDER BY id DESC LIMIT 1 OFFSET ?",
            (max_rows - 1,),
        ).fetchone()
        if row is not None:
            keep_min_id = int(row[0])

    cutoff = _cutoff_iso(max_age_days) if max_age_days > 0 else None

    where, params = _doomed_where(keep_min_id, cutoff)
    if where is None:
        return keep_min_id, cutoff, 0
    doomed = db.execute(
        f"SELECT COUNT(*) FROM queries_log WHERE {where}", params
    ).fetchone()[0]
    return keep_min_id, cutoff, int(doomed)


def _doomed_where(
    keep_min_id: int | None, cutoff: str | None
) -> tuple[str | None, tuple[object, ...]]:
    """The WHERE clause selecting doomed rows, or (None, ()) when nothing is doomed.

    The two bounds are OR'd: a row survives only if BOTH accept it.
    """
    clauses: list[str] = []
    params: list[object] = []
    if keep_min_id is not None:
        clauses.append("id < ?")
        params.append(keep_min_id)
    if cutoff is not None:
        clauses.append("ts < ?")
        params.append(cutoff)
    if not clauses:
        return None, ()
    return " OR ".join(clauses), tuple(params)


def _fsync_path(path: Path) -> None:
    """Flush a file's bytes to stable storage."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry (i.e. a rename) to stable storage.

    Best-effort: opening a directory O_RDONLY and fsyncing it is POSIX behaviour
    and a no-op that raises on Windows, where the atomic-rename durability story
    differs. The archive is still fsynced by _fsync_path either way, so a failure
    here costs the rename's durability, not the data's.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _verify_archive(path: Path) -> int:
    """Re-read a gzipped JSONL archive and return the number of valid rows in it.

    Every line is JSON-decoded and checked for the `id` field, so a truncated
    gzip stream, a partial final line, or a corrupted body raises here rather than
    being discovered after the rows it was meant to preserve are already deleted.
    """
    n = 0
    with gzip.open(path, "rt", encoding="utf-8") as gz:
        for line in gz:
            if not line.strip():
                continue
            if "id" not in json.loads(line):
                raise RuntimeError(f"archive {path.name} has a row with no id field")
            n += 1
    return n


def _archive_doomed(
    db: sqlite3.Connection, where: str, params: tuple[object, ...], archive_dir: Path
) -> tuple[Path, int]:
    """Stream every doomed row into a gzipped JSONL archive. Returns (path, count).

    Written to a .tmp and os.replace()d into place, so a crash mid-write can never
    leave a truncated archive that the delete step would then trust.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    bounds = db.execute(
        f"SELECT MIN(ts), MAX(ts) FROM queries_log WHERE {where}", params
    ).fetchone()
    first_d, last_d = _date_of(bounds[0]), _date_of(bounds[1])

    archive_path = archive_dir / f"queries_log-{first_d}_to_{last_d}.jsonl.gz"
    # Never clobber an existing same-date archive (reachable when a manual run
    # forces a smaller --max-rows on the same day). Disambiguate to preserve data.
    if archive_path.exists():
        n = 2
        while (archive_dir / f"queries_log-{first_d}_to_{last_d}-{n}.jsonl.gz").exists():
            n += 1
        archive_path = archive_dir / f"queries_log-{first_d}_to_{last_d}-{n}.jsonl.gz"

    tmp_gz = archive_path.with_suffix(".gz.tmp")
    written = 0
    cols = ", ".join(_COLUMNS)
    try:
        with gzip.open(tmp_gz, "wt", encoding="utf-8") as gz:
            # Streamed rather than fetchall()'d: the first run archives ~864k rows,
            # which would otherwise be materialised in memory all at once.
            for row in db.execute(
                f"SELECT {cols} FROM queries_log WHERE {where} ORDER BY id", params
            ):
                gz.write(json.dumps(dict(zip(_COLUMNS, row))) + "\n")
                written += 1
        # Verify by reading the archive BACK, not by trusting the write loop's own
        # counter. A counter proves the loop ran; re-reading proves the bytes are on
        # disk, the gzip stream is complete, and every line is parseable JSON -- the
        # only evidence that justifies deleting the rows it holds. Done on the .tmp,
        # before the rename, so an unverifiable archive never lands in the dir at all.
        verified = _verify_archive(tmp_gz)
        if verified != written:
            raise RuntimeError(
                f"archive {tmp_gz.name} verified {verified} rows but wrote {written}; "
                "refusing to delete"
            )
        # Durability, not just atomicity. os.replace is atomic, but the archive's
        # bytes and the rename itself may still sit in the page cache while the
        # DELETE below commits. A crash in that window would persist the deletion
        # and lose the archive -- exactly the outcome Rule 9's archive-before-delete
        # guarantee exists to prevent. So: fsync the file, rename, then fsync the
        # containing DIRECTORY (which is what makes the rename itself durable).
        _fsync_path(tmp_gz)
        os.replace(tmp_gz, archive_path)
        _fsync_dir(archive_dir)
    except BaseException:
        try:
            os.unlink(tmp_gz)
        except OSError:
            pass
        raise
    return archive_path, written


def prune(
    db_path: Path,
    max_rows: int,
    max_age_days: int,
    archive_keep_days: int,
    vacuum: bool,
    dry_run: bool,
    verbose: bool,
) -> tuple[int, int]:
    """Prune queries_log down to the retention bounds. Returns (deleted, kept)."""
    if not db_path.exists():
        print(f"prune_queries_log: no {db_path} - nothing to prune")
        return 0, 0

    archive_dir = db_path.parent / "archive"
    db = sqlite3.connect(str(db_path))
    # Match open_db()'s 30s busy_timeout in scripts/memory_tree.py. This runs from
    # the nightly maintenance block against a DB a live session may be writing, and
    # without it a momentary lock aborts the whole run with an uncaught
    # "database is locked" instead of waiting the contention out.
    db.execute("PRAGMA busy_timeout=30000")
    try:
        tbl = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='queries_log'"
        ).fetchone()
        if tbl is None:
            print(f"prune_queries_log: {db_path} has no queries_log table - nothing to prune")
            return 0, 0

        keep_min_id, cutoff, doomed = _doomed_bounds(db, max_rows, max_age_days)
        total = db.execute("SELECT COUNT(*) FROM queries_log").fetchone()[0]

        if doomed == 0:
            if verbose:
                print(
                    f"prune_queries_log: {total} rows within bounds "
                    f"(max_rows={max_rows}, max_age_days={max_age_days}) - no-op"
                )
            _prune_archives(archive_dir, archive_keep_days, dry_run, verbose)
            return 0, total

        where, params = _doomed_where(keep_min_id, cutoff)
        if where is None:
            # Unreachable: doomed > 0 implies at least one bound is active. Raised
            # rather than asserted so `python -O` cannot strip the guard and let a
            # `DELETE FROM queries_log WHERE None` reach the database.
            raise RuntimeError(
                f"{doomed} rows doomed but no bound is active; refusing to delete"
            )

        if dry_run:
            print(
                f"prune_queries_log: would archive+delete {doomed} rows, "
                f"keep {total - doomed} (total {total}, max_rows={max_rows}, "
                f"max_age_days={max_age_days})"
            )
            _prune_archives(archive_dir, archive_keep_days, dry_run, verbose)
            return doomed, total - doomed

        archive_path, archived = _archive_doomed(db, where, params, archive_dir)
        if archived != doomed:
            # A concurrent writer cannot inflate this (new rows get high ids and a
            # current ts, so they fail both bounds), which makes any mismatch a real
            # bug rather than a race. Refuse to delete on a mismatch.
            raise RuntimeError(
                f"archived {archived} rows but expected {doomed}; refusing to delete"
            )

        with db:
            db.execute(f"DELETE FROM queries_log WHERE {where}", params)
        kept = db.execute("SELECT COUNT(*) FROM queries_log").fetchone()[0]

        print(
            f"prune_queries_log: archived {archived} rows -> {archive_path.name}, "
            f"deleted {doomed}, kept {kept}"
        )

        if vacuum:
            before = db_path.stat().st_size
            # VACUUM cannot run inside a transaction; connect()'s implicit one is
            # already closed by the `with db` block above having committed.
            db.execute("VACUUM")
            after = db_path.stat().st_size
            print(
                f"prune_queries_log: VACUUM {before // 1024} KB -> {after // 1024} KB "
                f"(reclaimed {(before - after) // 1024} KB)"
            )

        _prune_archives(archive_dir, archive_keep_days, dry_run, verbose)
        return doomed, kept
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--db", type=Path, default=Path(os.path.expanduser(_DEFAULT_DB)),
        help="Memory-tree DB to prune (default: $DEUS_TREE_DB or ~/.deus/memory_tree.db).",
    )
    parser.add_argument(
        "--max-rows", type=int,
        default=int(os.environ.get("DEUS_TREE_QLOG_MAX_ROWS", "50000")),  # LIA-128
        help="Keep at most this many of the newest rows (default: 50000; 0 disables).",
    )
    parser.add_argument(
        "--max-age-days", type=int,
        default=int(os.environ.get("DEUS_TREE_QLOG_MAX_AGE_DAYS", "90")),  # LIA-128
        help="Also drop rows older than this many days (default: 90; 0 disables).",
    )
    parser.add_argument(
        "--archive-keep-days", type=int,
        default=int(os.environ.get("DEUS_TREE_QLOG_ARCHIVE_KEEP_DAYS", "365")),  # LIA-128
        help="Prune archive gzips older than this many days (default: 365; 0 disables).",
    )
    parser.add_argument(
        "--vacuum", action="store_true",
        help="VACUUM after deleting, to return freed pages to the filesystem. "
             "Off by default: only the first large trim needs it.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen; touch nothing.")
    parser.add_argument("--verbose", action="store_true",
                        help="Also print no-op and per-archive detail.")
    args = parser.parse_args(argv)

    if args.max_rows < 0 or args.max_age_days < 0:
        print("--max-rows and --max-age-days must be >= 0", file=sys.stderr)
        return 1
    if args.max_rows == 0 and args.max_age_days == 0:
        # Both bounds disabled would delete every row on the next run.
        print("--max-rows and --max-age-days cannot both be 0", file=sys.stderr)
        return 1

    prune(
        args.db, args.max_rows, args.max_age_days,
        args.archive_keep_days, args.vacuum, args.dry_run, args.verbose,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
