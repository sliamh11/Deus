"""Tests for the shared auto-memory dir resolver (LIA-341).

Frozen expected values: each scenario asserts a concrete resolved path so an
incorrectly-encoded directory key can't pass silently.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import auto_memory_dir as amd  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    # Detach from the real environment + home so derived/fallback paths are
    # deterministic under tmp_path.
    monkeypatch.delenv("DEUS_AUTO_MEMORY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def test_env_override_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", "~/explicit")
    assert amd.resolve_auto_memory_dir() == tmp_path / "explicit"


def test_env_override_absolute_wins(monkeypatch, tmp_path):
    target = tmp_path / "explicit-abs"
    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", str(target))
    assert amd.resolve_auto_memory_dir() == target


def test_project_dir_derived_frozen_key(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/foo/bar")
    target = tmp_path / ".claude" / "projects" / "-foo-bar" / "memory"
    target.mkdir(parents=True)
    assert amd.resolve_auto_memory_dir() == target


def test_project_dir_derived_skipped_when_dir_absent(monkeypatch, tmp_path):
    # CLAUDE_PROJECT_DIR set but its memory dir does not exist -> fall through
    # to the ~/.deus/auto-memory fallback (the repo-derived dir won't exist
    # under the tmp HOME either).
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/foo/bar")
    assert amd.resolve_auto_memory_dir() == tmp_path / ".deus" / "auto-memory"


def test_windows_backslash_encoding(monkeypatch, tmp_path):
    win_proj = r"C:\Users\x"
    encoded = amd._encode_project_dir(win_proj)
    assert "\\" not in encoded  # no raw backslash survives the encoding
    assert encoded == "-C:-Users-x"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", win_proj)
    target = tmp_path / ".claude" / "projects" / encoded / "memory"
    target.mkdir(parents=True)
    assert amd.resolve_auto_memory_dir() == target


def test_fallback_when_nothing_resolves(tmp_path):
    assert amd.resolve_auto_memory_dir() == tmp_path / ".deus" / "auto-memory"


def test_standards_pack_delegates_to_resolver(monkeypatch, tmp_path):
    import standards_pack

    target = tmp_path / "deleg"
    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", str(target))
    assert standards_pack._default_auto_mem_dir() == amd.resolve_auto_memory_dir()
    assert standards_pack._default_auto_mem_dir() == target
