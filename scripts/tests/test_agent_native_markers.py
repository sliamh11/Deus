"""
Tests for drift_check.check_agent_native_mcp action-marker classification.

Specifically verifies:
  1. The widened 30-line scan window catches action markers in deep handler
     bodies (where the prior 8-line window false-positived on
     server-base.ts send_message/connect/disconnect).
  2. The four mcp-x engagement markers ('Liked.', 'Like removed.',
     'Retweeted.', 'Retweet removed.') correctly classify those tools as
     action-only.
"""
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest

# Ensure scripts/ is importable.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import drift_check


def _make_pkg(root: Path, pkg_name: str, tool_src: str) -> None:
    src = root / "packages" / pkg_name / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "index.ts").write_text(tool_src)


# Action-only tool with marker DEEP in the handler body (line ~14 from
# server.tool decl) — pre-widening this would false-positive.
_DEEP_ACTION_TOOL = """\
import { z } from 'zod';

server.tool(
  'connect',
  'Connect to the platform',
  {},
  async () => {
    try {
      // simulate some work
      await provider.connect();
      // more setup
      const status = provider.getStatus();
      // assertions
      if (!status.connected) {
        throw new Error('not connected');
      }
    } catch (err) {
      return { content: [{ type: 'text', text: 'error' }], isError: true };
    }
    return { content: [{ type: 'text', text: 'Connected.' }] };
  },
);
"""

# Action-only tool returning 'Liked.' — new marker in this PR.
_LIKE_TOOL = """\
import { z } from 'zod';

server.tool(
  'like_tweet',
  'Like a tweet',
  { tweet_id: z.string() },
  async (args) => {
    await x.likeTweet(args.tweet_id);
    return { content: [{ type: 'text', text: 'Liked.' }] };
  },
);
"""

# Action-only tool returning 'Retweeted.' — new marker in this PR.
_RETWEET_TOOL = """\
import { z } from 'zod';

server.tool(
  'retweet',
  'Retweet a tweet',
  { tweet_id: z.string() },
  async (args) => {
    await x.retweet(args.tweet_id);
    return { content: [{ type: 'text', text: 'Retweeted.' }] };
  },
);
"""

# Action-only tool returning 'Like removed.' — new marker in this PR.
_UNLIKE_TOOL = """\
import { z } from 'zod';

server.tool(
  'unlike_tweet',
  'Remove a like from a tweet',
  { tweet_id: z.string() },
  async (args) => {
    await x.unlikeTweet(args.tweet_id);
    return { content: [{ type: 'text', text: 'Like removed.' }] };
  },
);
"""

# Action-only tool returning 'Retweet removed.' — new marker in this PR.
_UNDO_RETWEET_TOOL = """\
import { z } from 'zod';

server.tool(
  'undo_retweet',
  'Undo a retweet',
  { tweet_id: z.string() },
  async (args) => {
    await x.undoRetweet(args.tweet_id);
    return { content: [{ type: 'text', text: 'Retweet removed.' }] };
  },
);
"""

# Data-returning tool with NO compact/select and NO action marker — should warn.
_UNMIGRATED_TOOL = """\
import { z } from 'zod';

server.tool(
  'get_data',
  'Fetch some data',
  { id: z.string() },
  async (args) => {
    const data = await provider.fetch(args.id);
    return { content: [{ type: 'text', text: JSON.stringify(data) }] };
  },
);
"""

# Already-migrated tool with compact + select in schema — should pass cleanly.
_MIGRATED_TOOL = """\
import { z } from 'zod';

server.tool(
  'list_items',
  'List items',
  {
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => {
    const items = await provider.list();
    return mcpResponse(items, { compact: args.compact, select: args.select });
  },
);
"""


class TestActionMarkerScanWindow:
    def test_deep_action_marker_skipped(self, tmp_path: Path) -> None:
        """Action marker ~14 lines below server.tool() decl is correctly skipped."""
        _make_pkg(tmp_path, "mcp-deep", _DEEP_ACTION_TOOL)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_agent_native_mcp(tmp_path)
        assert rc == 0
        assert "missing agent-native params" not in buf.getvalue()
        assert "All MCP tool registrations" in buf.getvalue()

    def test_liked_marker_skipped(self, tmp_path: Path) -> None:
        """`'Liked.'` is a recognized action marker (mcp-x engagement tools)."""
        _make_pkg(tmp_path, "mcp-like", _LIKE_TOOL)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_agent_native_mcp(tmp_path)
        assert rc == 0
        assert "missing agent-native params" not in buf.getvalue()

    def test_retweeted_marker_skipped(self, tmp_path: Path) -> None:
        """`'Retweeted.'` is a recognized action marker."""
        _make_pkg(tmp_path, "mcp-rt", _RETWEET_TOOL)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_agent_native_mcp(tmp_path)
        assert rc == 0
        assert "missing agent-native params" not in buf.getvalue()

    def test_like_removed_marker_skipped(self, tmp_path: Path) -> None:
        """`'Like removed.'` is a recognized action marker (mcp-x unlike_tweet)."""
        _make_pkg(tmp_path, "mcp-unlike", _UNLIKE_TOOL)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_agent_native_mcp(tmp_path)
        assert rc == 0
        assert "missing agent-native params" not in buf.getvalue()

    def test_retweet_removed_marker_skipped(self, tmp_path: Path) -> None:
        """`'Retweet removed.'` is a recognized action marker (mcp-x undo_retweet)."""
        _make_pkg(tmp_path, "mcp-undo-rt", _UNDO_RETWEET_TOOL)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_agent_native_mcp(tmp_path)
        assert rc == 0
        assert "missing agent-native params" not in buf.getvalue()

    def test_unmigrated_tool_is_blocked(self, tmp_path: Path) -> None:
        """Data-returning tool without compact/select or action marker is flagged AND blocks."""
        _make_pkg(tmp_path, "mcp-unmig", _UNMIGRATED_TOOL)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_agent_native_mcp(tmp_path)
        # Blocking — non-zero on any violation.
        assert rc == 1
        out = buf.getvalue()
        assert "missing agent-native params (1)" in out
        assert "mcp-unmig/src/index.ts" in out

    def test_migrated_tool_passes(self, tmp_path: Path) -> None:
        """Tool with compact + select in schema is recognized as migrated."""
        _make_pkg(tmp_path, "mcp-mig", _MIGRATED_TOOL)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_agent_native_mcp(tmp_path)
        assert rc == 0
        assert "missing agent-native params" not in buf.getvalue()

    def test_marker_near_window_boundary(self, tmp_path: Path) -> None:
        """Action marker ~20 lines below decl is comfortably inside 30-line window."""
        # 20 lines of filler between server.tool() and the return statement.
        # The action-marker scan window is 30 lines; this marker lands at ~25.
        filler = "\n".join(f"      // filler line {n}" for n in range(20))
        boundary_src = f"""\
import {{ z }} from 'zod';

server.tool(
  'big_action',
  'A tool with a deep handler body',
  {{}},
  async () => {{
{filler}
      return {{ content: [{{ type: 'text', text: 'OK' }}] }};
  }},
);
"""
        _make_pkg(tmp_path, "mcp-deep-boundary", boundary_src)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_agent_native_mcp(tmp_path)
        assert rc == 0
        assert "missing agent-native params" not in buf.getvalue()


class TestSchemaMatchTightening:
    """Verify the `compact:` / `select:` match (with colon) catches a regression
    where a tool's DESCRIPTION mentions the hint but the SCHEMA lacks the params.
    """

    def test_hint_in_description_alone_does_not_pass(self, tmp_path: Path) -> None:
        # Description references the params but schema omits them — pre-tightening
        # the bare "compact"/"select" substring match would let this slip through.
        sneaky_src = """\
import { z } from 'zod';

server.tool(
  'sneaky',
  'Returns data. Pass select="id" + compact=true to slim payload.',
  { id: z.string() },
  async (args) => {
    const data = await provider.fetch(args.id);
    return { content: [{ type: 'text', text: JSON.stringify(data) }] };
  },
);
"""
        _make_pkg(tmp_path, "mcp-sneaky", sneaky_src)
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = drift_check.check_agent_native_mcp(tmp_path)
        assert rc == 1  # blocking — gate active
        out = buf.getvalue()
        # With the tightened `compact:` / `select:` match, this is now flagged.
        assert "missing agent-native params (1)" in out
        assert "mcp-sneaky/src/index.ts" in out


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
