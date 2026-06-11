"""Regression tests for the UserPromptSubmit retrieval hook abstain delegation (#766).

The hook used to hardcode a 0.45 abstain fallback, overriding the library's
resolution chain (env -> learned artifact -> provider default). It now passes
None unless DEUS_TREE_ABSTAIN is explicitly set, so memory_query.recall delegates
to memory_tree.DEFAULT_ABSTAIN_THRESHOLD — the single owner.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent


def _load(name: str, rel: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _ROOT / "scripts" / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


hook = _load("memory_retrieval_hook", "memory_retrieval_hook.py")
mq = _load("memory_query", "memory_query.py")


def _run_hook(monkeypatch, prompt: str = "what do you know about my preferences") -> dict:
    captured: dict = {}

    def fake_recall(query, **kwargs):
        captured.update(kwargs)
        captured["query"] = query
        return {"context": "", "paths": [], "confidence": 0.0, "fell_back": True}

    # The hook does `import memory_query as mq` inside main(); patching the
    # sys.modules copy makes that import resolve to our fake.
    monkeypatch.setattr(mq, "recall", fake_recall)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt})))
    hook.main()
    return captured


def test_hook_delegates_to_library_when_env_unset(monkeypatch):
    monkeypatch.delenv("DEUS_TREE_ABSTAIN", raising=False)
    captured = _run_hook(monkeypatch)
    # None → recall falls back to memory_tree.DEFAULT_ABSTAIN_THRESHOLD (the chain).
    assert captured["abstain_threshold"] is None


def test_hook_honors_explicit_env_abstain(monkeypatch):
    monkeypatch.setenv("DEUS_TREE_ABSTAIN", "0.37")
    captured = _run_hook(monkeypatch)
    assert captured["abstain_threshold"] == 0.37


def test_hook_treats_empty_env_as_unset(monkeypatch):
    # Empty / whitespace must delegate (None), not crash on float("").
    monkeypatch.setenv("DEUS_TREE_ABSTAIN", "  ")
    captured = _run_hook(monkeypatch)
    assert captured["abstain_threshold"] is None
