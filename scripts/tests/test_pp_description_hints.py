"""
Tests for drift_check.check_mcp_description_hints.

The sibling check warns when a server.tool() block whose schema accepts both
compact AND select fails to mention 'select', 'compact', or 'payload' in its
description string. Tests use a synthesized packages/ tree under tmp_path so
the real codebase isn't read.
"""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

# Make scripts/ importable so `import drift_check` resolves regardless of CWD.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import drift_check


def _make_pkg(root: Path, pkg_name: str, tool_src: str) -> None:
    """Create packages/<pkg_name>/src/index.ts with the given content."""
    src = root / "packages" / pkg_name / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "index.ts").write_text(tool_src)


# Tool whose description correctly mentions select/compact — no violation.
_HINTED_TOOL = """\
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';

server.tool(
  'list_events',
  'List upcoming calendar events. Pass select="id,start" + compact=true to cut payload.',
  {
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => mcpResponse(args, { compact: args.compact, select: args.select }),
);
"""

# Tool whose schema accepts compact/select but description lacks the hint — should warn.
_UNHINTED_TOOL = """\
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';

server.tool(
  'list_events',
  'List upcoming calendar events',
  {
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => mcpResponse(args, { compact: args.compact, select: args.select }),
);
"""

# Action-only tool: schema has neither compact nor select — must be skipped.
_ACTION_ONLY_TOOL = """\
import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';

server.tool(
  'delete_event',
  'Delete a calendar event',
  { event_id: z.string() },
  async (args) => ({ content: [{ type: 'text', text: 'OK' }] }),
);
"""


class TestCheckMcpDescriptionHints:
    def test_no_violation_when_hint_present(self, tmp_path: Path) -> None:
        _make_pkg(tmp_path, "mcp-hinted", _HINTED_TOOL)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_mcp_description_hints(tmp_path)
        assert rc == 0
        assert "missing description hints" not in buf.getvalue()
        assert "All projection-capable MCP tools" in buf.getvalue()

    def test_hint_missing_is_blocked(self, tmp_path: Path) -> None:
        _make_pkg(tmp_path, "mcp-unhinted", _UNHINTED_TOOL)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_mcp_description_hints(tmp_path)
        # Blocking — non-zero on any violation.
        assert rc == 1
        out = buf.getvalue()
        assert "missing description hints (1)" in out
        assert "mcp-unhinted/src/index.ts" in out

    def test_skips_action_only_tool(self, tmp_path: Path) -> None:
        """Tools without compact+select in schema are out of scope for this check."""
        _make_pkg(tmp_path, "mcp-action", _ACTION_ONLY_TOOL)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_mcp_description_hints(tmp_path)
        assert rc == 0
        assert "missing description hints" not in buf.getvalue()

    def test_returns_zero_when_no_packages_dir(self, tmp_path: Path) -> None:
        # tmp_path has no packages/ subdirectory at all.
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_mcp_description_hints(tmp_path)
        assert rc == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
