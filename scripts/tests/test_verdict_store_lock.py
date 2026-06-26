"""Complementary unit tests for the LIA-332 verdict-store lock.

These sit alongside the multiprocessing oracle in ``test_verdict_store_race.py``
and cover what the oracle (POSIX-only, fork-based) does not:

* a deterministic, CROSS-PLATFORM proof that the in-lock re-read MERGES a
  concurrent writer's key (no multiprocessing timing dependence),
* the ``fcntl is None`` no-op-lock branch (the Windows path, exercised on POSIX),
* ``_clear_verdict`` preserving a sibling key while removing the target.

Run:
    python3 -m pytest scripts/tests/test_verdict_store_lock.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import codex_warden_hooks as h
from warden_hooks import verdict_store as _vs


@pytest.fixture
def store_root(tmp_path, monkeypatch):
    """Isolated verdict store under ``tmp_path/.claude`` (mirrors the oracle fixture)."""
    cdir = tmp_path / ".claude"
    cdir.mkdir()
    monkeypatch.setattr(h, "_claude_marker_dir", lambda root: cdir)
    monkeypatch.setattr(h, "_worktree_for_cwd", lambda cwd, root: tmp_path)
    return tmp_path


def _read_store(store_root: Path) -> dict:
    path = store_root / ".claude" / ".warden-verdicts.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_inlock_reread_merges_concurrent_key(store_root, monkeypatch):
    """The in-lock FRESH read merges a key a concurrent writer already committed.

    Simulates the race deterministically: ``_read_verdicts_at`` (the read performed
    INSIDE the lock) returns a store that already holds a sibling key — as if another
    writer landed it just before our read. Our write must KEEP that sibling, not clobber
    it. This is the cross-platform analogue of the multiprocessing oracle.
    """
    sibling = "code-reviewer@gpt"
    ours = "code-reviewer@claude"

    real_read = _vs._read_verdicts_at

    def _read_with_sibling(path: Path) -> dict:
        data = real_read(path)
        data.setdefault(
            sibling,
            {"verdict": "SHIP", "ts": "2026-01-01T00:00:00Z", "reason": "concurrent", "source": "test"},
        )
        return data

    monkeypatch.setattr(_vs, "_read_verdicts_at", _read_with_sibling)

    _vs._write_verdict(store_root, ours, "SHIP", "ours")

    data = _read_store(store_root)
    assert ours in data, "our key was not written"
    assert sibling in data, "concurrent sibling key was clobbered by our write"


def test_write_still_works_when_fcntl_absent(store_root, monkeypatch):
    """The ``fcntl is None`` no-op-lock branch (Windows path) still writes correctly."""
    monkeypatch.setattr(_vs, "fcntl", None)

    _vs._write_verdict(store_root, "code-reviewer@claude", "SHIP", "no-lock path")

    data = _read_store(store_root)
    assert data.get("code-reviewer@claude", {}).get("verdict") == "SHIP"


def test_clear_preserves_sibling_key(store_root):
    """Clearing one key leaves the others intact (no whole-store clobber on delete)."""
    _vs._write_verdict(store_root, "code-reviewer", "SHIP", "cr")
    _vs._write_verdict(store_root, "plan-reviewer", "SHIP", "pr")

    # "code-reviewed" maps to "code-reviewer" via MARKER_NAMES.
    h._clear_verdict("code-reviewed", store_root)

    data = _read_store(store_root)
    assert "code-reviewer" not in data, "target key was not cleared"
    assert "plan-reviewer" in data, "sibling key was lost when clearing the target"
