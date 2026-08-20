"""Tests for the shared auto-memory dir resolver (LIA-341).

Frozen expected values: each scenario asserts a concrete resolved path so an
incorrectly-encoded directory key can't pass silently.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import auto_memory_dir as amd  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    # Detach from the real environment + home so derived/fallback paths are
    # deterministic under tmp_path.
    monkeypatch.delenv("DEUS_AUTO_MEMORY_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return tmp_path


def test_env_override_expands_tilde(monkeypatch, tmp_path):
    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", "~/explicit")
    assert amd.resolve_auto_memory_dir() == tmp_path / "explicit"


def test_env_override_absolute_wins(monkeypatch, tmp_path):
    target = tmp_path / "explicit-abs"
    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", str(target))
    assert amd.resolve_auto_memory_dir() == target


def test_project_dir_derived_frozen_key(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/foo/bar")
    target = tmp_path / ".claude" / "projects" / "-foo-bar" / "memory"
    target.mkdir(parents=True)
    assert amd.resolve_auto_memory_dir() == target


def test_project_dir_derived_skipped_when_dir_absent(monkeypatch, tmp_path):
    # CLAUDE_PROJECT_DIR set but its memory dir does not exist -> fall through
    # to the ~/.deus/auto-memory fallback (the repo-derived dir won't exist
    # under the tmp HOME either).
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/foo/bar")
    assert amd.resolve_auto_memory_dir() == tmp_path / ".deus" / "auto-memory"


def test_windows_backslash_encoding(monkeypatch, tmp_path):
    win_proj = r"C:\Users\x"
    encoded = amd._encode_project_dir(win_proj)
    assert "\\" not in encoded  # no raw backslash survives the encoding
    assert encoded == "-C:-Users-x"
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", win_proj)
    target = tmp_path / ".claude" / "projects" / encoded / "memory"
    target.mkdir(parents=True)
    assert amd.resolve_auto_memory_dir() == target


def test_fallback_when_nothing_resolves(tmp_path):
    assert amd.resolve_auto_memory_dir() == tmp_path / ".deus" / "auto-memory"


def test_standards_pack_delegates_to_resolver(monkeypatch, tmp_path):
    import standards_pack

    target = tmp_path / "deleg"
    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", str(target))
    assert standards_pack._default_auto_mem_dir() == amd.resolve_auto_memory_dir()
    assert standards_pack._default_auto_mem_dir() == target


# ── P3: project-id resolution (worktree-normalized) ──────────────────────────


class TestUnwindWorktree:
    """Hermetic: `_git_output` is mocked, so no real git is invoked. Mirrors
    test_drift_check.py's TestWorktreeAutoBase pattern for the same
    `--git-common-dir` mechanism."""

    @staticmethod
    def _patch_git(monkeypatch, responses):
        # responses: {(cmd tuple): output_str_or_None}
        monkeypatch.setattr(
            amd, "_git_output",
            lambda cmd, cwd: responses.get(tuple(cmd)),
        )

    def test_normal_checkout_returns_start(self, monkeypatch, tmp_path):
        # Main checkout: git-common-dir is a bare ".git" relative to start.
        self._patch_git(monkeypatch, {("rev-parse", "--git-common-dir"): ".git"})
        assert amd._unwind_worktree(tmp_path) == tmp_path

    def test_linked_worktree_unwinds_to_main_repo(self, monkeypatch, tmp_path):
        main_repo = tmp_path / "main"
        worktree = tmp_path / "main" / ".claude" / "worktrees" / "wt1"
        self._patch_git(monkeypatch, {
            ("rev-parse", "--git-common-dir"): str(main_repo / ".git"),
        })
        assert amd._unwind_worktree(worktree) == main_repo

    def test_git_unavailable_returns_start_unchanged(self, monkeypatch, tmp_path):
        self._patch_git(monkeypatch, {})  # no responses configured -> None
        assert amd._unwind_worktree(tmp_path) == tmp_path

    def test_non_git_common_dir_output_returns_start(self, monkeypatch, tmp_path):
        # Defensive: a resolved path whose basename isn't ".git" is not
        # trusted as a repo root.
        self._patch_git(monkeypatch, {
            ("rev-parse", "--git-common-dir"): str(tmp_path / "not-a-git-dir"),
        })
        assert amd._unwind_worktree(tmp_path) == tmp_path


class TestResolveProjectRoot:
    def test_none_when_claude_project_dir_unset(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        assert amd.resolve_project_root() is None

    def test_unwinds_via_git_common_dir(self, monkeypatch, tmp_path):
        main_repo = tmp_path / "main"
        worktree = tmp_path / "main" / ".claude" / "worktrees" / "wt1"
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(worktree))
        monkeypatch.setattr(
            amd, "_git_output",
            lambda cmd, cwd: str(main_repo / ".git") if tuple(cmd) == ("rev-parse", "--git-common-dir") else None,
        )
        assert amd.resolve_project_root() == main_repo


class TestResolveProjectId:
    """The round-1 plan-review bug: resolve_project_id()'s self-check must
    compare TWO independently-unwound roots, not an unwound root against a
    raw (non-worktree-normalized) `Path(__file__)...` — otherwise a linked
    worktree of THIS repo never matches its own main checkout."""

    def test_none_when_project_root_unresolvable(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
        assert amd.resolve_project_id() is None

    def test_returns_deus_sentinel_for_this_repo_normal_checkout(self, monkeypatch):
        this_repo = Path(amd.__file__).resolve().parent.parent
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(this_repo))
        monkeypatch.setattr(
            amd, "_git_output",
            lambda cmd, cwd: ".git" if tuple(cmd) == ("rev-parse", "--git-common-dir") else None,
        )
        assert amd.resolve_project_id() == amd.DEUS_PROJECT_ID

    def test_returns_deus_sentinel_from_a_linked_worktree_of_this_repo(self, monkeypatch):
        """The exact regression this round fixed: a session running from a
        linked worktree of THIS repo must still resolve to DEUS_PROJECT_ID,
        not a distinct (wrong) encoded id."""
        this_repo = Path(amd.__file__).resolve().parent.parent
        worktree = this_repo / ".claude" / "worktrees" / "some-agent"
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(worktree))

        def _fake_git(cmd, cwd):
            if tuple(cmd) != ("rev-parse", "--git-common-dir"):
                return None
            # Both the worktree AND this_repo unwind to the SAME main repo,
            # exactly as `git rev-parse --git-common-dir` does in practice
            # from anywhere inside either checkout.
            return str(this_repo / ".git")

        monkeypatch.setattr(amd, "_git_output", _fake_git)
        assert amd.resolve_project_id() == amd.DEUS_PROJECT_ID

    def test_returns_encoded_root_for_a_different_project(self, monkeypatch, tmp_path):
        other_repo = tmp_path / "some-other-project"
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other_repo))
        monkeypatch.setattr(
            amd, "_git_output",
            lambda cmd, cwd: ".git" if tuple(cmd) == ("rev-parse", "--git-common-dir") else None,
        )
        assert amd.resolve_project_id() == amd._encode_project_dir(other_repo.as_posix())
        assert amd.resolve_project_id() != amd.DEUS_PROJECT_ID
