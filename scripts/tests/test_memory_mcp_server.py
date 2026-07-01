"""Tests for scripts/memory_mcp_server.py — offline, stubbed recall()."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent

# ------------------------------------------------------------------
# Load memory_mcp_server as a module (mirrors test_memory_query.py).
# memory_query is loaded transitively; conftest already loaded memory_tree.
# ------------------------------------------------------------------
if "memory_mcp_server" in sys.modules:
    mms = sys.modules["memory_mcp_server"]
else:
    _SPEC = importlib.util.spec_from_file_location(
        "memory_mcp_server", _ROOT / "scripts" / "memory_mcp_server.py"
    )
    mms = importlib.util.module_from_spec(_SPEC)
    sys.modules["memory_mcp_server"] = mms
    _SPEC.loader.exec_module(mms)

mq = sys.modules["memory_query"]

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------
FAKE_RECALL_RESULT = {
    "context": "=== Auto-retrieved memory ===\nsome content\n=== End ===",
    "paths": ["CLAUDE.md", "INFRA.md"],
    "confidence": 0.72,
    "fell_back": False,
}


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------
class TestMemoryRecallTool:
    """Test the memory_recall tool function directly."""

    def test_calls_recall_with_correct_args(self, monkeypatch):
        # Default (flag unset): procedures ON -> exclude_kinds={"standard"}.
        monkeypatch.delenv("DEUS_PROCEDURE_MEMORY", raising=False)
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            result = mms.memory_recall("what timezone?", k=5, source="test")

        mock_recall.assert_called_once_with(
            "what timezone?",
            k=5,
            source="test",
            exclude_kinds={"standard"},
            max_context_chars=mms._MAX_CONTEXT_CHARS,
        )
        assert result == FAKE_RECALL_RESULT

    @pytest.mark.parametrize("value", ["1", " 1 ", "\t1\n", "true", "anything"])
    def test_procedures_on_by_default_and_for_non_disable_values(
        self, monkeypatch, value
    ):
        # Procedures recall by default. Only an explicit "0" disables, so every
        # non-"0" value (incl. unset, handled above) keeps procedures eligible.
        monkeypatch.setenv("DEUS_PROCEDURE_MEMORY", value)
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            mms.memory_recall("how do I capture a procedure?")

        _, kwargs = mock_recall.call_args
        assert kwargs["exclude_kinds"] == {"standard"}

    @pytest.mark.parametrize("value", ["0", " 0 ", "\t0\n"])
    def test_explicit_zero_is_the_kill_switch(self, monkeypatch, value):
        # The ONLY disable is an explicit "0" (stripped). Then exclude_kinds=None,
        # which falls through to recall()'s default that also drops procedures.
        monkeypatch.setenv("DEUS_PROCEDURE_MEMORY", value)
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            mms.memory_recall("how do I capture a procedure?")

        _, kwargs = mock_recall.call_args
        assert kwargs["exclude_kinds"] is None

    def test_default_source_is_mcp(self):
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            mms.memory_recall("hello")

        _, kwargs = mock_recall.call_args
        assert kwargs["source"] == "mcp"

    def test_default_k_is_3(self):
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            mms.memory_recall("hello")

        _, kwargs = mock_recall.call_args
        assert kwargs["k"] == 3

    def test_returns_full_dict(self):
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT):
            result = mms.memory_recall("test query")

        assert "context" in result
        assert "paths" in result
        assert "confidence" in result
        assert "fell_back" in result

    def test_propagates_recall_error(self):
        with patch.object(mq, "recall", side_effect=RuntimeError("db down")):
            with pytest.raises(RuntimeError, match="db down"):
                mms.memory_recall("test")


class TestMissingMcpPackage:
    """Test clean error when mcp package is not installed."""

    def test_exits_with_error_message(self, capsys, monkeypatch):
        monkeypatch.setattr(mms, "_MCP_AVAILABLE", False)

        with pytest.raises(SystemExit) as exc_info:
            mms._run_mcp_server()

        assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "mcp package not installed" in err


class TestServerName:
    """Verify server metadata."""

    @pytest.mark.skipif(
        not getattr(mms, "_MCP_AVAILABLE", False),
        reason="mcp package not installed",
    )
    def test_server_creates_with_correct_name(self, monkeypatch):
        """If mcp is available, verify the server is named 'deus-memory'."""
        from mcp.server.fastmcp import FastMCP

        created_servers = []
        original_init = FastMCP.__init__

        def spy_init(self, name, *args, **kwargs):
            created_servers.append(name)
            original_init(self, name, *args, **kwargs)

        with patch.object(FastMCP, "__init__", spy_init), \
             patch.object(FastMCP, "run"):
            mms._run_mcp_server()

        assert "deus-memory" in created_servers


class TestMemoryRecallCap:
    """LIA-344: server-side payload bound (k clamp + max_context_chars)."""

    def test_forwards_max_context_chars(self):
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            mms.memory_recall("hello")
        _, kwargs = mock_recall.call_args
        assert kwargs["max_context_chars"] == mms._MAX_CONTEXT_CHARS

    def test_clamps_large_k_to_ceiling(self):
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            mms.memory_recall("hello", k=9999)
        _, kwargs = mock_recall.call_args
        assert kwargs["k"] == mms._K_MAX

    def test_clamps_non_positive_k_to_one(self):
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            mms.memory_recall("hello", k=0)
        _, kwargs = mock_recall.call_args
        assert kwargs["k"] == 1

    def test_k_within_range_passes_through(self):
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            mms.memory_recall("hello", k=3)
        _, kwargs = mock_recall.call_args
        assert kwargs["k"] == 3


class TestIntEnvGuard:
    """LIA-344: env override parsing with safe fallback."""

    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("DEUS_MCP_RECALL_MAX_CHARS", raising=False)
        assert mms._int_env("DEUS_MCP_RECALL_MAX_CHARS", 8192) == 8192

    def test_valid_override(self, monkeypatch):
        monkeypatch.setenv("DEUS_MCP_RECALL_MAX_CHARS", "5000")
        assert mms._int_env("DEUS_MCP_RECALL_MAX_CHARS", 8192) == 5000

    @pytest.mark.parametrize("bad", ["abc", "", "0", "-10", "3.5"])
    def test_invalid_or_non_positive_falls_back(self, monkeypatch, bad):
        monkeypatch.setenv("DEUS_MCP_RECALL_MAX_CHARS", bad)
        assert mms._int_env("DEUS_MCP_RECALL_MAX_CHARS", 8192) == 8192
