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
    "paths": ["CLAUDE.md", "STATE.md"],
    "confidence": 0.72,
    "fell_back": False,
}


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------
class TestMemoryRecallTool:
    """Test the memory_recall tool function directly."""

    def test_calls_recall_with_correct_args(self):
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            result = mms.memory_recall("what timezone?", k=5, source="test")

        mock_recall.assert_called_once_with("what timezone?", k=5, source="test")
        assert result == FAKE_RECALL_RESULT

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
