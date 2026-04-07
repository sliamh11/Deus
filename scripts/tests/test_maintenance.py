"""
Tests for evolution/maintenance.py.

Covers:
  - is_maintenance_due() scheduling logic (never ran, interval-based)
  - run_maintenance() archive trigger and sentinel bookkeeping
  - CLI entry-point via __main__ module
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root is importable
_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import evolution.config as config_mod
import evolution.db as db_mod


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def patch_db_path(tmp_path, monkeypatch):
    """Redirect evolution DB_PATH to a temp file and ensure providers are registered."""
    test_db_path = tmp_path / "test_maint.db"
    monkeypatch.setattr(db_mod, "DB_PATH", test_db_path)
    monkeypatch.setattr(config_mod, "DB_PATH", test_db_path)

    # Re-register built-in storage providers unconditionally.
    # test_storage_provider.py has an autouse fixture that calls StorageRegistry.reset()
    # after every test, which leaves the registry empty for tests in other files.
    # We force re-registration here so maintenance tests are always self-contained.
    from evolution.storage.provider import StorageRegistry
    from evolution.storage.providers.sqlite import SQLiteStorageProvider

    registry = StorageRegistry.default()
    if "sqlite" not in registry.list_providers():
        registry.register(SQLiteStorageProvider())

    yield test_db_path


# ── is_maintenance_due ────────────────────────────────────────────────────────


def test_is_maintenance_due_returns_true_when_never_ran():
    """First-ever run — no sentinel record → maintenance is due."""
    from evolution.maintenance import is_maintenance_due

    assert is_maintenance_due() is True


def test_is_maintenance_due_returns_false_below_interval():
    """After maintenance ran, not due again until the interval passes."""
    from evolution.maintenance import (
        MAINTENANCE_INTERACTION_INTERVAL,
        _SENTINEL_ID,
        is_maintenance_due,
    )
    from evolution.storage import get_storage

    store = get_storage()
    # Simulate: maintenance just ran at interaction count = 0
    store.log_interaction(
        prompt="[maintenance sentinel]",
        response=None,
        group_folder="__maintenance__",
        timestamp="2024-01-01T00:00:00+00:00",
        interaction_id=_SENTINEL_ID,
        latency_ms=0.0,
        eval_suite="maintenance",
    )

    # Delta = 0 — not yet due
    assert is_maintenance_due(interaction_count=0) is False


def test_is_maintenance_due_returns_true_at_interval():
    """Maintenance is due once interaction count delta reaches the threshold."""
    from evolution.maintenance import (
        MAINTENANCE_INTERACTION_INTERVAL,
        _SENTINEL_ID,
        is_maintenance_due,
    )
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="[maintenance sentinel]",
        response=None,
        group_folder="__maintenance__",
        timestamp="2024-01-01T00:00:00+00:00",
        interaction_id=_SENTINEL_ID,
        latency_ms=0.0,
        eval_suite="maintenance",
    )

    assert is_maintenance_due(interaction_count=MAINTENANCE_INTERACTION_INTERVAL) is True


def test_is_maintenance_due_returns_false_just_below_interval():
    """One interaction below threshold — not yet due."""
    from evolution.maintenance import (
        MAINTENANCE_INTERACTION_INTERVAL,
        _SENTINEL_ID,
        is_maintenance_due,
    )
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="[maintenance sentinel]",
        response=None,
        group_folder="__maintenance__",
        timestamp="2024-01-01T00:00:00+00:00",
        interaction_id=_SENTINEL_ID,
        latency_ms=0.0,
        eval_suite="maintenance",
    )

    assert (
        is_maintenance_due(interaction_count=MAINTENANCE_INTERACTION_INTERVAL - 1)
        is False
    )


# ── run_maintenance ───────────────────────────────────────────────────────────


def test_run_maintenance_skipped_when_not_due():
    """run_maintenance returns skipped=True when is_maintenance_due() is False."""
    from evolution.maintenance import _SENTINEL_ID, run_maintenance
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="[maintenance sentinel]",
        response=None,
        group_folder="__maintenance__",
        timestamp="2024-01-01T00:00:00+00:00",
        interaction_id=_SENTINEL_ID,
        latency_ms=0.0,
        eval_suite="maintenance",
    )

    result = run_maintenance()
    assert result["skipped"] is True
    assert result["archived_reflections"] == 0
    assert result["ran_at"] is None


def test_run_maintenance_force_bypasses_due_check():
    """force=True runs even when maintenance is not due."""
    from evolution.maintenance import _SENTINEL_ID, run_maintenance
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="[maintenance sentinel]",
        response=None,
        group_folder="__maintenance__",
        timestamp="2024-01-01T00:00:00+00:00",
        interaction_id=_SENTINEL_ID,
        latency_ms=0.0,
        eval_suite="maintenance",
    )

    # archive_stale_reflections is imported inside run_maintenance; patch at source
    with patch("evolution.reflexion.store.archive_stale_reflections", return_value=0):
        result = run_maintenance(force=True)

    assert result["skipped"] is False
    assert result["ran_at"] is not None


def test_run_maintenance_archives_stale_reflections():
    """run_maintenance calls archive_stale_reflections and reports the count."""
    from evolution.maintenance import run_maintenance

    with patch("evolution.reflexion.store.archive_stale_reflections", return_value=5) as mock_archive:
        result = run_maintenance(force=True, days=30)

    mock_archive.assert_called_once_with(days=30)
    assert result["archived_reflections"] == 5
    assert result["skipped"] is False


def test_run_maintenance_records_sentinel_after_run():
    """After running, a sentinel interaction is stored."""
    from evolution.maintenance import _SENTINEL_ID, run_maintenance
    from evolution.storage import get_storage

    with patch("evolution.reflexion.store.archive_stale_reflections", return_value=0):
        run_maintenance(force=True)

    store = get_storage()
    sentinel = store.get_interaction(_SENTINEL_ID)
    assert sentinel is not None


def test_run_maintenance_result_has_ran_at_timestamp():
    """ran_at in the result should be a parseable ISO-8601 timestamp."""
    from datetime import datetime
    from evolution.maintenance import run_maintenance

    with patch("evolution.reflexion.store.archive_stale_reflections", return_value=0):
        result = run_maintenance(force=True)

    assert result["ran_at"] is not None
    datetime.fromisoformat(result["ran_at"])  # Raises if not valid ISO-8601


# ── CLI entry-point ───────────────────────────────────────────────────────────


def test_maintenance_cli_json_output(capsys):
    """python3 -m evolution.maintenance --force --json prints valid JSON."""
    import runpy

    with (
        patch("evolution.reflexion.store.archive_stale_reflections", return_value=3),
        patch("sys.argv", ["evolution.maintenance", "--force", "--json"]),
    ):
        runpy.run_module("evolution.maintenance", run_name="__main__", alter_sys=True)

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["archived_reflections"] == 3
    assert data["skipped"] is False


def test_maintenance_cli_human_output(capsys):
    """python3 -m evolution.maintenance --force (no --json) prints human text."""
    import runpy

    with (
        patch("evolution.reflexion.store.archive_stale_reflections", return_value=2),
        patch("sys.argv", ["evolution.maintenance", "--force"]),
    ):
        runpy.run_module("evolution.maintenance", run_name="__main__", alter_sys=True)

    captured = capsys.readouterr()
    assert "archived" in captured.out.lower()


def test_maintenance_cli_skipped_message(capsys):
    """When not due, CLI prints 'skipped' message."""
    import runpy
    from evolution.maintenance import _SENTINEL_ID
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="[maintenance sentinel]",
        response=None,
        group_folder="__maintenance__",
        timestamp="2024-01-01T00:00:00+00:00",
        interaction_id=_SENTINEL_ID,
        latency_ms=0.0,
        eval_suite="maintenance",
    )

    with patch("sys.argv", ["evolution.maintenance"]):
        runpy.run_module("evolution.maintenance", run_name="__main__", alter_sys=True)

    captured = capsys.readouterr()
    assert "skipped" in captured.out.lower()
