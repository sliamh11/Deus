"""Tests for memory_retrieval_hook.py — UserPromptSubmit auto-retrieval.

The hook must not resolve the abstain threshold itself: memory_query.recall
falls back to memory_tree.DEFAULT_ABSTAIN_THRESHOLD, which is the single
source of truth (env var -> learned artifact -> provider-aware default).
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent


def _load_hook(monkeypatch, recall_calls: list[dict]):
    """Load the hook module with stubbed memory_query / session_concepts."""
    fake_mq = types.ModuleType("memory_query")

    def fake_recall(prompt, **kwargs):
        recall_calls.append(kwargs)
        return {"context": "stubbed context"}

    fake_mq.recall = fake_recall

    fake_sc = types.ModuleType("session_concepts")
    fake_sc.extract_terms = lambda prompt: []
    fake_sc.update_concepts = lambda session_id, terms: None

    monkeypatch.setitem(sys.modules, "memory_query", fake_mq)
    monkeypatch.setitem(sys.modules, "session_concepts", fake_sc)

    spec = importlib.util.spec_from_file_location(
        "memory_retrieval_hook_under_test",
        SCRIPTS_DIR / "memory_retrieval_hook.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_recall_called_without_abstain_override(monkeypatch, capsys):
    calls: list[dict] = []
    hook = _load_hook(monkeypatch, calls)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"prompt": "what do you know about my style"})),
    )

    hook.main()

    assert len(calls) == 1
    assert "abstain_threshold" not in calls[0]
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["additionalContext"] == "stubbed context"


def test_short_prompt_bails_before_recall(monkeypatch, capsys):
    calls: list[dict] = []
    hook = _load_hook(monkeypatch, calls)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "hi"})))

    hook.main()

    assert calls == []
    assert capsys.readouterr().out == ""

def test_session_concepts_passed_to_recall(monkeypatch, capsys):
    calls: list[dict] = []
    hook = _load_hook(monkeypatch, calls)
    sys.modules["session_concepts"].update_concepts = lambda sid, terms: ["drums", "music"]
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"prompt": "what instruments do I play", "session_id": "s1"})),
    )

    hook.main()

    assert len(calls) == 1
    assert calls[0]["concepts"] == ["drums", "music"]
    assert "abstain_threshold" not in calls[0]
