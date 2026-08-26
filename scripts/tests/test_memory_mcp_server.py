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
        # Pin the scope so this exact-args assertion does not depend on the
        # runner's cwd: DEUS_PROJECT_ROOT is this repo, so _resolve_scope()
        # returns auto_memory_dir.DEUS_PROJECT_ID ("deus") through the
        # inode-identity check, not through a basename literal.
        # DEUS_PROJECT_SCOPE must be ON: scoping is gated on the same flag as
        # the host hook, so both surfaces flip together (see memory_recall).
        monkeypatch.setenv("DEUS_PROJECT_ROOT", str(_ROOT))
        monkeypatch.setenv("DEUS_PROJECT_SCOPE", "1")
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            result = mms.memory_recall("what timezone?", k=5, source="test")

        mock_recall.assert_called_once_with(
            "what timezone?",
            k=5,
            source="test",
            exclude_kinds={"standard"},
            max_context_chars=mms._MAX_CONTEXT_CHARS,
            project_scope="deus",
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


class TestResolveScope:
    """LIA-123: the MCP tool's project scoping, tested directly.

    `_resolve_scope` carries a four-way precedence, worktree unwinding and a
    this-repo identity override, and it was previously exercised only
    incidentally through one args assertion. The fail-closed branch in
    particular had ZERO coverage, which is how a fail-closed becomes a
    fail-open on a later refactor with nothing to catch it.
    """

    @pytest.fixture(autouse=True)
    def _no_ambient_scope(self, monkeypatch):
        monkeypatch.delenv("DEUS_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)

    def test_explicit_all_is_unscoped_and_says_so(self):
        scope, label = mms._resolve_scope("all")
        assert scope is None
        assert "explicitly requested" in label

    def test_explicit_path_resolves_to_that_project(self, tmp_path):
        proj = tmp_path / "widget-co"
        proj.mkdir()
        scope, label = mms._resolve_scope(str(proj))
        assert scope == label
        assert scope.endswith("-widget-co")

    def test_explicit_id_is_taken_verbatim_so_quarantine_stays_reachable(self):
        """A quarantined project's tag is its raw directory name and matches no
        live scope. Passing it by name is the escape hatch that makes
        quarantine recoverable rather than a slow delete."""
        raw = "-some-historical-path-that-no-longer-exists"
        scope, label = mms._resolve_scope(raw)
        assert scope == raw

    def test_this_repo_resolves_to_the_deus_sentinel(self):
        import auto_memory_dir as amd

        scope, _label = mms._resolve_scope(str(_ROOT))
        assert scope == amd.DEUS_PROJECT_ID

    def test_deus_project_root_wins_over_claude_project_dir(self, tmp_path, monkeypatch):
        """Precedence must be REAL, not just documented. An earlier version
        called os.environ.setdefault and then read CLAUDE_PROJECT_DIR, so with
        both set it returned the wrong project under a DEUS_PROJECT_ROOT
        label -- a wrong scope wearing a correct-looking name."""
        preferred = tmp_path / "preferred-repo"
        other = tmp_path / "other-repo"
        preferred.mkdir()
        other.mkdir()
        monkeypatch.setenv("DEUS_PROJECT_ROOT", str(preferred))
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))

        scope, label = mms._resolve_scope(None)
        assert scope.endswith("-preferred-repo"), scope
        assert "DEUS_PROJECT_ROOT" in label
        assert "other-repo" not in scope

    def test_cwd_fallback_resolves_when_no_env_var_is_set(self, tmp_path, monkeypatch):
        """The fourth precedence branch. Not disclosure-relevant on its own --
        it returns a real scope, not None -- but it was the one arm of the
        four-way precedence with no coverage."""
        import subprocess

        repo = tmp_path / "cwd-repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        monkeypatch.chdir(repo)

        scope, label = mms._resolve_scope(None)
        assert scope is not None
        assert scope.endswith("-cwd-repo"), scope
        assert "from server cwd" in label


class TestMemoryRecallFailsClosed:
    """The security-relevant branch: an indeterminate scope must NOT become
    'search every project'.

    This server is registered as a plain stdio command with an empty env and no
    pinned cwd, so a client setting neither variable would otherwise pull every
    project's memory into every answer.
    """

    def test_guard_refuses_a_none_scope_and_never_calls_recall(self, monkeypatch):
        """`memory_recall`'s OWN composition logic, in isolation.

        `_resolve_scope` is stubbed deliberately here: this asserts the guard
        (`scope is None and project != "all"`) short-circuits, which is code
        distinct from `_resolve_scope`'s internals. The end-to-end case below
        is what proves the two pieces compose under real conditions -- both are
        needed, and neither substitutes for the other.
        """
        monkeypatch.setenv("DEUS_PROJECT_SCOPE", "1")
        monkeypatch.setattr(
            mms, "_resolve_scope", lambda project: (None, "unscoped - project indeterminate")
        )

        with patch.object(mq, "recall") as mock_recall:
            result = mms.memory_recall("what timezone?")

        mock_recall.assert_not_called()
        assert result["paths"] == []
        assert result["context"] == ""
        assert result["fell_back"] is True
        assert "indeterminate" in result["scope"]
        assert "project=" in result["error"]

    def test_end_to_end_refusal_under_a_genuinely_indeterminate_environment(
        self, monkeypatch, tmp_path
    ):
        """The real condition, with NOTHING stubbed but `recall` itself.

        Both env vars unset and a cwd outside any git repo, so `_resolve_scope`
        produces `None` naturally rather than being told to. Without this, the
        guard is only ever verified against a synthetic `None` and nothing
        proves the resolver and the guard actually meet.
        """
        monkeypatch.delenv("DEUS_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.setenv("DEUS_PROJECT_SCOPE", "1")
        non_repo = tmp_path / "not-a-repo"
        non_repo.mkdir()
        monkeypatch.chdir(non_repo)

        # Guard the premise: if this tmp dir were somehow inside a repo, the
        # test would pass for the wrong reason.
        import auto_memory_dir as amd

        assert amd._git_output(["rev-parse", "--show-toplevel"], non_repo) is None

        with patch.object(mq, "recall") as mock_recall:
            result = mms.memory_recall("what timezone?")

        mock_recall.assert_not_called()
        assert result["fell_back"] is True
        assert "indeterminate" in result["scope"]

    def test_explicit_all_is_allowed_through_unscoped(self, monkeypatch):
        """`project='all'` is the DELIBERATE unscoped path and must still run -
        the refusal above must not swallow it."""
        monkeypatch.delenv("DEUS_PROJECT_ROOT", raising=False)
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        monkeypatch.setenv("DEUS_PROJECT_SCOPE", "1")

        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            result = mms.memory_recall("what timezone?", project="all")

        assert mock_recall.called
        assert mock_recall.call_args.kwargs["project_scope"] is None
        assert result["paths"] == FAKE_RECALL_RESULT["paths"]


class TestScopingIsGatedOnTheSameFlagAsTheHook:
    """LIA-122/123: MCP and the host hook must flip together.

    Scoping this path unconditionally while `memory_retrieval_hook.py` stayed
    opt-in was a real defect. With the live store still holding `deus`-tagged
    nodes, a non-Deus session asking through MCP would scope to its own project
    and filter out every host-global procedure -- the exact regression the
    "retag first, flag second" ordering exists to prevent, on the surface an
    agent reaches for most.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        monkeypatch.delenv("DEUS_PROJECT_SCOPE", raising=False)
        monkeypatch.setenv("DEUS_PROJECT_ROOT", str(_ROOT))

    def test_flag_off_does_not_scope(self, monkeypatch):
        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            result = mms.memory_recall("what timezone?")

        assert mock_recall.call_args.kwargs["project_scope"] is None
        assert "DEUS_PROJECT_SCOPE is off" in result["scope"]

    def test_flag_on_scopes(self, monkeypatch):
        monkeypatch.setenv("DEUS_PROJECT_SCOPE", "1")
        import auto_memory_dir as amd

        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            mms.memory_recall("what timezone?")

        assert mock_recall.call_args.kwargs["project_scope"] == amd.DEUS_PROJECT_ID

    def test_explicit_project_scopes_even_with_the_flag_off(self, monkeypatch, tmp_path):
        """An explicit argument is the caller asking for a specific project, and
        is also what keeps a QUARANTINED project reachable by name. It must not
        be suppressed by a flag aimed at the ambient default."""
        raw = "-some-historical-path-that-no-longer-exists"

        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            mms.memory_recall("what timezone?", project=raw)

        assert mock_recall.call_args.kwargs["project_scope"] == raw

    def test_flag_off_never_refuses(self, monkeypatch):
        """The fail-closed refusal belongs to the scoped mode. With scoping off,
        an unscoped result is the CORRECT answer, not an indeterminate one."""
        monkeypatch.delenv("DEUS_PROJECT_ROOT", raising=False)

        with patch.object(mq, "recall", return_value=FAKE_RECALL_RESULT) as mock_recall:
            result = mms.memory_recall("what timezone?")

        assert mock_recall.called
        assert "error" not in result
