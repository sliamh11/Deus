"""Tests for scripts/maintenance.py — KB daily maintenance orchestrator."""
import importlib.util
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

MAINTENANCE_PATH = Path(__file__).parent.parent / "maintenance.py"


@pytest.fixture
def maint():
    spec = importlib.util.spec_from_file_location("kb_maintenance", str(MAINTENANCE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_run_task_success(maint):
    """run_task returns True on successful subprocess."""
    with patch.object(subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="OK\n", stderr="")
        assert maint.run_task("test", [sys.executable, "-c", "pass"]) is True


def test_run_task_failure(maint):
    """run_task returns False on non-zero exit code."""
    with patch.object(subprocess, "run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error\n")
        assert maint.run_task("test", [sys.executable, "-c", "pass"]) is False


def test_run_task_timeout(maint):
    """run_task returns False on timeout."""
    with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("cmd", 300)):
        assert maint.run_task("test", [sys.executable, "-c", "pass"]) is False


def test_run_task_dry_run_skips(maint, capsys):
    """run_task in dry-run mode doesn't execute, returns True."""
    result = maint.run_task("test", [sys.executable, "-c", "should not run"], dry_run=True)
    assert result is True
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_run_task_isolates_failures(maint):
    """One task failure doesn't prevent other tasks from running."""
    results = []
    with patch.object(subprocess, "run") as mock_run:
        # First call fails, second succeeds
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="fail"),
            MagicMock(returncode=0, stdout="ok", stderr=""),
        ]
        results.append(maint.run_task("fail_task", [sys.executable, "-c", "x"]))
        results.append(maint.run_task("ok_task", [sys.executable, "-c", "x"]))
    assert results == [False, True]


def test_maintenance_has_daily_tasks(maint):
    """main() should reference prune, decay, health, and memory_gc."""
    import inspect
    source = inspect.getsource(maint.main)
    for task in ["prune", "decay", "health", "gc"]:
        assert task in source, f"Daily task '{task}' not found in main()"


def test_maintenance_has_weekly_gate(maint):
    """Weekly tasks should be gated by day-of-week check."""
    import inspect
    source = inspect.getsource(maint.main)
    assert "weekday" in source, "Weekly gate should check weekday()"
