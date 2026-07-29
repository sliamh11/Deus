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
import types
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


# ── Fork-origin coverage (kept across the upstream merge): the hook passes
# session concepts to recall, and bails on short prompts before recalling.
# Fully-stubbed loader so neither memory_query nor session_concepts loads real
# deps. Abstain assertions match HEAD source (passes abstain_threshold=None).


def _load_hook(monkeypatch, recall_calls: list[dict]):
    """Load the hook module with stubbed memory_query / session_concepts."""
    fake_mq = types.ModuleType("memory_query")

    def fake_recall(prompt, **kwargs):
        recall_calls.append(kwargs)
        return {"context": "stubbed context"}

    fake_mq.recall = fake_recall
    # LIA-355: the hook's session_id/dedup branch reads mq.WRAP_OVERHEAD_CHARS
    # unconditionally once a session_id is present. Keep the stub in sync with
    # the real module's contract (scripts/memory_query.py:97) or any test that
    # passes session_id hits AttributeError on this stub instead.
    fake_mq.WRAP_OVERHEAD_CHARS = 512

    fake_sc = types.ModuleType("session_concepts")
    fake_sc.extract_terms = lambda prompt: []
    fake_sc.update_concepts = lambda session_id, terms: None

    monkeypatch.setitem(sys.modules, "memory_query", fake_mq)
    monkeypatch.setitem(sys.modules, "session_concepts", fake_sc)

    spec = importlib.util.spec_from_file_location(
        "memory_retrieval_hook_under_test",
        _ROOT / "scripts" / "memory_retrieval_hook.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_session_concepts_passed_to_recall(monkeypatch, capsys):
    calls: list[dict] = []
    hook_mod = _load_hook(monkeypatch, calls)
    sys.modules["session_concepts"].update_concepts = lambda sid, terms: ["drums", "music"]
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"prompt": "what instruments do I play", "session_id": "s1"})),
    )

    hook_mod.main()

    assert len(calls) == 1
    assert calls[0]["concepts"] == ["drums", "music"]
    assert calls[0]["abstain_threshold"] is None


def test_short_prompt_bails_before_recall(monkeypatch, capsys):
    calls: list[dict] = []
    hook_mod = _load_hook(monkeypatch, calls)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": "hi"})))

    hook_mod.main()

    assert calls == []
    assert capsys.readouterr().out == ""
def test_hook_excludes_procedures_when_flag_unset(monkeypatch):
    # LIA-334: procedure surfacing is opt-in. Flag off → pass None so recall()
    # uses its dormant-by-default exclusion ({"standard","procedure"}); the
    # kill-switch lives in the shared recall layer, not the hook.
    monkeypatch.delenv("DEUS_PROCEDURE_MEMORY", raising=False)
    captured = _run_hook(monkeypatch)
    assert captured["exclude_kinds"] is None


def test_hook_includes_procedures_when_flag_on(monkeypatch):
    # Flag "1" → opt procedures IN by excluding only {"standard"} ("standard"
    # stays excluded as usual; procedures become eligible to surface).
    monkeypatch.setenv("DEUS_PROCEDURE_MEMORY", "1")
    captured = _run_hook(monkeypatch)
    assert captured["exclude_kinds"] == {"standard"}


def test_hook_keeps_procedures_dormant_when_flag_zero(monkeypatch):
    # Any value other than "1" is off (kill-switch) → None → recall default.
    monkeypatch.setenv("DEUS_PROCEDURE_MEMORY", "0")
    captured = _run_hook(monkeypatch)
    assert captured["exclude_kinds"] is None


# ── EP-001: skip retrieval on synthetic task-notification prompts ───────────
# The early return must fire BEFORE the deferred `import session_concepts` /
# `import memory_query` lines — that's what avoids both the ~200ms import
# cost and the session-concept-store pollution. Test 1 verifies this directly
# by proving the imports never happen, not just that no output was produced.


def test_synthetic_notification_prompt_skips_recall_and_deferred_imports(monkeypatch, capsys, tmp_path):
    """EP-001 test 1: anchored marker bails before the deferred imports.

    Deliberately does NOT use `_load_hook` above: that fixture pre-inserts
    stub `memory_query`/`session_concepts` into sys.modules as part of its own
    setup (monkeypatch.setitem), which would make an absence-assertion pass
    trivially regardless of whether this fix actually prevents the import —
    a captured-oracle bug caught on review. Instead, delete both names from
    sys.modules immediately before the call (fail-open: if the fix's early
    return doesn't fire, the real deferred imports execute and repopulate
    sys.modules, and this test correctly fails) and reuse the module-level
    `hook` object loaded via `_load()` above.

    Regression guard: if the early return above ever breaks, this is the one
    test that would exercise the REAL `memory_query.recall()` (a real
    embedding call) and its real `_log_retrieval` write. The conftest
    autouse fixture only isolates `memory_tree`'s DB/log paths, not
    `memory_query`'s own `DEUS_RETRIEVAL_LOG` — so redirect it here too,
    otherwise a future regression would silently pollute the production
    `~/.deus/memory_retrieval_log.jsonl` that this EP's own live acceptance
    check (docs/exec-plans/active/EP-001-*.md) depends on staying clean.
    """
    monkeypatch.delitem(sys.modules, "memory_query", raising=False)
    monkeypatch.delitem(sys.modules, "session_concepts", raising=False)
    monkeypatch.setenv("DEUS_RETRIEVAL_LOG", str(tmp_path / "regression-guard-log.jsonl"))
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({
            "prompt": "<task-notification>\n<task-id>abc123</task-id>\n<status>completed</status>\n</task-notification>",
        })),
    )

    hook.main()

    assert "memory_query" not in sys.modules
    assert "session_concepts" not in sys.modules
    assert capsys.readouterr().out == ""


def test_notification_marker_mid_prompt_is_not_skipped(monkeypatch):
    """EP-001 test 2: anchored-vs-substring. A prompt that merely QUOTES a
    notification (marker not at the start) must still retrieve normally —
    the false-positive class found in real quoted-transcript/summarizer
    prompts during review."""
    prompt = (
        "I noticed the last message looked like <task-notification>...</task-notification> "
        "-- can you explain what that means?"
    )
    captured = _run_hook(monkeypatch, prompt=prompt)
    assert captured  # fake_recall WAS called -- retrieval proceeded normally
    assert captured["query"] == prompt


def test_notification_marker_with_leading_whitespace_is_skipped(monkeypatch, capsys):
    """EP-001 test 3: leading whitespace/newlines before the marker must
    still match after stripping."""
    captured = _run_hook(
        monkeypatch,
        prompt="\n\n   <task-notification>\n<task-id>xyz</task-id>\n</task-notification>",
    )
    assert captured == {}  # fake_recall never called
    assert capsys.readouterr().out == ""


def test_notification_marker_wrong_case_is_not_skipped(monkeypatch):
    """EP-001 test 4: the match is case-sensitive, matching the observed
    real-data shape (all production occurrences are lowercase-exact)."""
    prompt = "<Task-Notification>\n<task-id>xyz</task-id>\n</Task-Notification>"
    captured = _run_hook(monkeypatch, prompt=prompt)
    assert captured  # different case -> not matched -> retrieval proceeds
    assert captured["query"] == prompt


def test_notification_marker_alone_clears_min_prompt_len(monkeypatch, capsys):
    """EP-001 test 5: the bare marker (verified 19 chars) clears the
    pre-existing MIN_PROMPT_LEN gate (verified =10) and is reached and
    matched by the new early return -- grounded in the actual constant
    values, not an assumed length."""
    marker = "<task-notification>"
    assert len(marker) == 19
    assert hook.MIN_PROMPT_LEN == 10
    assert len(marker) > hook.MIN_PROMPT_LEN

    captured = _run_hook(monkeypatch, prompt=marker)
    assert captured == {}  # reached the marker check and was skipped, not recalled
    assert capsys.readouterr().out == ""
