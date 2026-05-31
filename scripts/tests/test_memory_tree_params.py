"""Tests for the DEUS_TREE_PARAMS learned-artifact gate in memory_tree (LIA-136).

The wire: optimized retrieval params load *by default*, with an off-switch
(DEUS_TREE_PARAMS=0) and a *visible* fallback on load failure (a silent
swallow would hide a broken optimizer wire — the facade class this gate
exists to prevent).

Each test loads a fresh, uniquely-named copy of memory_tree so the
module-level param-load block re-executes under the test's env + a stubbed
`evolution.optimizer.artifacts.get_active`. memory_tree does no DB work at
import time, so fresh loads are side-effect free.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_MT_PATH = _ROOT / "scripts" / "memory_tree.py"

# Distinct synthetic values, all different from the hardcoded defaults, so a
# successful override is unambiguous.
_ARTIFACT = {
    "low_threshold": 0.111,
    "abstain_threshold": 0.222,
    "gap_threshold": 0.333,
    "top_k": 17,
    "rrf_k": 19,
    "min_entity_overlap": 5,
}

_EVO_KEYS = ("evolution", "evolution.optimizer", "evolution.optimizer.artifacts")


def _install_fake_artifacts(get_active_fn):
    """Stub evolution.optimizer.artifacts so memory_tree's
    `from evolution.optimizer.artifacts import get_active` resolves to our fn."""
    evo = types.ModuleType("evolution")
    opt = types.ModuleType("evolution.optimizer")
    art = types.ModuleType("evolution.optimizer.artifacts")
    art.get_active = get_active_fn
    evo.optimizer = opt
    opt.artifacts = art
    sys.modules["evolution"] = evo
    sys.modules["evolution.optimizer"] = opt
    sys.modules["evolution.optimizer.artifacts"] = art


def _load_fresh(name):
    """Load a fresh copy of memory_tree under a unique name (not 'memory_tree',
    which conftest owns) so the param-load block re-runs."""
    spec = importlib.util.spec_from_file_location(name, _MT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


@pytest.fixture
def restore_artifacts():
    """Snapshot/restore the evolution.* sys.modules entries around a test."""
    saved = {k: sys.modules.get(k) for k in _EVO_KEYS}
    yield
    for k, v in saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def test_artifact_overrides_defaults_when_gate_default_on(monkeypatch, restore_artifacts):
    """Env unset => default-on: a present artifact overrides all six DEFAULT_* constants."""
    monkeypatch.delenv("DEUS_TREE_PARAMS", raising=False)
    _install_fake_artifacts(lambda module: {"content": json.dumps(_ARTIFACT)})

    mod = _load_fresh("memory_tree_lia136_on")

    assert mod.DEFAULT_LOW_THRESHOLD == pytest.approx(0.111)
    assert mod.DEFAULT_ABSTAIN_THRESHOLD == pytest.approx(0.222)
    assert mod.DEFAULT_SCORE_GAP_THRESHOLD == pytest.approx(0.333)
    assert mod.DEFAULT_TOP_K == 17
    assert mod.DEFAULT_RRF_K == 19
    assert mod.DEFAULT_MIN_ENTITY_OVERLAP == 5


def test_load_failure_is_visible_and_falls_back(monkeypatch, capsys, restore_artifacts):
    """A raising get_active emits a stderr signal AND defaults survive (no silent swallow)."""
    monkeypatch.delenv("DEUS_TREE_PARAMS", raising=False)

    def _boom(module):
        raise RuntimeError("artifact store unavailable")

    _install_fake_artifacts(_boom)

    mod = _load_fresh("memory_tree_lia136_boom")

    # Defaults must survive the failure (module didn't crash, constants intact).
    assert isinstance(mod.DEFAULT_TOP_K, int)
    assert mod.DEFAULT_LOW_THRESHOLD != pytest.approx(0.111)  # artifact NOT applied
    # The fallback must be observable, not silent.
    assert "optimized-param load failed" in capsys.readouterr().err


def test_off_switch_suppresses_load(monkeypatch, restore_artifacts):
    """DEUS_TREE_PARAMS=0 must NOT consult the artifact store even when one exists."""
    monkeypatch.setenv("DEUS_TREE_PARAMS", "0")
    called = {"hit": False}

    def _track(module):
        called["hit"] = True
        return {"content": json.dumps(_ARTIFACT)}

    _install_fake_artifacts(_track)

    mod = _load_fresh("memory_tree_lia136_off")

    assert called["hit"] is False
    # The synthetic override must not have been applied.
    assert mod.DEFAULT_LOW_THRESHOLD != pytest.approx(0.111)
