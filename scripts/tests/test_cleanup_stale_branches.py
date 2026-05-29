"""
Tests for scripts/cleanup_stale_branches.py.

Mocks subprocess calls so no real git repo is needed.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure scripts/ is importable
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import cleanup_stale_branches as csb
from _exit_codes import NOT_FOUND, SUCCESS


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_run_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> tuple:
    return (returncode, stdout, stderr)


def _worktree_porcelain(*branches: str) -> str:
    """Build porcelain output for git worktree list with named branches."""
    lines = []
    for b in branches:
        lines.append("worktree /some/path")
        lines.append("HEAD abc123")
        lines.append(f"branch refs/heads/{b}")
        lines.append("")
    return "\n".join(lines)


# ── get_current_branch ────────────────────────────────────────────────────────

class TestGetCurrentBranch:
    def test_returns_branch_name(self):
        with patch.object(csb, "_run", return_value=(0, "feat/foo\n", "")):
            assert csb.get_current_branch() == "feat/foo"

    def test_returns_none_for_detached_head(self):
        with patch.object(csb, "_run", return_value=(0, "HEAD\n", "")):
            assert csb.get_current_branch() is None

    def test_returns_none_on_git_failure(self):
        with patch.object(csb, "_run", return_value=(128, "", "fatal: not a git repo")):
            assert csb.get_current_branch() is None


# ── get_worktree_branches ─────────────────────────────────────────────────────

class TestGetWorktreeBranches:
    def test_parses_single_branch(self):
        porcelain = _worktree_porcelain("feat/some-feature")
        with patch.object(csb, "_run", return_value=(0, porcelain, "")):
            assert csb.get_worktree_branches() == {"feat/some-feature"}

    def test_parses_multiple_branches(self):
        porcelain = _worktree_porcelain("feat/a", "feat/b")
        with patch.object(csb, "_run", return_value=(0, porcelain, "")):
            assert csb.get_worktree_branches() == {"feat/a", "feat/b"}

    def test_ignores_bare_worktree_no_branch_line(self):
        # Detached HEAD worktree has no branch line
        porcelain = "worktree /some/path\nHEAD abc123\ndetached\n\n"
        with patch.object(csb, "_run", return_value=(0, porcelain, "")):
            assert csb.get_worktree_branches() == set()

    def test_returns_empty_on_git_failure(self):
        with patch.object(csb, "_run", return_value=(1, "", "error")):
            assert csb.get_worktree_branches() == set()


# ── is_squash_merged ──────────────────────────────────────────────────────────

class TestIsSquashMerged:
    def test_all_minus_lines_is_stale(self):
        cherry_output = "- abc123 First commit\n- def456 Second commit\n"
        with patch.object(csb, "_run", return_value=(0, cherry_output, "")):
            assert csb.is_squash_merged("feat/done") is True

    def test_any_plus_line_is_not_stale(self):
        cherry_output = "- abc123 Already merged\n+ def456 Not merged yet\n"
        with patch.object(csb, "_run", return_value=(0, cherry_output, "")):
            assert csb.is_squash_merged("feat/wip") is False

    def test_all_plus_lines_is_not_stale(self):
        cherry_output = "+ abc123 Unmerged\n+ def456 Also unmerged\n"
        with patch.object(csb, "_run", return_value=(0, cherry_output, "")):
            assert csb.is_squash_merged("feat/active") is False

    def test_empty_output_is_stale(self):
        """Empty cherry output means no commits diverge from main — branch is stale."""
        with patch.object(csb, "_run", return_value=(0, "", "")):
            assert csb.is_squash_merged("feat/empty") is True

    def test_whitespace_only_output_is_stale(self):
        with patch.object(csb, "_run", return_value=(0, "   \n  \n", "")):
            assert csb.is_squash_merged("feat/ws") is True

    def test_git_error_returns_none(self):
        with patch.object(csb, "_run", return_value=(128, "", "fatal: unknown revision")):
            assert csb.is_squash_merged("feat/unknown") is None


# ── build_skip_set ────────────────────────────────────────────────────────────

class TestBuildSkipSet:
    def test_always_protected_included(self):
        skip = csb.build_skip_set(None, set(), set())
        assert "main" in skip
        assert skip["main"] == "protected"

    def test_current_branch_marked_current(self):
        skip = csb.build_skip_set("feat/cur", set(), set())
        assert skip.get("feat/cur") == "current"

    def test_worktree_branch_marked_worktree(self):
        skip = csb.build_skip_set(None, {"feat/wt"}, set())
        assert skip.get("feat/wt") == "worktree"

    def test_extra_protected_included(self):
        skip = csb.build_skip_set(None, set(), {"feat/keep"})
        assert skip.get("feat/keep") == "protected"

    def test_current_branch_takes_priority_over_worktree(self):
        # The current branch is also a worktree entry — should be "current" not "worktree"
        skip = csb.build_skip_set("feat/cur", {"feat/cur"}, set())
        assert skip.get("feat/cur") == "current"


# ── run() integration ─────────────────────────────────────────────────────────

class TestRun:
    """Integration tests that patch _run, list_local_branches, etc."""

    def _setup_mocks(
        self,
        monkeypatch,
        branches: list[str],
        cherry_map: dict[str, str],  # branch -> cherry stdout
        current: str | None = "main",
        worktrees: set[str] | None = None,
    ):
        """Patch the helpers used by run()."""
        if worktrees is None:
            worktrees = set()

        monkeypatch.setattr(csb, "get_current_branch", lambda: current)
        monkeypatch.setattr(csb, "get_worktree_branches", lambda: worktrees)
        monkeypatch.setattr(csb, "list_local_branches", lambda: branches)

        def fake_is_squash_merged(branch: str, base_branch: str = "main") -> bool | None:
            output = cherry_map.get(branch, "")
            lines = [l.strip() for l in output.splitlines() if l.strip()]
            if not lines:
                return True
            return all(l.startswith("-") for l in lines)

        monkeypatch.setattr(csb, "is_squash_merged", fake_is_squash_merged)

    def test_stale_branch_detected(self, monkeypatch):
        self._setup_mocks(
            monkeypatch,
            branches=["main", "feat/done"],
            cherry_map={"feat/done": "- abc First\n"},
        )
        exit_code = csb.run(delete=False)
        assert exit_code == SUCCESS

    def test_no_stale_branches_returns_not_found(self, monkeypatch):
        self._setup_mocks(
            monkeypatch,
            branches=["main", "feat/active"],
            cherry_map={"feat/active": "+ abc WIP\n"},
        )
        exit_code = csb.run(delete=False)
        assert exit_code == NOT_FOUND

    def test_protected_branch_skipped(self, monkeypatch):
        self._setup_mocks(
            monkeypatch,
            branches=["main", "feat/done"],
            cherry_map={"feat/done": "- abc\n"},
        )

        deleted_calls: list[str] = []
        monkeypatch.setattr(
            csb, "delete_branch", lambda b: (deleted_calls.append(b), (True, ""))[1]
        )

        # main is always protected; feat/done is stale but not protected
        exit_code = csb.run(delete=True)
        assert exit_code == SUCCESS
        assert "main" not in deleted_calls

    def test_protect_flag_prevents_deletion(self, monkeypatch):
        self._setup_mocks(
            monkeypatch,
            branches=["main", "feat/keep", "feat/purge"],
            cherry_map={
                "feat/keep": "- abc\n",
                "feat/purge": "- def\n",
            },
        )

        deleted_calls: list[str] = []
        monkeypatch.setattr(
            csb,
            "delete_branch",
            lambda b: (deleted_calls.append(b), (True, ""))[1],
        )

        exit_code = csb.run(delete=True, extra_protected={"feat/keep"})
        assert exit_code == SUCCESS
        assert "feat/keep" not in deleted_calls
        assert "feat/purge" in deleted_calls

    def test_delete_calls_delete_branch(self, monkeypatch):
        self._setup_mocks(
            monkeypatch,
            branches=["main", "feat/stale"],
            cherry_map={"feat/stale": "- abc\n"},
        )

        deleted_calls: list[str] = []
        monkeypatch.setattr(
            csb,
            "delete_branch",
            lambda b: (deleted_calls.append(b), (True, ""))[1],
        )

        csb.run(delete=True)
        assert "feat/stale" in deleted_calls

    def test_dry_run_does_not_call_delete_branch(self, monkeypatch):
        self._setup_mocks(
            monkeypatch,
            branches=["main", "feat/stale"],
            cherry_map={"feat/stale": "- abc\n"},
        )

        delete_branch_called = []
        monkeypatch.setattr(
            csb,
            "delete_branch",
            lambda b: delete_branch_called.append(b) or (True, ""),
        )

        csb.run(delete=False)
        assert delete_branch_called == []

    def test_worktree_branch_skipped(self, monkeypatch):
        self._setup_mocks(
            monkeypatch,
            branches=["main", "feat/in-worktree", "feat/stale"],
            cherry_map={
                "feat/in-worktree": "- abc\n",
                "feat/stale": "- def\n",
            },
            worktrees={"feat/in-worktree"},
        )

        deleted_calls: list[str] = []
        monkeypatch.setattr(
            csb,
            "delete_branch",
            lambda b: (deleted_calls.append(b), (True, ""))[1],
        )

        exit_code = csb.run(delete=True)
        assert exit_code == SUCCESS
        assert "feat/in-worktree" not in deleted_calls
        assert "feat/stale" in deleted_calls

    def test_json_output_structure(self, monkeypatch, capsys):
        self._setup_mocks(
            monkeypatch,
            branches=["main", "feat/stale", "feat/active"],
            cherry_map={
                "feat/stale": "- abc\n",
                "feat/active": "+ def\n",
            },
        )

        monkeypatch.setattr(csb, "delete_branch", lambda b: (True, ""))

        csb.run(delete=True, use_json=True)

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "stale" in data
        assert "deleted" in data
        assert "skipped" in data
        assert "feat/stale" in data["stale"]
        assert "feat/stale" in data["deleted"]
        # feat/active should appear in skipped with reason "unmerged"
        unmerged_skipped = [s for s in data["skipped"] if s["branch"] == "feat/active"]
        assert len(unmerged_skipped) == 1
        assert unmerged_skipped[0]["reason"] == "unmerged"

    def test_json_no_delete_mode(self, monkeypatch, capsys):
        self._setup_mocks(
            monkeypatch,
            branches=["main", "feat/stale"],
            cherry_map={"feat/stale": "- abc\n"},
        )

        csb.run(delete=False, use_json=True)

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["stale"] == ["feat/stale"]
        assert data["deleted"] == []
