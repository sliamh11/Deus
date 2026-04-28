#!/usr/bin/env python3
"""Deus Memory MCP Server (stdio transport).

Exposes the memory recall pipeline as a single MCP tool so any agent that can
register an MCP server (Claude Code, Cursor, Windsurf, etc.) gets the same
retrieval quality as the host hook — closing the cross-interface parity gap.

Platform: Linux/macOS only (depends on sqlite_vec C extension + Ollama).

Usage:
    python3 scripts/memory_mcp_server.py   # stdio

Register in ~/.claude/settings.json:
    {
      "mcpServers": {
        "deus-memory": {
          "command": "python3",
          "args": ["/path/to/deus/scripts/memory_mcp_server.py"],
          "env": {}
        }
      }
    }
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    print(
        "memory_mcp_server.py requires Linux or macOS (sqlite_vec + Ollama).",
        file=sys.stderr,
    )
    sys.exit(1)

from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import memory_query  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


def memory_recall(query: str, k: int = 3, source: str = "mcp") -> dict:
    """Retrieve memory context for a query.

    Wraps ``memory_query.recall()`` so any MCP-capable agent gets the same
    retrieval quality as the Deus host hook.

    Args:
        query:  Natural-language query (e.g. "what is Liam's timezone?").
        k:      Number of top results to return.
        source: Identifier written to the retrieval log (default ``"mcp"``).

    Returns:
        ``{"context": str, "paths": [str], "confidence": float, "fell_back": bool}``
    """
    return memory_query.recall(query, k=k, source=source)


def _run_mcp_server() -> None:
    """Start the FastMCP stdio server."""
    if not _MCP_AVAILABLE:
        print(
            "ERROR: mcp package not installed. Run: pip install mcp",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp = FastMCP("deus-memory")
    mcp.tool()(memory_recall)
    mcp.run()


if __name__ == "__main__":
    _run_mcp_server()
