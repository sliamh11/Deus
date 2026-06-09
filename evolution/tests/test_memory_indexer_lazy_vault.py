"""Regression tests: memory-vault resolution must be LAZY.

Guards the contract introduced when ``memory_indexer`` stopped resolving the
vault at module-import scope. Previously ``_vault_root = _load_vault_path()`` ran
at import and ``sys.exit``ed with AUTH_ERROR when no vault was configured, so the
module was unimportable in any vault-less environment (CI, fresh checkout) and
even DB-only commands (``--query``) died at startup. Resolution now happens on
first actual vault access via ``_vault_root()`` (mirrors the lazy Gemini client,
PR #677).

Three contracts:
  1. Import does NOT resolve the vault (cache is None; accessors are callables).
  2. The ``sys.exit`` on a missing vault is DEFERRED to first ``_vault_root()``
     call, not raised at import.
  3. ``_vault_root()`` is the sole resolver and caches (resolves once).

No vault is required to import this module — that is the whole point.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

_SCRIPTS = str(Path(__file__).resolve().parents[2] / "scripts")


def _import_memory_indexer():
    """Import scripts/memory_indexer.py without leaving scripts/ on sys.path
    (it would shadow names while OTHER evolution tests are collected)."""
    added = _SCRIPTS not in sys.path
    if added:
        sys.path.insert(0, _SCRIPTS)
    try:
        return importlib.import_module("memory_indexer")
    finally:
        if added:
            sys.path.remove(_SCRIPTS)


def test_import_does_not_resolve_vault():
    # The headline contract: importing resolves NOTHING. Force a GENUINELY fresh
    # import (drop the cached module) so module top-level code re-runs — the
    # cache must come back None regardless of test order. A non-None value would
    # mean the module eagerly resolved the vault (the old import-time sys.exit
    # behaviour) — exactly what this change removes.
    sys.modules.pop("memory_indexer", None)
    mi = _import_memory_indexer()
    assert mi._vault_root_cache is None
    for accessor in (
        mi._vault_root,
        mi._vault_session_logs,
        mi._vault_atoms,
        mi._vault_entities,
    ):
        assert callable(accessor)


def test_vault_root_defers_exit_when_unconfigured(tmp_path, monkeypatch):
    # Force a genuinely vault-less environment (all three resolution tiers miss):
    # no env override, cwd has no ./.deus/config.json, global CONFIG_PATH absent.
    mi = _import_memory_indexer()
    monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
    monkeypatch.chdir(tmp_path)  # no ./.deus/config.json here
    monkeypatch.setattr(mi, "CONFIG_PATH", tmp_path / "nonexistent" / "config.json")
    # setattr auto-reverts to the original None, so this never leaks to siblings.
    monkeypatch.setattr(mi, "_vault_root_cache", None)

    with pytest.raises(SystemExit) as exc:
        mi._vault_root()
    assert exc.value.code == mi.AUTH_ERROR


def test_vault_root_is_sole_resolver_and_caches(tmp_path, monkeypatch):
    # _vault_root() resolves exactly once and reuses the cached Path thereafter.
    mi = _import_memory_indexer()
    calls = {"n": 0}

    def _fake_load() -> Path:
        calls["n"] += 1
        return tmp_path / "vault"

    monkeypatch.setattr(mi, "_load_vault_path", _fake_load)
    monkeypatch.setattr(mi, "_vault_root_cache", None)

    first = mi._vault_root()
    second = mi._vault_root()
    assert first == tmp_path / "vault"
    assert first is second
    assert calls["n"] == 1, "the vault must be resolved once and cached"
    # Derived accessors compose on top of the cached root.
    assert mi._vault_atoms() == tmp_path / "vault" / "Atoms"
    assert mi._vault_session_logs() == tmp_path / "vault" / "Session-Logs"
    assert mi._vault_entities() == tmp_path / "vault" / "Entities"
