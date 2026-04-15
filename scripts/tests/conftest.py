"""Pytest conftest for scripts/tests/.

Autouse safety: redirect the two production paths that `memory_tree` writes
to (DB_PATH and _LOG_PATH) into a per-test tmp directory. Prevents pytest
runs from polluting ~/.deus/memory_tree.db and ~/.deus/memory_tree_queries.jsonl.

This fixture is autouse — every test in this directory is protected, including
any that forget to redirect paths explicitly. It is idempotent with tests
that do their own redirection (they simply override these defaults).

Regression guard for the 2026-04-15 test-pollution incident: prior to this
conftest, retrieve/query tests in test_memory_tree.py appended ~12 rows to
prod JSONL per pytest run because only one test monkey-patched _LOG_PATH.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_MT_PATH = _ROOT / "scripts" / "memory_tree.py"


def _load_once(name: str, path: Path):
    """Load a script as a module exactly once, caching in sys.modules.

    Test files that use `sys.modules.get(name)` before calling
    `importlib.util.spec_from_file_location` will share this instance —
    meaning monkeypatch on it applies everywhere.
    """
    if name in sys.modules:
        return sys.modules[name]
    if not path.exists():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load at conftest-import time (before test collection) so every test file
# that checks sys.modules first reuses this single instance.
_mt = _load_once("memory_tree", _MT_PATH)
_load_once("memory_tree_hook", _ROOT / "scripts" / "memory_tree_hook.py")
_load_once("stop_hook", _ROOT / "scripts" / "stop_hook.py")


@pytest.fixture(autouse=True)
def isolate_memory_tree_paths(tmp_path, monkeypatch):
    """Redirect memory_tree DB + query log to tmp so tests never touch prod.

    Patches every module in sys.modules that exposes DB_PATH / _LOG_PATH —
    catches modules test files may have reloaded under aliases.
    """
    tmp_db = tmp_path / "tree.db"
    tmp_log = tmp_path / "queries.jsonl"
    tmp_audit = tmp_path / "audit.jsonl"
    for mod in list(sys.modules.values()):
        if mod is None:
            continue
        if getattr(mod, "__name__", "") not in {"memory_tree"}:
            continue
        monkeypatch.setattr(mod, "DB_PATH", tmp_db, raising=False)
        monkeypatch.setattr(mod, "_LOG_PATH", tmp_log, raising=False)
        monkeypatch.setattr(mod, "_AUDIT_PATH", tmp_audit, raising=False)
    yield
