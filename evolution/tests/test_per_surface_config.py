"""Tests for Phase 3 per-surface llama.cpp model env vars.

Verifies that each per-surface env var (LLAMA_CPP_GEN_MODEL, LLAMA_CPP_JUDGE_MODEL,
LLAMA_CPP_EMBED_MODEL) overrides when set, and falls back to LLAMA_CPP_MODEL
when unset — preserving back-compat with PR #452/#453 single-var deployments.
"""
import importlib
import os
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture(autouse=True)
def reload_config():
    """Reload evolution.config (and the providers that import from it) so
    each test picks up the patched env vars. Uses importlib.reload — does
    NOT delete modules — so other tests' import references remain valid.
    """
    import evolution.config
    importlib.reload(evolution.config)
    yield
    # Restore baseline config after test by reloading once more with the
    # default (post-monkeypatch-rollback) environment.
    importlib.reload(evolution.config)


def test_judge_falls_back_to_catchall(monkeypatch):
    """When LLAMA_CPP_JUDGE_MODEL unset, falls back to LLAMA_CPP_MODEL."""
    monkeypatch.setenv("LLAMA_CPP_MODEL", "fallback-model")
    monkeypatch.delenv("LLAMA_CPP_JUDGE_MODEL", raising=False)
    import evolution.config
    cfg = importlib.reload(evolution.config)
    assert cfg.LLAMA_CPP_JUDGE_MODEL == "fallback-model"


def test_judge_specific_overrides_catchall(monkeypatch):
    """When LLAMA_CPP_JUDGE_MODEL set, it wins over LLAMA_CPP_MODEL."""
    monkeypatch.setenv("LLAMA_CPP_MODEL", "fallback-model")
    monkeypatch.setenv("LLAMA_CPP_JUDGE_MODEL", "judge-specific")
    import evolution.config
    cfg = importlib.reload(evolution.config)
    assert cfg.LLAMA_CPP_JUDGE_MODEL == "judge-specific"
    # Sibling surfaces still fall back
    assert cfg.LLAMA_CPP_GEN_MODEL == "fallback-model"


def test_gen_falls_back_to_catchall(monkeypatch):
    """Same fallback pattern for LLAMA_CPP_GEN_MODEL."""
    monkeypatch.setenv("LLAMA_CPP_MODEL", "fallback-model")
    monkeypatch.delenv("LLAMA_CPP_GEN_MODEL", raising=False)
    import evolution.config
    cfg = importlib.reload(evolution.config)
    assert cfg.LLAMA_CPP_GEN_MODEL == "fallback-model"


def test_gen_specific_overrides_catchall(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_MODEL", "fallback-model")
    monkeypatch.setenv("LLAMA_CPP_GEN_MODEL", "gen-specific")
    import evolution.config
    cfg = importlib.reload(evolution.config)
    assert cfg.LLAMA_CPP_GEN_MODEL == "gen-specific"


def test_embed_falls_back_to_catchall(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_MODEL", "fallback-model")
    monkeypatch.delenv("LLAMA_CPP_EMBED_MODEL", raising=False)
    import evolution.config
    cfg = importlib.reload(evolution.config)
    assert cfg.LLAMA_CPP_EMBED_MODEL == "fallback-model"


def test_embed_specific_overrides_catchall(monkeypatch):
    monkeypatch.setenv("LLAMA_CPP_MODEL", "fallback-model")
    monkeypatch.setenv("LLAMA_CPP_EMBED_MODEL", "embed-specific")
    import evolution.config
    cfg = importlib.reload(evolution.config)
    assert cfg.LLAMA_CPP_EMBED_MODEL == "embed-specific"


def test_all_empty_when_nothing_set(monkeypatch):
    """When neither catch-all nor surface-specific set, all default to empty."""
    for var in ("LLAMA_CPP_MODEL", "LLAMA_CPP_GEN_MODEL",
                "LLAMA_CPP_JUDGE_MODEL", "LLAMA_CPP_EMBED_MODEL"):
        monkeypatch.delenv(var, raising=False)
    import evolution.config
    cfg = importlib.reload(evolution.config)
    assert cfg.LLAMA_CPP_MODEL == ""
    assert cfg.LLAMA_CPP_GEN_MODEL == ""
    assert cfg.LLAMA_CPP_JUDGE_MODEL == ""
    assert cfg.LLAMA_CPP_EMBED_MODEL == ""


def test_judge_provider_uses_judge_specific(monkeypatch):
    """LlamaCppProvider.default_model returns LLAMA_CPP_JUDGE_MODEL."""
    monkeypatch.setenv("LLAMA_CPP_MODEL", "catchall")
    monkeypatch.setenv("LLAMA_CPP_JUDGE_MODEL", "for-judge")
    import evolution.config
    importlib.reload(evolution.config)
    import evolution.judge.providers.llama_cpp as jp
    importlib.reload(jp)
    p = jp.LlamaCppProvider()
    assert p.default_model == "for-judge"


def test_gen_provider_uses_gen_specific(monkeypatch):
    """LlamaCppGenerativeProvider.default_model returns LLAMA_CPP_GEN_MODEL."""
    monkeypatch.setenv("LLAMA_CPP_MODEL", "catchall")
    monkeypatch.setenv("LLAMA_CPP_GEN_MODEL", "for-gen")
    import evolution.config
    importlib.reload(evolution.config)
    import evolution.generative.providers.llama_cpp as gp
    importlib.reload(gp)
    p = gp.LlamaCppGenerativeProvider()
    assert p.default_model == "for-gen"
