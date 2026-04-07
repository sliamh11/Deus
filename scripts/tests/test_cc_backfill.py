"""
Tests for evolution/cc_backfill.py — Claude Code session ingestion.

Covers:
  - Pair extraction from CC .jsonl format
  - Filtering of tool_results, system noise, streaming duplicates
  - Deterministic ID generation
  - Project name inference
"""
import json
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from evolution.cc_backfill import (
    _deterministic_id,
    _extract_assistant_content,
    _extract_pairs,
    _extract_user_text,
    _infer_project_name,
    _is_skip_prompt,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_user_entry(content, **extra):
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


def _make_assistant_entry(content_blocks, stop_reason="end_turn", msg_id="msg_001", **extra):
    return {
        "type": "assistant",
        "message": {
            "id": msg_id,
            "role": "assistant",
            "content": content_blocks,
            "stop_reason": stop_reason,
        },
        **extra,
    }


def _write_jsonl(tmp_path, filename, entries):
    fpath = tmp_path / filename
    fpath.write_text("\n".join(json.dumps(e) for e in entries))
    return fpath


# ── _extract_user_text ───────────────────────────────────────────────────────


class TestExtractUserText:
    def test_plain_string(self):
        entry = _make_user_entry("Hello, how are you?")
        assert _extract_user_text(entry) == "Hello, how are you?"

    def test_text_blocks(self):
        entry = _make_user_entry([
            {"type": "text", "text": "Hello "},
            {"type": "text", "text": "world"},
        ])
        assert _extract_user_text(entry) == "Hello  world"

    def test_tool_result_returns_none(self):
        entry = _make_user_entry([
            {"type": "tool_result", "tool_use_id": "abc", "content": [{"type": "text", "text": "result"}]},
        ])
        assert _extract_user_text(entry) is None

    def test_empty_string_returns_none(self):
        entry = _make_user_entry("")
        assert _extract_user_text(entry) is None


# ── _extract_assistant_content ───────────────────────────────────────────────


class TestExtractAssistantContent:
    def test_text_only(self):
        entry = _make_assistant_entry([
            {"type": "text", "text": "Here is my answer."},
        ])
        result = _extract_assistant_content(entry)
        assert result["text"] == "Here is my answer."
        assert result["tools"] == []

    def test_tool_use(self):
        entry = _make_assistant_entry([
            {"type": "text", "text": "Let me check."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
        ])
        result = _extract_assistant_content(entry)
        assert result["text"] == "Let me check."
        assert result["tools"] == ["Bash"]

    def test_streaming_partial_returns_none(self):
        entry = _make_assistant_entry(
            [{"type": "text", "text": "partial..."}],
            stop_reason=None,
        )
        assert _extract_assistant_content(entry) is None

    def test_empty_content_returns_none(self):
        entry = _make_assistant_entry([])
        assert _extract_assistant_content(entry) is None


# ── _is_skip_prompt ──────────────────────────────────────────────────────────


class TestIsSkipPrompt:
    def test_command_message(self):
        assert _is_skip_prompt("<command-message>compact</command-message>")

    def test_task_notification(self):
        assert _is_skip_prompt("<task-notification><task-id>abc</task-id>")

    def test_normal_prompt(self):
        assert not _is_skip_prompt("How do I fix this bug in the auth module?")


# ── _extract_pairs ───────────────────────────────────────────────────────────


class TestExtractPairs:
    def test_simple_conversation(self, tmp_path):
        entries = [
            _make_user_entry("What is the meaning of life? Tell me in detail please."),
            _make_assistant_entry(
                [{"type": "text", "text": "The meaning of life is a philosophical question that has been debated for centuries."}],
                msg_id="msg_001",
            ),
            _make_user_entry("Can you elaborate more on that topic?"),
            _make_assistant_entry(
                [{"type": "text", "text": "Of course, there are many perspectives from different philosophical traditions and schools of thought."}],
                msg_id="msg_002",
            ),
        ]
        fpath = _write_jsonl(tmp_path, "test.jsonl", entries)
        pairs = list(_extract_pairs(fpath))
        assert len(pairs) == 2
        assert "meaning of life" in pairs[0]["prompt"]
        assert pairs[0]["pair_index"] == 0
        assert pairs[1]["pair_index"] == 1

    def test_skips_tool_result_users(self, tmp_path):
        entries = [
            _make_user_entry("Fix the bug in the authentication module please."),
            _make_assistant_entry(
                [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"path": "auth.ts"}}],
                msg_id="msg_001",
            ),
            # Tool result — should be skipped as a "user" turn
            _make_user_entry([
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "file contents"}]},
            ]),
            _make_assistant_entry(
                [{"type": "text", "text": "I found the bug. The authentication check was missing a null guard for the session token."}],
                msg_id="msg_002",
            ),
        ]
        fpath = _write_jsonl(tmp_path, "test.jsonl", entries)
        pairs = list(_extract_pairs(fpath))
        assert len(pairs) == 1
        assert "bug" in pairs[0]["prompt"]
        assert "found the bug" in pairs[0]["response"]

    def test_skips_command_messages(self, tmp_path):
        entries = [
            _make_user_entry("<command-message>resume</command-message>"),
            _make_assistant_entry(
                [{"type": "text", "text": "Loading context from the vault for this session now."}],
                msg_id="msg_001",
            ),
            _make_user_entry("Now fix the real bug in the payment processing module."),
            _make_assistant_entry(
                [{"type": "text", "text": "Looking at the payment module, I can see the issue with the amount calculation."}],
                msg_id="msg_002",
            ),
        ]
        fpath = _write_jsonl(tmp_path, "test.jsonl", entries)
        pairs = list(_extract_pairs(fpath))
        assert len(pairs) == 1
        assert "payment" in pairs[0]["prompt"]

    def test_deduplicates_streaming_assistants(self, tmp_path):
        entries = [
            _make_user_entry("Tell me about quantum computing in detail please."),
            # Streaming partial (stop_reason=null)
            _make_assistant_entry(
                [{"type": "text", "text": "Quantum"}],
                stop_reason=None, msg_id="msg_001",
            ),
            # Complete (stop_reason set)
            _make_assistant_entry(
                [{"type": "text", "text": "Quantum computing uses qubits which can exist in superposition states."}],
                stop_reason="end_turn", msg_id="msg_001",
            ),
        ]
        fpath = _write_jsonl(tmp_path, "test.jsonl", entries)
        pairs = list(_extract_pairs(fpath))
        assert len(pairs) == 1
        assert "superposition" in pairs[0]["response"]

    def test_extracts_tool_names(self, tmp_path):
        entries = [
            _make_user_entry("Read the config file and tell me what's wrong."),
            _make_assistant_entry([
                {"type": "text", "text": "Let me check the config file for any issues or misconfigurations."},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
            ], msg_id="msg_001"),
        ]
        fpath = _write_jsonl(tmp_path, "test.jsonl", entries)
        pairs = list(_extract_pairs(fpath))
        assert len(pairs) == 1
        assert pairs[0]["tools"] == ["Read"]

    def test_empty_file(self, tmp_path):
        fpath = _write_jsonl(tmp_path, "empty.jsonl", [])
        pairs = list(_extract_pairs(fpath))
        assert pairs == []


# ── Deterministic IDs ────────────────────────────────────────────────────────


class TestDeterministicId:
    def test_consistent(self):
        a = _deterministic_id("session-abc", 0)
        b = _deterministic_id("session-abc", 0)
        assert a == b

    def test_different_for_different_pairs(self):
        a = _deterministic_id("session-abc", 0)
        b = _deterministic_id("session-abc", 1)
        assert a != b

    def test_different_for_different_sessions(self):
        a = _deterministic_id("session-abc", 0)
        b = _deterministic_id("session-xyz", 0)
        assert a != b


# ── Project name inference ───────────────────────────────────────────────────


class TestInferProjectName:
    def test_deus_project(self, tmp_path):
        fpath = tmp_path / "-Users-liam10play-deus" / "abc.jsonl"
        fpath.parent.mkdir(parents=True)
        assert _infer_project_name(fpath) == "deus"

    def test_nested_path(self, tmp_path):
        fpath = tmp_path / "-Users-liam10play-Dev-myapp" / "abc.jsonl"
        fpath.parent.mkdir(parents=True)
        assert _infer_project_name(fpath) == "myapp"
