"""Smoke tests for the CLI (list, run --dry, report)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.bench.store import save_run
from scripts.bench.types import CaseResult, RunResult

_WORKTREE = Path(__file__).resolve().parent.parent.parent.parent


def _run_cli(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke cli.py as a subprocess using the current Python."""
    e = {**os.environ, **(env or {})}
    return subprocess.run(
        [sys.executable, "-m", "scripts.bench.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(_WORKTREE),
        env=e,
    )


def test_list_shows_memory_and_token(isolate_bench_db):
    r = _run_cli("list", env={"DEUS_BENCH_DB": str(isolate_bench_db)})
    assert r.returncode == 0, r.stderr
    names = r.stdout.strip().splitlines()
    assert "memory" in names
    assert "token" in names


def test_report_empty_db(isolate_bench_db):
    r = _run_cli("report", env={"DEUS_BENCH_DB": str(isolate_bench_db)})
    assert r.returncode == 0, r.stderr
    assert "no runs found" in r.stdout


def test_report_seeded_db(isolate_bench_db):
    # Seed with a run directly, then check report output
    save_run(RunResult(
        suite="memory",
        score=0.85,
        cases=[CaseResult(case_id="c1", score=0.85)],
        tokens_in=42,
        latency_ms=100,
    ))
    r = _run_cli("report", env={"DEUS_BENCH_DB": str(isolate_bench_db)})
    assert r.returncode == 0, r.stderr
    assert "memory" in r.stdout
    assert "0.850" in r.stdout


def test_run_unknown_suite_exits_1(isolate_bench_db):
    r = _run_cli("run", "_nonexistent_xyz", env={"DEUS_BENCH_DB": str(isolate_bench_db)})
    assert r.returncode == 1


def test_list_exit_code(isolate_bench_db):
    r = _run_cli("list", env={"DEUS_BENCH_DB": str(isolate_bench_db)})
    assert r.returncode == 0
