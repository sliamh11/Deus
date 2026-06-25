"""Tests for LIA-218 query-log rotation (scripts/maintenance/rotate_query_log.py).

Hermetic: every test builds a throwaway log under pytest's tmp_path, so nothing
touches the real ~/.deus/memory_tree_queries.jsonl. Lines mimic the production
JSONL shape (one JSON object per line, with a `ts` field).
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path

_MOD_PATH = (
    Path(__file__).resolve().parents[1] / "maintenance" / "rotate_query_log.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("rotate_query_log", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rotate_query_log"] = mod
    spec.loader.exec_module(mod)
    return mod


rql = _load()


def _write_log(path: Path, n: int, day: str = "2026-06-01") -> None:
    """Write n production-shaped JSONL lines."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for i in range(n):
            f.write(json.dumps({
                "ts": f"{day}T00:00:{i % 60:02d}",
                "query": f"query number {i}",
                "trace": [],
                "final_confidence": 0.5,
                "results": [],
                "fell_back": False,
            }) + "\n")


def _archive_lines(archive_dir: Path) -> int:
    total = 0
    for gz in archive_dir.glob("memory_tree_queries-*.jsonl.gz"):
        with gzip.open(gz, "rt") as f:
            total += sum(1 for _ in f)
    return total


def test_under_threshold_is_noop(tmp_path: Path):
    log = tmp_path / "queries.jsonl"
    _write_log(log, 50)
    before = log.read_bytes()

    archived, kept = rql.rotate(log, max_lines=100, keep_lines=50,
                                archive_keep_days=365, dry_run=False, verbose=False)

    assert (archived, kept) == (0, 50)
    assert log.read_bytes() == before  # untouched
    assert not (tmp_path / "archive").exists()


def test_over_threshold_archives_and_keeps_without_loss(tmp_path: Path):
    log = tmp_path / "queries.jsonl"
    _write_log(log, 1000)

    archived, kept = rql.rotate(log, max_lines=100, keep_lines=50,
                                archive_keep_days=365, dry_run=False, verbose=False)

    assert kept == 50
    assert archived == 950
    # Live file trimmed to exactly the last 50 lines, all valid JSON.
    live = log.read_text().splitlines()
    assert len(live) == 50
    for line in live:
        json.loads(line)
    # The last live line is the original last line (recency preserved).
    assert json.loads(live[-1])["query"] == "query number 999"
    # No loss: archived + kept == original total.
    archive_dir = tmp_path / "archive"
    assert _archive_lines(archive_dir) == 950
    assert _archive_lines(archive_dir) + len(live) == 1000


def test_second_run_is_noop(tmp_path: Path):
    log = tmp_path / "queries.jsonl"
    _write_log(log, 1000)
    rql.rotate(log, max_lines=100, keep_lines=50, archive_keep_days=365,
               dry_run=False, verbose=False)
    after_first = log.read_bytes()
    archive_count_first = len(list((tmp_path / "archive").glob("*.jsonl.gz")))

    archived, kept = rql.rotate(log, max_lines=100, keep_lines=50,
                                archive_keep_days=365, dry_run=False, verbose=False)

    assert archived == 0  # now 50 lines <= max 100
    assert log.read_bytes() == after_first  # idempotent, no re-trim
    # No duplicate archive produced on the no-op pass.
    assert len(list((tmp_path / "archive").glob("*.jsonl.gz"))) == archive_count_first


def test_dry_run_touches_nothing(tmp_path: Path):
    log = tmp_path / "queries.jsonl"
    _write_log(log, 1000)
    before_bytes = log.read_bytes()
    before_mtime = log.stat().st_mtime

    archived, kept = rql.rotate(log, max_lines=100, keep_lines=50,
                                archive_keep_days=365, dry_run=True, verbose=False)

    assert (archived, kept) == (950, 50)  # reports intent
    assert log.read_bytes() == before_bytes  # live untouched
    assert log.stat().st_mtime == before_mtime
    assert not (tmp_path / "archive").exists()  # no archive written


def test_archive_filename_uses_date_range(tmp_path: Path):
    log = tmp_path / "queries.jsonl"
    _write_log(log, 1000, day="2026-04-14")

    rql.rotate(log, max_lines=100, keep_lines=50, archive_keep_days=365,
               dry_run=False, verbose=False)

    names = [p.name for p in (tmp_path / "archive").glob("*.jsonl.gz")]
    assert len(names) == 1
    # Date-only range (no colons → Windows-safe filename).
    assert names[0] == "memory_tree_queries-2026-04-14_to_2026-04-14.jsonl.gz"
    assert ":" not in names[0]


def test_same_date_archive_is_not_clobbered(tmp_path: Path):
    # A pre-existing archive with the same date range must survive a second
    # rotation on the same day (manual --max-lines forcing edge).
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    existing = archive_dir / "memory_tree_queries-2026-04-14_to_2026-04-14.jsonl.gz"
    with gzip.open(existing, "wt") as f:
        f.write('{"prior": "archive"}\n')

    log = tmp_path / "queries.jsonl"
    _write_log(log, 1000, day="2026-04-14")
    rql.rotate(log, max_lines=100, keep_lines=50, archive_keep_days=365,
               dry_run=False, verbose=False)

    names = sorted(p.name for p in archive_dir.glob("*.jsonl.gz"))
    # Both the prior archive and the new -2 archive exist; nothing overwritten.
    assert names == [
        "memory_tree_queries-2026-04-14_to_2026-04-14-2.jsonl.gz",
        "memory_tree_queries-2026-04-14_to_2026-04-14.jsonl.gz",
    ]
    with gzip.open(existing, "rt") as f:
        assert f.read() == '{"prior": "archive"}\n'  # untouched


def test_missing_log_is_noop(tmp_path: Path):
    log = tmp_path / "does-not-exist.jsonl"
    archived, kept = rql.rotate(log, max_lines=100, keep_lines=50,
                                archive_keep_days=365, dry_run=False, verbose=False)
    assert (archived, kept) == (0, 0)
