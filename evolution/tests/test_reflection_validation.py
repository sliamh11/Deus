"""Tests for LIA-213 reflection save-time validation."""
from unittest.mock import MagicMock, patch

import pytest

import evolution.reflexion.store as store_mod
from evolution.reflexion.validation import is_valid_reflection


# ── is_valid_reflection unit tests ────────────────────────────────────────────


def test_accepts_a_clean_lesson():
    ok, reason = is_valid_reflection(
        "Prefer COALESCE over plain assignment in an upsert so a NULL re-log "
        "does not clobber a previously persisted value."
    )
    assert ok is True
    assert reason == ""


@pytest.mark.parametrize("content,marker", [
    ("<start_of_turn>user\nleaked turn", "banned_token:<start_of_turn>"),
    ("dump </start_of_turn> User: gate", "banned_token:</start_of_turn>"),
    ("some lesson <end_of_turn> trailing", "banned_token:<end_of_turn>"),
    ("garbage <unused0> tokens", "banned_token:<unused"),
    ("<bos> leaked bos", "banned_token:<bos>"),
    ("trailing eos <eos>", "banned_token:<eos>"),
    ("[INST] do the thing [/INST]", "banned_token:[INST]"),
    ("system <<SYS>> leak", "banned_token:<<SYS>>"),
])
def test_rejects_raw_control_tokens(content, marker):
    ok, reason = is_valid_reflection(content)
    assert ok is False
    assert reason == marker


@pytest.mark.parametrize("content", [
    "text <|assistant|> more",
    "newline-split <|assistant\nhere|> marker",  # multiline: must not slip past a regex
])
def test_rejects_pipe_markers(content):
    ok, reason = is_valid_reflection(content)
    assert ok is False
    assert reason == "banned_token:<|"


@pytest.mark.parametrize("content", [
    "Score: 0.8/1.0 | Breakdown: {q:0.9} | Rationale: ...",
    "score: 0.42/1.0 | breakdown: lowercased leak",  # case-insensitive
])
def test_rejects_judge_preamble_echo(content):
    ok, reason = is_valid_reflection(content)
    assert ok is False
    assert reason.startswith("banned_pattern:")


def test_rejects_runaway_length():
    ok, reason = is_valid_reflection("x" * 5000)
    assert ok is False
    assert reason.startswith("too_long")


@pytest.mark.parametrize("content", ["", "   ", "\n\t ", None])
def test_rejects_empty(content):
    ok, reason = is_valid_reflection(content)
    assert ok is False
    assert reason == "empty"


# ── HIGH-PRECISION guard: legit lessons that DISCUSS prompt structure must pass ─
# Regression guard for the false positives the dry-run surfaced: a real lesson
# (one was retrieved 90 times) that quotes prompt-structure terms is not corrupt.


@pytest.mark.parametrize("content", [
    "Wrap distinct instructions in consistent tags (e.g. <gate-spec>) for clarity.",
    "The agent hallucinated malformed tool calls `Tools: [, ]` and executed nothing; "
    "validate the tool manifest before dispatch.",
    "Echo the retrieval envelope <reflections> back into a new reflection causes "
    "recursion -- strip it.",
    "Reply in this exact format was ignored; enforce the schema downstream instead.",
    "A long but coherent analysis of why the structured-output task failed: " + "detail " * 80,
])
def test_accepts_topical_mentions_and_long_lessons(content):
    ok, reason = is_valid_reflection(content)
    assert ok is True, f"false positive: {reason}"


# ── save_reflection integration: reject short-circuits before the embed ───────


def test_save_reflection_rejects_corrupted_before_embedding():
    bad = "<start_of_turn>user\n</start_of_turn> Assistant: leaked transcript"
    with patch.object(store_mod, "_embed") as m_embed, \
            patch.object(store_mod, "get_storage") as m_store:
        result = store_mod.save_reflection(bad, "tool_use", 1.0)
    assert result is None
    m_embed.assert_not_called()   # rejected before the expensive embed
    m_store.assert_not_called()   # and never reached the dedup/persist path


def test_save_reflection_accepts_valid_content():
    good = "When two writers can both observe NULL, gate the side effect on an atomic claim."
    fake_store = MagicMock()
    fake_store.check_reflection_duplicate.return_value = False
    with patch.object(store_mod, "_embed", return_value=[0.0] * 768), \
            patch.object(store_mod, "get_storage", return_value=fake_store):
        result = store_mod.save_reflection(good, "reasoning", 0.5)
    assert result is not None
    fake_store.save_reflection.assert_called_once()
