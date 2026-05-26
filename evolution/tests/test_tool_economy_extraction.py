"""Tests for enriched extraction and compose_score backward compat."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch

from evolution.cc_backfill import _extract_pairs
from evolution.judge.criteria import compose_score, _DIM_DEFAULTS, COMPOSITE_WEIGHTS


def _make_jsonl(entries: list[dict]) -> str:
    return "\n".join(json.dumps(e) for e in entries)


def _user_entry(text: str) -> dict:
    return {"type": "user", "message": {"content": text}}


def _assistant_entry(msg_id: str, tool_blocks: list[dict] = None,
                     text: str = "", stop_reason: str = "tool_use") -> dict:
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for tb in (tool_blocks or []):
        content.append({"type": "tool_use", **tb})
    return {
        "type": "assistant",
        "message": {"id": msg_id, "stop_reason": stop_reason, "content": content},
    }


def _tool_result_entry() -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


class TestEnrichedExtraction:
    def test_multi_msg_id_aggregates_tool_calls(self, tmp_path):
        """tool_calls should include calls from ALL assistant msgs, not just the last."""
        entries = [
            _user_entry("Fix the bug in foo.py please, it is broken"),
            _assistant_entry("msg_1", [
                {"name": "Read", "input": {"file_path": "/src/foo.py"}},
            ]),
            _tool_result_entry(),
            _assistant_entry("msg_2", [
                {"name": "Edit", "input": {"file_path": "/src/foo.py", "old_string": "x", "new_string": "y"}},
            ]),
            _tool_result_entry(),
            _assistant_entry("msg_3", text="Done, I fixed the bug in foo.py.", stop_reason="end_turn"),
        ]
        jsonl_path = tmp_path / "test.jsonl"
        jsonl_path.write_text(_make_jsonl(entries))

        pairs = list(_extract_pairs(jsonl_path))
        assert len(pairs) == 1
        p = pairs[0]
        assert len(p["tool_calls"]) == 2
        assert p["tool_calls"][0]["name"] == "Read"
        assert p["tool_calls"][0]["file_path"] == "/src/foo.py"
        assert p["tool_calls"][1]["name"] == "Edit"
        assert p["tool_calls"][1]["file_path"] == "/src/foo.py"

    def test_tools_key_is_last_assistant_only(self, tmp_path):
        """The 'tools' key should only contain names from the last assistant msg."""
        entries = [
            _user_entry("Fix the bug in foo.py please, it is broken"),
            _assistant_entry("msg_1", [
                {"name": "Read", "input": {"file_path": "/src/foo.py"}},
            ]),
            _tool_result_entry(),
            _assistant_entry("msg_2", text="Done, I fixed the bug in foo.py.", stop_reason="end_turn"),
        ]
        jsonl_path = tmp_path / "test.jsonl"
        jsonl_path.write_text(_make_jsonl(entries))

        pairs = list(_extract_pairs(jsonl_path))
        assert len(pairs) == 1
        assert pairs[0]["tools"] == []
        assert len(pairs[0]["tool_calls"]) == 1

    def test_pair_index_increments(self, tmp_path):
        """Multiple pairs should have sequential pair_index values."""
        entries = [
            _user_entry("First question about the codebase architecture"),
            _assistant_entry("msg_1", text="Here is the answer to the first question about architecture.", stop_reason="end_turn"),
            _user_entry("Second question about deployment patterns"),
            _assistant_entry("msg_2", text="Here is the answer to the second question about deployment.", stop_reason="end_turn"),
        ]
        jsonl_path = tmp_path / "test.jsonl"
        jsonl_path.write_text(_make_jsonl(entries))

        pairs = list(_extract_pairs(jsonl_path))
        assert len(pairs) == 2
        assert pairs[0]["pair_index"] == 0
        assert pairs[1]["pair_index"] == 1

    def test_subagent_type_extracted(self, tmp_path):
        """Agent tool calls should have subagent_type in tool_calls."""
        entries = [
            _user_entry("Explore the evolution subsystem thoroughly please"),
            _assistant_entry("msg_1", [
                {"name": "Agent", "input": {"subagent_type": "Explore", "prompt": "find files"}},
            ]),
            _tool_result_entry(),
            _assistant_entry("msg_2", text="I found the files in the evolution subsystem here.", stop_reason="end_turn"),
        ]
        jsonl_path = tmp_path / "test.jsonl"
        jsonl_path.write_text(_make_jsonl(entries))

        pairs = list(_extract_pairs(jsonl_path))
        assert pairs[0]["tool_calls"][0]["subagent_type"] == "Explore"

    def test_command_extracted_for_bash(self, tmp_path):
        """Bash tool calls should have command in tool_calls."""
        entries = [
            _user_entry("Search for the function definition in the codebase"),
            _assistant_entry("msg_1", [
                {"name": "Bash", "input": {"command": "grep -rn 'def foo' src/"}},
            ]),
            _tool_result_entry(),
            _assistant_entry("msg_2", text="Found the function definition at line 42 in src.", stop_reason="end_turn"),
        ]
        jsonl_path = tmp_path / "test.jsonl"
        jsonl_path.write_text(_make_jsonl(entries))

        pairs = list(_extract_pairs(jsonl_path))
        assert pairs[0]["tool_calls"][0]["command"] == "grep -rn 'def foo' src/"


class TestComposeScoreBackwardCompat:
    def test_old_4dim_row_gets_neutral_tool_economy(self):
        """Old rows missing tool_economy should default to 1.0, not 0.0."""
        old_dims = {"quality": 1.0, "safety": 1.0, "tool_use": 1.0, "personalization": 1.0}
        score = compose_score(old_dims)
        expected = 0.35 + 0.25 + 0.15 + 0.15 + 0.10  # tool_economy=1.0 default
        assert score == pytest.approx(expected)

    def test_new_5dim_row(self):
        dims = {"quality": 1.0, "safety": 1.0, "tool_use": 1.0,
                "personalization": 1.0, "tool_economy": 0.5}
        score = compose_score(dims)
        expected = 0.35 + 0.25 + 0.15 + 0.15 + 0.05
        assert score == pytest.approx(expected)

    def test_dim_defaults_match_weights_keys(self):
        assert set(COMPOSITE_WEIGHTS.keys()) == set(_DIM_DEFAULTS.keys())
