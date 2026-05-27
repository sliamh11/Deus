#!/usr/bin/env python3
"""Code Search MCP Server (stdio transport).

Exposes semantic code search as MCP tools so any MCP-capable agent gets
the same retrieval quality. Follows the memory_mcp_server.py pattern.

Platform: Linux/macOS only (depends on sqlite_vec + Ollama).

Register in ~/.claude.json:
    {
      "mcpServers": {
        "code-search": {
          "command": "python3",
          "args": ["/path/to/deus/scripts/code_search_mcp.py"],
          "env": {}
        }
      }
    }
"""
from __future__ import annotations

import sys

if sys.platform == "win32":
    print(
        "code_search_mcp.py requires Linux or macOS (sqlite_vec + Ollama).",
        file=sys.stderr,
    )
    sys.exit(1)

from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import code_search  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


def _run_mcp_server() -> None:
    if not _MCP_AVAILABLE:
        print(
            "ERROR: mcp package not installed. Run: pip install mcp",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp = FastMCP("code-search")

    @mcp.tool()
    def search_code(query: str, k: int = 10, min_confidence: float = 0.0) -> str:
        """Search indexed code semantically. Returns ranked code chunks matching the query.

        Each result includes two confidence scores:
        - confidence (0-1): per-result ranking quality (RRF score normalized).
        - retrieval_confidence (0-1): query-level signal indicating how well the
          query matches the indexed codebase. Below 0.3 = likely out-of-domain.
          This value is identical across all results for the same query.

        Args:
            query: Natural-language description of what you're looking for
                   (e.g. "retry logic for failed API calls").
            k: Number of results to return (default 10).
            min_confidence: Filter results below this confidence (default 0.0 = no filter).
        """
        import json
        results = code_search.search(query, k=k, min_confidence=min_confidence)
        if not results:
            return "No results found. The index may be empty — run reindex first."
        return json.dumps(results, indent=2)

    @mcp.tool()
    def reindex(directory: str = ".") -> str:
        """Index or re-index a codebase for semantic search.

        Args:
            directory: Path to the codebase root (default: current directory).
        """
        import json
        result = code_search.reindex(directory)
        return json.dumps(result, indent=2)

    @mcp.tool()
    def index_status() -> str:
        """Show the current state of the code search index."""
        import json
        return json.dumps(code_search.status(), indent=2)

    mcp.run(transport="stdio")


if __name__ == "__main__":
    _run_mcp_server()
