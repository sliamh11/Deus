"""Tests for the judge-specific Ollama model override (EVOLUTION_OLLAMA_JUDGE_MODEL).

Verifies config.OLLAMA_JUDGE_MODEL falls back to OLLAMA_MODEL when unset (a true
no-op default → production scoring byte-identical) and overrides when set, and that
OllamaProvider.default_model — the sole default-model source for all production
judges (hot + batch) — reflects it. Deterministic; no Ollama calls.

Mirrors test_per_surface_config.py (the LLAMA_CPP_JUDGE_MODEL precedent).
"""
import importlib
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


@pytest.fixture(autouse=True)
def reload_config():
    """Reload evolution.config so each test picks up patched env vars, and restore
    the baseline afterward. importlib.reload (not module deletion) keeps other tests'
    import references valid."""
    import evolution.config
    importlib.reload(evolution.config)
    yield
    importlib.reload(evolution.config)


def test_judge_model_falls_back_to_ollama_model(monkeypatch):
    """Unset → OLLAMA_JUDGE_MODEL == OLLAMA_MODEL (true no-op default)."""
    monkeypatch.setenv("OLLAMA_MODEL", "gemma4:e4b")
    monkeypatch.delenv("EVOLUTION_OLLAMA_JUDGE_MODEL", raising=False)
    import evolution.config
    cfg = importlib.reload(evolution.config)
    assert cfg.OLLAMA_JUDGE_MODEL == cfg.OLLAMA_MODEL == "gemma4:e4b"


def test_judge_model_override_wins(monkeypatch):
    """Set → OLLAMA_JUDGE_MODEL is the override; OLLAMA_MODEL (extraction surface) untouched."""
    monkeypatch.setenv("OLLAMA_MODEL", "gemma4:e4b")
    monkeypatch.setenv("EVOLUTION_OLLAMA_JUDGE_MODEL", "gemma4:12b")
    import evolution.config
    cfg = importlib.reload(evolution.config)
    assert cfg.OLLAMA_JUDGE_MODEL == "gemma4:12b"
    assert cfg.OLLAMA_MODEL == "gemma4:e4b"   # atom/entity extraction surface unchanged


def test_provider_default_model_unset_is_noop(monkeypatch):
    """OllamaProvider.default_model == OLLAMA_MODEL when override unset (byte-identical prod)."""
    monkeypatch.setenv("OLLAMA_MODEL", "gemma4:e4b")
    monkeypatch.delenv("EVOLUTION_OLLAMA_JUDGE_MODEL", raising=False)
    import evolution.config
    importlib.reload(evolution.config)
    import evolution.judge.providers.ollama as op
    importlib.reload(op)
    assert op.OllamaProvider().default_model == "gemma4:e4b"


def test_provider_default_model_reflects_override(monkeypatch):
    """OllamaProvider.default_model returns the override when set (the hot + batch injection point)."""
    monkeypatch.setenv("OLLAMA_MODEL", "gemma4:e4b")
    monkeypatch.setenv("EVOLUTION_OLLAMA_JUDGE_MODEL", "gemma4:12b")
    import evolution.config
    importlib.reload(evolution.config)
    import evolution.judge.providers.ollama as op
    importlib.reload(op)
    assert op.OllamaProvider().default_model == "gemma4:12b"
