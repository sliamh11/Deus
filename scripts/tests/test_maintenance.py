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
    """Redirect evolution EVOLUTION_DB_PATH to a temp file and ensure providers are registered."""
    test_db_path = tmp_path / "test_maint.db"
    monkeypatch.setattr(db_mod, "EVOLUTION_DB_PATH", test_db_path)
    monkeypatch.setattr(config_mod, "EVOLUTION_DB_PATH", test_db_path)
    monkeypatch.setattr(config_mod, "DB_PATH", tmp_path / "nonexistent_legacy.db")

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


def _patch_maintenance_internals():
    """Return a context manager that patches all maintenance sub-functions."""
    from contextlib import contextmanager

    @contextmanager
    def _ctx(**overrides):
        defaults = {
            "evolution.reflexion.store.archive_stale_reflections": 0,
            "evolution.maintenance.judge_pending_interactions": 0,
            "evolution.maintenance.compact_old_interactions": 0,
        }
        defaults.update(overrides)
        patches = [patch(k, return_value=v) for k, v in defaults.items()]
        mocks = [p.__enter__() for p in patches]
        try:
            yield mocks
        finally:
            for p in patches:
                p.__exit__(None, None, None)

    return _ctx


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

    with _patch_maintenance_internals()():
        result = run_maintenance(force=True)

    assert result["skipped"] is False
    assert result["ran_at"] is not None


def test_run_maintenance_archives_stale_reflections():
    """run_maintenance calls archive_stale_reflections and reports the count."""
    from evolution.maintenance import run_maintenance

    with _patch_maintenance_internals()(
        **{"evolution.reflexion.store.archive_stale_reflections": 5}
    ):
        result = run_maintenance(force=True, days=30)

    assert result["archived_reflections"] == 5
    assert result["skipped"] is False


def test_run_maintenance_records_sentinel_after_run():
    """After running, a sentinel interaction is stored."""
    from evolution.maintenance import _SENTINEL_ID, run_maintenance
    from evolution.storage import get_storage

    with _patch_maintenance_internals()():
        run_maintenance(force=True)

    store = get_storage()
    sentinel = store.get_interaction(_SENTINEL_ID)
    assert sentinel is not None


def test_run_maintenance_result_has_ran_at_timestamp():
    """ran_at in the result should be a parseable ISO-8601 timestamp."""
    from datetime import datetime
    from evolution.maintenance import run_maintenance

    with _patch_maintenance_internals()():
        result = run_maintenance(force=True)

    assert result["ran_at"] is not None
    datetime.fromisoformat(result["ran_at"])  # Raises if not valid ISO-8601


# ── CLI entry-point ───────────────────────────────────────────────────────────


def test_maintenance_cli_json_output(capsys):
    """python3 -m evolution.maintenance --force --json prints valid JSON."""
    from evolution.maintenance import run_maintenance

    # Test the CLI by calling _main directly rather than via runpy,
    # which avoids module re-import issues with deep dependency chains.
    with (
        _patch_maintenance_internals()(
            **{"evolution.reflexion.store.archive_stale_reflections": 3}
        ),
        patch("sys.argv", ["evolution.maintenance", "--force", "--json"]),
    ):
        from evolution.maintenance import _main
        _main()

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["archived_reflections"] == 3
    assert data["skipped"] is False


def test_maintenance_cli_human_output(capsys):
    """python3 -m evolution.maintenance --force (no --json) prints human text."""
    with (
        _patch_maintenance_internals()(
            **{"evolution.reflexion.store.archive_stale_reflections": 2}
        ),
        patch("sys.argv", ["evolution.maintenance", "--force"]),
    ):
        from evolution.maintenance import _main
        _main()

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


# ── Compaction tests ─────────────────────────────────────────────────────────


def test_get_compactable_interactions_empty():
    """No interactions → empty list."""
    from evolution.storage import get_storage

    store = get_storage()
    assert store.get_compactable_interactions(days=7) == []


def test_get_compactable_interactions_skips_recent():
    """Interactions newer than threshold are not returned."""
    from datetime import datetime, timezone
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="x" * 400,
        response="long response",
        group_folder="test",
        timestamp=datetime.now(timezone.utc).isoformat(),
        interaction_id="recent1",
    )
    store.update_interaction("recent1", judge_score=0.8)

    assert store.get_compactable_interactions(days=7) == []


def test_get_compactable_interactions_returns_old_scored():
    """Old scored interactions with long prompts are returned."""
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="x" * 400,
        response="long response",
        group_folder="test",
        timestamp="2020-01-01T00:00:00+00:00",
        interaction_id="old1",
    )
    store.update_interaction("old1", judge_score=0.7)

    results = store.get_compactable_interactions(days=7)
    assert len(results) == 1
    assert results[0]["id"] == "old1"


def test_get_compactable_interactions_skips_short_prompts():
    """Interactions with prompts <= 300 chars are not compacted."""
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="short",
        response="r",
        group_folder="test",
        timestamp="2020-01-01T00:00:00+00:00",
        interaction_id="short1",
    )
    store.update_interaction("short1", judge_score=0.7)

    assert store.get_compactable_interactions(days=7) == []


def test_get_compactable_interactions_skips_unjudged():
    """Unjudged interactions are not compacted."""
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="x" * 400,
        response="r",
        group_folder="test",
        timestamp="2020-01-01T00:00:00+00:00",
        interaction_id="unjudged1",
    )

    assert store.get_compactable_interactions(days=7) == []


def test_compact_interaction_replaces_text():
    """compact_interaction replaces prompt with summary and NULLs response."""
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="x" * 400,
        response="long response",
        group_folder="test",
        timestamp="2020-01-01T00:00:00+00:00",
        interaction_id="compact1",
    )
    store.update_interaction("compact1", judge_score=0.8)

    store.compact_interaction("compact1", "Summary of interaction")

    row = store.get_interaction("compact1")
    assert row["prompt"] == "Summary of interaction"
    assert row["response"] is None
    # Score is preserved
    assert abs(row["judge_score"] - 0.8) < 1e-5


def test_compact_old_interactions_with_no_provider():
    """compact_old_interactions falls back to truncation when no generative provider."""
    from evolution.maintenance import compact_old_interactions
    from evolution.storage import get_storage

    store = get_storage()
    original_prompt = "x" * 400
    store.log_interaction(
        prompt=original_prompt,
        response="long response",
        group_folder="test",
        timestamp="2020-01-01T00:00:00+00:00",
        interaction_id="fallback1",
    )
    store.update_interaction("fallback1", judge_score=0.7)

    # Patch GenerativeRegistry.resolve to raise (simulating no provider)
    with patch(
        "evolution.generative.provider.GenerativeRegistry.default",
        side_effect=Exception("no provider"),
    ):
        count = compact_old_interactions()

    assert count == 1
    row = store.get_interaction("fallback1")
    assert "[compacted]" in row["prompt"]
    assert row["response"] is None
    # Score metadata should be included in fallback summary
    assert "0.70" in row["prompt"]


# ── Batch judging tests ──────────────────────────────────────────────────────


def test_get_unjudged_interactions_empty():
    """No interactions → empty list."""
    from evolution.storage import get_storage

    store = get_storage()
    assert store.get_unjudged_interactions() == []


def test_get_unjudged_interactions_returns_unjudged():
    """Unjudged interactions are returned."""
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="test prompt",
        response="test response",
        group_folder="test",
        timestamp="2024-01-01T00:00:00+00:00",
        interaction_id="uj1",
    )

    results = store.get_unjudged_interactions()
    assert len(results) == 1
    assert results[0]["id"] == "uj1"


def test_get_unjudged_interactions_skips_judged():
    """Judged interactions are excluded."""
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="test",
        response="r",
        group_folder="test",
        timestamp="2024-01-01T00:00:00+00:00",
        interaction_id="judged1",
    )
    store.update_interaction("judged1", judge_score=0.9)

    assert store.get_unjudged_interactions() == []


def test_get_unjudged_interactions_skips_maintenance():
    """Maintenance sentinel interactions are excluded."""
    from evolution.storage import get_storage

    store = get_storage()
    store.log_interaction(
        prompt="[maintenance sentinel]",
        response=None,
        group_folder="__maintenance__",
        timestamp="2024-01-01T00:00:00+00:00",
        interaction_id="maint1",
        eval_suite="maintenance",
    )

    assert store.get_unjudged_interactions() == []


def test_run_maintenance_includes_new_fields():
    """run_maintenance result dict includes judged and compacted counts."""
    from evolution.maintenance import run_maintenance

    with (
        patch("evolution.reflexion.store.archive_stale_reflections", return_value=0),
        patch("evolution.maintenance.judge_pending_interactions", return_value=3),
        patch("evolution.maintenance.compact_old_interactions", return_value=2),
    ):
        result = run_maintenance(force=True)

    assert result["judged_interactions"] == 3
    assert result["compacted_interactions"] == 2
    assert result["archived_reflections"] == 0
    assert result["skipped"] is False


def test_run_maintenance_skipped_includes_new_fields():
    """Skipped result includes all new fields as zero."""
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
    assert result["judged_interactions"] == 0
    assert result["compacted_interactions"] == 0
