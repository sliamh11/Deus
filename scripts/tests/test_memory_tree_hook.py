"""Tests for scripts/memory_tree_hook.py and the drift scan in stop_hook.py.

All tests redirect memory_tree's DB_PATH and _LOG_PATH to tmp — no prod writes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name: str, path: Path):
    """Reuse conftest's pre-loaded instance if present — otherwise load once."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mt = _load("memory_tree", _ROOT / "scripts" / "memory_tree.py")
hook = _load("memory_tree_hook", _ROOT / "scripts" / "memory_tree_hook.py")
stop_hook = _load("stop_hook", _ROOT / "scripts" / "stop_hook.py")


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def redirect_paths(tmp_path, monkeypatch):
    """Never touch prod DB or JSONL from tests."""
    db_path = tmp_path / "tree.db"
    log_path = tmp_path / "queries.jsonl"
    monkeypatch.setattr(mt, "DB_PATH", db_path)
    monkeypatch.setattr(mt, "_LOG_PATH", log_path)
    yield


@pytest.fixture
def fake_vault(tmp_path, monkeypatch):
    v = tmp_path / "vault"
    (v / "Persona" / "life").mkdir(parents=True)
    (v / "MEMORY_TREE.md").write_text(
        "---\nid: root0000000000000000000000000001\n"
        "title: Root\ndescription: Root map.\nlevel: 0\n"
        "children:\n  - Persona/life/household.md\n---\n",
        encoding="utf-8",
    )
    (v / "Persona" / "life" / "household.md").write_text(
        "---\nid: hh000000000000000000000000000002\n"
        "title: Household\ndescription: Who Liam lives with.\nlevel: 2\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEUS_VAULT_PATH", str(v))
    return v


@pytest.fixture
def stub_embed(monkeypatch):
    """Deterministic lightweight embed — every call returns a fixed-length vector."""
    def _embed(text: str):
        v = [0.0] * mt.EMBED_DIM
        for i, c in enumerate(text[:mt.EMBED_DIM]):
            v[i] = (ord(c) % 17) / 17.0
        return v
    monkeypatch.setattr(mt, "embed_text", _embed)
    return _embed


@pytest.fixture
def built_db(fake_vault, stub_embed):
    db = mt.open_db()
    mt.build_tree(fake_vault, db, rebuild=False)
    return db


# ── memory_tree_hook.dispatch ─────────────────────────────────────────────────

class TestHookDispatch:
    def test_gate_off_noop(self, fake_vault, monkeypatch):
        monkeypatch.delenv("DEUS_MEMORY_TREE", raising=False)
        result = hook.dispatch({"tool_input": {"file_path": str(fake_vault / "MEMORY_TREE.md")}})
        assert result == "gate_off"

    def test_bad_input(self, fake_vault, monkeypatch):
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        assert hook.dispatch({}) == "bad_input"
        assert hook.dispatch({"tool_input": {}}) == "bad_input"
        assert hook.dispatch({"tool_input": {"file_path": ""}}) == "bad_input"

    def test_no_vault(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
        # Point config lookup at a non-existent file so fallback returns None
        monkeypatch.setattr(
            hook, "_vault_root", lambda: None
        )
        result = hook.dispatch({"tool_input": {"file_path": str(tmp_path / "x.md")}})
        assert result == "no_vault"

    def test_file_outside_vault(self, fake_vault, tmp_path, monkeypatch):
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        outside = tmp_path / "outside.md"
        outside.write_text("# outside")
        result = hook.dispatch({"tool_input": {"file_path": str(outside)}})
        assert result == "not_vault_file"

    def test_non_markdown(self, fake_vault, monkeypatch):
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        txt = fake_vault / "notes.txt"
        txt.write_text("hello")
        result = hook.dispatch({"tool_input": {"file_path": str(txt)}})
        assert result == "not_markdown"

    def test_dispatches_and_reembeds(self, built_db, fake_vault, stub_embed, monkeypatch):
        """Markdown file under vault → reembed_file is called and returns a status."""
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        # Edit household.md's description so hash changes
        p = fake_vault / "Persona" / "life" / "household.md"
        p.write_text(
            "---\nid: hh000000000000000000000000000002\n"
            "title: Household\ndescription: Liam lives with Shani and Omer (Eden from Aug 2026).\n"
            "level: 2\n---\n",
            encoding="utf-8",
        )
        result = hook.dispatch({"tool_input": {"file_path": str(p)}})
        assert result == "reembedded"

    def test_unchanged_is_reported(self, built_db, fake_vault, stub_embed, monkeypatch):
        """No description change → 'unchanged' status."""
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        p = fake_vault / "Persona" / "life" / "household.md"
        result = hook.dispatch({"tool_input": {"file_path": str(p)}})
        assert result == "unchanged"


# ── stop_hook._scan_vault_drift ───────────────────────────────────────────────

class TestDriftScan:
    def test_gate_off_returns_zero(self, fake_vault, monkeypatch):
        monkeypatch.delenv("DEUS_MEMORY_TREE", raising=False)
        assert stop_hook._scan_vault_drift(fake_vault, limit=5) == 0

    def test_picks_up_stale_files(self, built_db, fake_vault, stub_embed, monkeypatch):
        """Files with mtime > node.updated_at are candidates."""
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        p = fake_vault / "Persona" / "life" / "household.md"
        # Change description AND bump mtime to the future
        p.write_text(
            "---\nid: hh000000000000000000000000000002\n"
            "title: Household\ndescription: Liam lives with Shani; Eden moves in Aug 2026.\n"
            "level: 2\n---\n",
            encoding="utf-8",
        )
        future = time.time() + 3600
        import os
        os.utime(p, (future, future))
        attempted = stop_hook._scan_vault_drift(fake_vault, limit=5)
        assert attempted >= 1

    def test_respects_limit(self, built_db, fake_vault, stub_embed, monkeypatch):
        """With 2 tracked files both stale, limit=1 only processes one."""
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        future = time.time() + 3600
        import os
        for p in [
            fake_vault / "MEMORY_TREE.md",
            fake_vault / "Persona" / "life" / "household.md",
        ]:
            # mtime drift alone is enough; content_hash gate will skip re-embed but
            # _scan_vault_drift counts every attempt.
            os.utime(p, (future, future))
        attempted = stop_hook._scan_vault_drift(fake_vault, limit=1)
        assert attempted == 1

    def test_missing_db_recovers_via_discovery(self, fake_vault, stub_embed, monkeypatch, tmp_path):
        """If DB doesn't exist, scan rebuilds it via discovery on the next pass."""
        monkeypatch.setenv("DEUS_MEMORY_TREE", "1")
        monkeypatch.setattr(mt, "DB_PATH", tmp_path / "nonexistent.db")
        attempted = stop_hook._scan_vault_drift(fake_vault, limit=5)
        # Both fixture files (MEMORY_TREE.md + household.md) should be discovered.
        assert attempted == 2
