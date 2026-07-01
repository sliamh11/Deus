#!/usr/bin/env python3
"""Deus Memory MCP Server (stdio transport).

Exposes the memory recall pipeline as a single MCP tool so any agent that can
register an MCP server (Claude Code, Cursor, Windsurf, etc.) gets the same
retrieval quality as the host hook — closing the cross-interface parity gap.

Platform: Linux/macOS only (depends on sqlite_vec C extension + Ollama).

Usage:
    scripts/deus-memory-mcp   # stdio; selects a Python env with mcp installed

Register with Codex:
    codex mcp add deus-memory -- /path/to/deus/scripts/deus-memory-mcp

Register in ~/.claude/settings.json:
    {
      "mcpServers": {
        "deus-memory": {
          "command": "/path/to/deus/scripts/deus-memory-mcp",
          "args": [],
          "env": {}
        }
      }
    }
"""
from __future__ import annotations

import os
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


def _int_env(name: str, default: int) -> int:
    """Positive-int env override with a safe fallback (invalid/<=0 -> default)."""
    try:
        v = int(os.environ.get(name, ""))  # LIA-344
    except ValueError:
        return default
    return v if v > 0 else default


# LIA-344: bound the MCP recall payload server-side (the host hook caps at 4096,
# but the MCP path returned recall()'s context uncapped with no k ceiling).
# 8192 chars (~2k tok) exceeds the largest real procedure node (2,962) and a
# typical k=3 body (~5 KB) while bounding the k=10 worst case (~57 KB). Both
# env-overridable starting values, tunable post-ship on real MCP traffic.
_MAX_CONTEXT_CHARS = _int_env("DEUS_MCP_RECALL_MAX_CHARS", 8192)  # LIA-344
_K_MAX = _int_env("DEUS_MCP_RECALL_MAX_K", 10)  # LIA-344


def memory_recall(query: str, k: int = 3, source: str = "mcp") -> dict:
    """Retrieve memory context for a query.

    Wraps ``memory_query.recall()`` so any MCP-capable agent gets the same
    retrieval quality as the Deus host hook.

    Args:
        query:  Natural-language query (e.g. "what is Liam's timezone?").
        k:      Number of top results to return (LIA-344: clamped server-side to
                1.._K_MAX, default ceiling 10).
        source: Identifier written to the retrieval log (default ``"mcp"``).

    The formatted context is capped server-side to ``_MAX_CONTEXT_CHARS``
    (LIA-344) so a caller cannot pull an unbounded payload.

    Returns:
        ``{"context": str, "paths": [str], "confidence": float, "fell_back": bool}``
    """
    # Procedures recall by default on the MCP path (the broad external recall
    # surface). Kill-switch is an explicit DEUS_PROCEDURE_MEMORY=0; any other value
    # (incl. unset) keeps them eligible via {"standard"} (None falls through to
    # recall()'s default which ALSO drops procedures). Intentionally diverges from
    # the default-off host hook — see docs/decisions/procedure-memory-default-on.md.
    proc_disabled = os.environ.get("DEUS_PROCEDURE_MEMORY", "").strip() == "0"
    exclude_kinds = None if proc_disabled else {"standard"}
    # LIA-344: clamp k and cap the formatted context so an MCP caller cannot pull
    # an unbounded payload (the sentinel framing survives — see _truncate_body).
    k = min(max(1, k), _K_MAX)
    return memory_query.recall(
        query,
        k=k,
        source=source,
        exclude_kinds=exclude_kinds,
        max_context_chars=_MAX_CONTEXT_CHARS,
    )


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
