"""Smoke tests for the CLI (list, run --dry, report, diff)."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.bench.store import recent_runs, resolve_run, save_run
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


def _seed_run(suite: str, score: float, cases: list[tuple[str, float]], label: str | None = None) -> str:
    """Helper: seed a run with given cases directly via store."""
    result = RunResult(
        suite=suite,
        score=score,
        cases=[CaseResult(case_id=cid, score=s) for cid, s in cases],
        tokens_in=0,
    )
    return save_run(result, label=label)


def test_list_shows_memory_and_token(isolate_bench_db):
    r = _run_cli("list", env={"DEUS_BENCH_DB": str(isolate_bench_db)})
    assert r.returncode == 0, r.stderr
    names = r.stdout.strip().splitlines()
    assert "memory" in names
    assert "token" in names
    assert "hygiene" in names


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


# ── --label flag ──────────────────────────────────────────────────────────────

def test_report_shows_label_column(isolate_bench_db):
    save_run(RunResult(
        suite="memory",
        score=0.9,
        cases=[CaseResult(case_id="c1", score=0.9)],
        tokens_in=10,
    ), label="my-label")
    r = _run_cli("report", env={"DEUS_BENCH_DB": str(isolate_bench_db)})
    assert r.returncode == 0, r.stderr
    assert "label" in r.stdout
    assert "my-label" in r.stdout


# ── diff subcommand ───────────────────────────────────────────────────────────

def test_diff_basic_table(isolate_bench_db):
    """Two runs with 3 cases: assert all case_ids and correct deltas appear."""
    db = str(isolate_bench_db)
    id_a = _seed_run("token", 0.8, [("c1", 0.8), ("c2", 1.0), ("c3", 0.5)], label="pre")
    id_b = _seed_run("token", 0.9, [("c1", 0.9), ("c2", 1.0), ("c3", 0.6)], label="post")
    r = _run_cli("diff", id_a, id_b, env={"DEUS_BENCH_DB": db})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "c1" in r.stdout
    assert "c2" in r.stdout
    assert "c3" in r.stdout
    # c1 improved by 0.1
    assert "+0.100" in r.stdout or "+0.10" in r.stdout


def test_diff_by_label(isolate_bench_db):
    """Diff can be invoked using labels."""
    db = str(isolate_bench_db)
    _seed_run("token", 0.8, [("c1", 0.8)], label="before")
    _seed_run("token", 0.9, [("c1", 0.9)], label="after")
    r = _run_cli("diff", "before", "after", env={"DEUS_BENCH_DB": db})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "c1" in r.stdout


def test_diff_regression_exit_1(isolate_bench_db):
    """At least one regression → exit code 1."""
    db = str(isolate_bench_db)
    id_a = _seed_run("token", 1.0, [("c1", 1.0)])
    id_b = _seed_run("token", 0.5, [("c1", 0.5)])
    r = _run_cli("diff", id_a, id_b, env={"DEUS_BENCH_DB": db})
    assert r.returncode == 1, f"expected exit 1, got {r.returncode}\n{r.stdout}"
    assert "-regressed" in r.stdout


def test_diff_same_run_unchanged_exit_0(isolate_bench_db):
    """Diffing a run against itself → all unchanged, exit 0."""
    db = str(isolate_bench_db)
    run_id = _seed_run("token", 0.8, [("c1", 0.8), ("c2", 0.6)])
    r = _run_cli("diff", run_id, run_id, env={"DEUS_BENCH_DB": db})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "unchanged" in r.stdout
    assert "-regressed" not in r.stdout


def test_diff_unknown_arg_exit_2(isolate_bench_db):
    """Unknown argument → exit 2, stderr lists candidates."""
    db = str(isolate_bench_db)
    _seed_run("token", 0.8, [("c1", 0.8)], label="existing")
    r = _run_cli("diff", "ghost-run-xyz", "also-ghost", env={"DEUS_BENCH_DB": db})
    assert r.returncode == 2
    assert "ghost-run-xyz" in r.stderr


def test_diff_added_dropped_cases(isolate_bench_db):
    """Cases only in A are 'dropped'; cases only in B are 'added'."""
    db = str(isolate_bench_db)
    id_a = _seed_run("token", 0.8, [("c1", 0.8), ("old_case", 1.0)])
    id_b = _seed_run("token", 0.9, [("c1", 0.9), ("new_case", 1.0)])
    r = _run_cli("diff", id_a, id_b, env={"DEUS_BENCH_DB": db})
    assert r.returncode == 0, r.stderr + r.stdout
    assert "dropped" in r.stdout
    assert "added" in r.stdout


# ── growth alert ──────────────────────────────────────────────────────────────

def _seed_run_with_tokens(
    suite: str,
    score: float,
    cases: list[tuple[str, float, int]],
    suite_tokens_in: int = 0,
    label: str | None = None,
) -> str:
    """Seed a run with per-case tokens_in and suite-level tokens_in."""
    result = RunResult(
        suite=suite,
        score=score,
        cases=[
            CaseResult(case_id=cid, score=s, tokens_in=tok)
            for cid, s, tok in cases
        ],
        tokens_in=suite_tokens_in,
    )
    return save_run(result, label=label)


def test_diff_growth_alert_fires_on_token_growth_unchanged_score(isolate_bench_db):
    """tokens_in grew >5% with unchanged score → growth alert + exit 1."""
    db = str(isolate_bench_db)
    # c1: score unchanged 0.8→0.8, tokens 1000→1100 (10% growth > 5% threshold)
    id_a = _seed_run_with_tokens("token", 0.8, [("c1", 0.8, 1000)])
    id_b = _seed_run_with_tokens("token", 0.8, [("c1", 0.8, 1100)])
    r = _run_cli("diff", id_a, id_b, env={"DEUS_BENCH_DB": db})
    assert r.returncode == 1, f"expected exit 1, got {r.returncode}\n{r.stdout}"
    assert "growth alert" in r.stdout
    assert "c1" in r.stdout
    assert "+10.0%" in r.stdout


def test_diff_growth_alert_does_not_fire_when_score_changed(isolate_bench_db):
    """Score improved → no growth alert even if tokens grew."""
    db = str(isolate_bench_db)
    # c1: score changed 0.5→0.9, tokens 1000→1200 (20% growth but score changed)
    id_a = _seed_run_with_tokens("token", 0.5, [("c1", 0.5, 1000)])
    id_b = _seed_run_with_tokens("token", 0.9, [("c1", 0.9, 1200)])
    r = _run_cli("diff", id_a, id_b, env={"DEUS_BENCH_DB": db})
    assert "growth alert" not in r.stdout


def test_diff_growth_alert_does_not_fire_below_threshold(isolate_bench_db):
    """tokens grew 3% (below 5% default threshold) → no alert."""
    db = str(isolate_bench_db)
    id_a = _seed_run_with_tokens("token", 0.8, [("c1", 0.8, 1000)])
    id_b = _seed_run_with_tokens("token", 0.8, [("c1", 0.8, 1030)])
    r = _run_cli("diff", id_a, id_b, env={"DEUS_BENCH_DB": db})
    assert "growth alert" not in r.stdout


def test_diff_growth_alert_respects_custom_threshold(isolate_bench_db):
    """--growth-threshold 0.20 → 10% growth does not fire."""
    db = str(isolate_bench_db)
    id_a = _seed_run_with_tokens("token", 0.8, [("c1", 0.8, 1000)])
    id_b = _seed_run_with_tokens("token", 0.8, [("c1", 0.8, 1100)])
    r = _run_cli("diff", id_a, id_b, "--growth-threshold", "0.20", env={"DEUS_BENCH_DB": db})
    assert "growth alert" not in r.stdout
