#!/usr/bin/env python3
"""
Rotate the memory-tree query log (LIA-218).

`_log_query()` in scripts/memory_tree.py appends one JSON line per memory-tree
query to `~/.deus/memory_tree_queries.jsonl` (override: $DEUS_TREE_LOG). It is
append-only with no cap, so benchmark/calibration sweeps grow it without bound
(observed: 248 MB / 710k lines). An oversized log slows the one consumer that
reads the whole file — `scripts/mine_implicit_feedback.py` — and dilutes its
real-user signal with synthetic noise.

This standalone maintenance tool keeps the most recent N lines live and ARCHIVES
the older prefix (gzipped, never deleted) under `~/.deus/archive/`. It is wired
into scripts/maintenance.py's daily block (launchd 04:30), so the live file is
bounded on a real schedule while the full history stays recoverable.

Design — copy-on-write swap with tail-capture:
  1. Record the live file's byte size S, read bytes [0, S) and split into the
     archive prefix and the kept tail by line position.       O(N) on line count
  2. gzip the archive prefix to a dated archive file.
  3. Capture any bytes appended in [S, EOF) since step 1 (the writer never
     blocks — it opens `open("a")` fresh per line and swallows errors — so we
     cannot lock it; instead we re-read the delta up to 3x until stable).
  4. Write `kept + delta` to a temp file on the SAME filesystem and
     `os.replace()` it over the live log.                              O(1) swap

The residual sub-millisecond race (an append landing between the final delta
read and `os.replace`) is mitigated by running at the 04:30 idle window and by
the SQLite `queries_log` secondary copy in memory_tree.py (most fields survive
there regardless). Archives are pruned only after --archive-keep-days (default
365), so nothing is silently lost within a year.

Run with --help for flags. Safe to run repeatedly: below --max-lines it is a
no-op, so a second run never produces a duplicate archive.
"""
from __future__ import annotations

import argparse
import gzip
import os
import sys
import tempfile
import time
from pathlib import Path

# Same default + env override as scripts/memory_tree.py:_LOG_PATH, so test
# isolation via $DEUS_TREE_LOG works against both writer and rotator.
_DEFAULT_LOG = os.environ.get("DEUS_TREE_LOG", "~/.deus/memory_tree_queries.jsonl")


def _count_lines(path: Path) -> int:
    n = 0
    with path.open("rb") as f:
        for _ in f:
            n += 1
    return n


def _date_of(line: bytes) -> str:
    """The YYYY-MM-DD prefix of a log line's `ts` field, or 'unknown'.

    Only the date (not the full ISO timestamp) is used in archive filenames —
    the ISO `ts` contains ':' which is not a legal filename character on
    Windows, and day granularity is enough to identify an archive.
    """
    try:
        import json

        ts = json.loads(line).get("ts", "")
        return ts[:10] if len(ts) >= 10 else "unknown"
    except Exception:
        return "unknown"


def _prune_archives(archive_dir: Path, keep_days: int, dry_run: bool, verbose: bool) -> int:
    """Delete archive gzips older than keep_days (by mtime). Best-effort."""
    if keep_days <= 0 or not archive_dir.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for gz in archive_dir.glob("memory_tree_queries-*.jsonl.gz"):
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


def rotate(
    log_path: Path,
    max_lines: int,
    keep_lines: int,
    archive_keep_days: int,
    dry_run: bool,
    verbose: bool,
) -> tuple[int, int]:
    """Rotate the log if it exceeds max_lines. Returns (archived, kept) lines.

    No-op (returns (0, total)) when the file is missing or at/under max_lines.
    """
    if not log_path.exists():
        print(f"rotate_query_log: no {log_path} — nothing to rotate")
        return 0, 0

    # [1] Freeze the size we are responsible for and read that prefix. Anything
    #     appended after S is captured separately in [3] so it is never lost.
    size_s = log_path.stat().st_size
    with log_path.open("rb") as f:
        prefix = f.read(size_s)
    lines = prefix.splitlines(keepends=True)
    total = len(lines)

    if total <= max_lines:
        if verbose:
            print(f"rotate_query_log: {total} lines <= max {max_lines} — no-op")
        # Still prune stale archives even when the live file is small.
        _prune_archives(log_path.parent / "archive", archive_keep_days, dry_run, verbose)
        return 0, total

    archive_lines = lines[: total - keep_lines]
    kept = lines[total - keep_lines:]
    archive_count = len(archive_lines)

    if dry_run:
        print(
            f"rotate_query_log: would archive {archive_count} lines, "
            f"keep {len(kept)} (total {total}, max {max_lines})"
        )
        _prune_archives(log_path.parent / "archive", archive_keep_days, dry_run, verbose)
        return archive_count, len(kept)

    # [2] gzip the archive prefix to a dated file (write temp, then rename).
    archive_dir = log_path.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    first_d, last_d = _date_of(archive_lines[0]), _date_of(archive_lines[-1])
    archive_path = archive_dir / f"memory_tree_queries-{first_d}_to_{last_d}.jsonl.gz"
    # Never clobber an existing same-date archive (possible only under manual
    # --max-lines forcing; the normal daily schedule no-ops once the live file
    # is back under max_lines). Disambiguate with a -N suffix to preserve data.
    if archive_path.exists():
        n = 2
        while (archive_dir / f"memory_tree_queries-{first_d}_to_{last_d}-{n}.jsonl.gz").exists():
            n += 1
        archive_path = archive_dir / f"memory_tree_queries-{first_d}_to_{last_d}-{n}.jsonl.gz"
    tmp_gz = archive_path.with_suffix(".gz.tmp")
    with gzip.open(tmp_gz, "wb") as gz:
        for line in archive_lines:
            gz.write(line)
    os.replace(tmp_gz, archive_path)

    # [3] Capture bytes appended since [1] (re-read up to 3x; keep the last
    #     snapshot if still growing — safe here because each writer append is a
    #     single write() syscall, so a read never sees a half-written line).
    delta = b""
    for _ in range(3):
        cur = log_path.stat().st_size
        if cur <= size_s:
            break
        with log_path.open("rb") as f:
            f.seek(size_s)
            new = f.read()
        if new == delta:
            break
        delta = new

    # [4] Atomically replace the live log with kept tail + captured delta.
    #     Temp file lives in the SAME directory (filesystem) so os.replace is
    #     atomic — a /tmp temp could be a cross-device rename and fail.
    # TODO(windows, 2026-06-20): os.replace over a file another process holds
    #     open for append raises PermissionError on Windows. Revisit when the
    #     deus-cmd.sh Windows port lands (Windows support is blocked on it today).
    fd, tmp_name = tempfile.mkstemp(dir=str(log_path.parent), prefix=".qlog-rot-")
    try:
        with os.fdopen(fd, "wb") as out:
            for line in kept:
                out.write(line)
            if delta:
                out.write(delta)
        os.replace(tmp_name, log_path)
    except BaseException:
        # Leave the live log untouched on any failure; clean up the temp.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    print(
        f"rotate_query_log: archived {archive_count} lines -> {archive_path.name}, "
        f"kept {len(kept)} live"
    )
    _prune_archives(archive_dir, archive_keep_days, dry_run, verbose)
    return archive_count, len(kept)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--log-path", type=Path, default=Path(os.path.expanduser(_DEFAULT_LOG)),
        help="Query log to rotate (default: $DEUS_TREE_LOG or ~/.deus/memory_tree_queries.jsonl).",
    )
    parser.add_argument(
        "--max-lines", type=int,
        default=int(os.environ.get("DEUS_TREE_LOG_MAX_LINES", "100000")),  # LIA-218
        help="Rotate only when the log exceeds this many lines (default: 100000).",
    )
    parser.add_argument(
        "--keep-lines", type=int,
        default=int(os.environ.get("DEUS_TREE_LOG_KEEP_LINES", "50000")),  # LIA-218
        help="Lines to retain live after rotation (default: 50000).",
    )
    parser.add_argument(
        "--archive-keep-days", type=int,
        default=int(os.environ.get("DEUS_TREE_LOG_ARCHIVE_KEEP_DAYS", "365")),  # LIA-218
        help="Prune archive gzips older than this many days (default: 365; 0 disables).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen; touch nothing.")
    parser.add_argument("--verbose", action="store_true",
                        help="Also print no-op and per-archive detail.")
    args = parser.parse_args(argv)

    if args.keep_lines < 0 or args.max_lines < 0:
        print("--max-lines and --keep-lines must be >= 0", file=sys.stderr)
        return 1
    if args.keep_lines > args.max_lines:
        # Keeping more than the rotate threshold would never shrink the file.
        print("--keep-lines must be <= --max-lines", file=sys.stderr)
        return 1

    rotate(
        args.log_path, args.max_lines, args.keep_lines,
        args.archive_keep_days, args.dry_run, args.verbose,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
