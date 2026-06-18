"""Tests for scripts/session_preflight.py.

Mocks subprocess (`_run`) and the sessions directory so no real git repo or live
session is needed. Covers the frozen prediction table from the plan.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import session_preflight as sp
from _exit_codes import CONFLICT, SUCCESS, USAGE_ERROR


def _ctx(repo_root="/repo", branch="feat/x", window_min=10, own_sids=None, own_pids=None):
    return sp.Ctx(
        repo_root=Path(repo_root),
        branch=branch,
        window_min=window_min,
        own_session_ids=own_sids or set(),
        own_pids=own_pids or set(),
    )


def _worktree_porcelain(*entries: tuple[str, str]) -> str:
    """entries = (path, branch_name)."""
    lines = []
    for path, br in entries:
        lines += [f"worktree {path}", "HEAD abc123", f"branch refs/heads/{br}", ""]
    return "\n".join(lines)


# ── _run (output handling) ────────────────────────────────────────────────────

class TestRun:
    def test_preserves_leading_whitespace(self):
        # Regression: git status --porcelain encodes status in the first columns;
        # a leading space (" M path") must survive so probe_recent_writes parses
        # the path correctly. _run must not lstrip.
        rc, out, _ = sp._run(["printf", "%s", " M a.txt"])
        assert rc == 0
        assert out == " M a.txt"

    def test_missing_binary_is_nonzero_not_raise(self):
        rc, out, err = sp._run(["definitely-not-a-real-binary-xyz"])
        assert rc != 0
        assert out == ""
        assert "not found" in err


# ── probe_worktree ──────────────────────────────────────────────────────────

class TestProbeWorktree:
    def test_branch_checked_out_elsewhere_is_critical(self):
        ctx = _ctx(repo_root="/repo", branch="feat/x")
        out = _worktree_porcelain(("/repo", "main"), ("/other", "feat/x"))
        with patch.object(sp, "_run", return_value=(0, out, "")):
            findings = sp.probe_worktree(ctx)
        assert len(findings) == 1
        assert findings[0].severity == sp.CRITICAL
        assert "/other" in findings[0].message

    def test_branch_only_here_no_finding(self):
        ctx = _ctx(repo_root="/repo", branch="feat/x")
        out = _worktree_porcelain(("/repo", "feat/x"))
        with patch.object(sp, "_run", return_value=(0, out, "")):
            assert sp.probe_worktree(ctx) == []

    def test_git_failure_is_skipped(self):
        with patch.object(sp, "_run", return_value=(128, "", "boom")):
            findings = sp.probe_worktree(_ctx())
        assert findings[0].severity == sp.SKIPPED


# ── probe_sessions ────────────────────────────────────────────────────────────

class TestProbeSessions:
    def _seed(self, tmp_path, sessions):
        d = tmp_path / ".claude" / "sessions"
        d.mkdir(parents=True)
        for name, obj in sessions.items():
            (d / f"{name}.json").write_text(json.dumps(obj))
        return tmp_path

    def test_live_session_same_checkout_is_critical(self, tmp_path, monkeypatch):
        now = time.time() * 1000
        home = self._seed(
            tmp_path,
            {"111": {"sessionId": "other", "pid": os.getpid(), "cwd": "/work/repo", "status": "busy", "updatedAt": now - 1000}},
        )
        monkeypatch.setattr(sp.Path, "home", classmethod(lambda cls: home))
        ctx = _ctx(repo_root="/work/repo", branch="feat/x")
        with patch.object(sp, "_git_toplevel", return_value=Path("/work/repo")), \
             patch.object(sp.Path, "exists", lambda self: True):
            findings = sp.probe_sessions(ctx, now_ms=now)
        assert any(f.severity == sp.CRITICAL for f in findings)

    def test_own_session_excluded(self, tmp_path, monkeypatch):
        now = time.time() * 1000
        home = self._seed(
            tmp_path,
            {"111": {"sessionId": "me", "pid": os.getpid(), "cwd": "/work/repo", "status": "busy", "updatedAt": now - 1000}},
        )
        monkeypatch.setattr(sp.Path, "home", classmethod(lambda cls: home))
        ctx = _ctx(repo_root="/work/repo", branch="feat/x", own_sids={"me"})
        with patch.object(sp, "_git_toplevel", return_value=Path("/work/repo")), \
             patch.object(sp.Path, "exists", lambda self: True):
            assert sp.probe_sessions(ctx, now_ms=now) == []

    def test_own_session_excluded_by_pid(self, tmp_path, monkeypatch):
        # pid-branch of the self-exclusion OR (distinct from the sessionId branch):
        # an own pid is filtered BEFORE the liveness check, so it never fires.
        now = time.time() * 1000
        home = self._seed(
            tmp_path,
            {"111": {"sessionId": "other", "pid": os.getpid(), "cwd": "/work/repo", "status": "busy", "updatedAt": now - 1000}},
        )
        monkeypatch.setattr(sp.Path, "home", classmethod(lambda cls: home))
        ctx = _ctx(repo_root="/work/repo", branch="feat/x", own_pids={os.getpid()})
        with patch.object(sp, "_git_toplevel", return_value=Path("/work/repo")), \
             patch.object(sp.Path, "exists", lambda self: True):
            assert sp.probe_sessions(ctx, now_ms=now) == []

    def test_stale_session_not_flagged(self, tmp_path, monkeypatch):
        now = time.time() * 1000
        home = self._seed(
            tmp_path,
            {"111": {"sessionId": "other", "pid": os.getpid(), "cwd": "/work/repo", "status": "idle", "updatedAt": now - 9_999_999}},
        )
        monkeypatch.setattr(sp.Path, "home", classmethod(lambda cls: home))
        ctx = _ctx(repo_root="/work/repo", branch="feat/x")
        with patch.object(sp, "_git_toplevel", return_value=Path("/work/repo")), \
             patch.object(sp.Path, "exists", lambda self: True):
            assert sp.probe_sessions(ctx, now_ms=now) == []

    def test_different_checkout_not_flagged(self, tmp_path, monkeypatch):
        now = time.time() * 1000
        home = self._seed(
            tmp_path,
            {"111": {"sessionId": "other", "pid": os.getpid(), "cwd": "/somewhere/else", "status": "busy", "updatedAt": now - 1000}},
        )
        monkeypatch.setattr(sp.Path, "home", classmethod(lambda cls: home))
        ctx = _ctx(repo_root="/work/repo", branch="feat/x")
        with patch.object(sp, "_git_toplevel", return_value=Path("/somewhere/else")), \
             patch.object(sp.Path, "exists", lambda self: True):
            assert sp.probe_sessions(ctx, now_ms=now) == []

    def test_no_sessions_dir_is_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sp.Path, "home", classmethod(lambda cls: tmp_path))
        findings = sp.probe_sessions(_ctx(), now_ms=time.time() * 1000)
        assert findings[0].severity == sp.SKIPPED


# ── probe_open_pr ─────────────────────────────────────────────────────────────

class TestProbeOpenPr:
    def test_open_pr_is_warning(self):
        prs = json.dumps([{"number": 12, "url": "http://pr/12", "state": "OPEN"}])
        with patch.object(sp, "_run", return_value=(0, prs, "")):
            findings = sp.probe_open_pr(_ctx(branch="feat/x"))
        assert findings[0].severity == sp.WARNING
        assert "12" in findings[0].message

    def test_gh_absent_is_skipped(self):
        with patch.object(sp, "_run", return_value=(127, "", "gh: not found")):
            findings = sp.probe_open_pr(_ctx())
        assert findings[0].severity == sp.SKIPPED

    def test_no_prs_no_finding(self):
        with patch.object(sp, "_run", return_value=(0, "[]", "")):
            assert sp.probe_open_pr(_ctx()) == []


# ── probe_recent_writes ───────────────────────────────────────────────────────

class TestProbeRecentWrites:
    def test_recent_file_is_warning(self, tmp_path):
        (tmp_path / "a.txt").write_text("x")
        ctx = _ctx(repo_root=str(tmp_path), branch="feat/x")
        with patch.object(sp, "_run", return_value=(0, " M a.txt", "")):
            findings = sp.probe_recent_writes(ctx, now=time.time())
        assert findings[0].severity == sp.WARNING
        assert findings[0].detail["count"] == 1

    def test_old_file_no_finding(self, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        old = time.time() - 9_999_999
        os.utime(f, (old, old))
        ctx = _ctx(repo_root=str(tmp_path), branch="feat/x")
        with patch.object(sp, "_run", return_value=(0, " M a.txt", "")):
            assert sp.probe_recent_writes(ctx, now=time.time()) == []

    def test_git_status_failure_is_skipped(self):
        with patch.object(sp, "_run", return_value=(1, "", "fail")):
            findings = sp.probe_recent_writes(_ctx())
        assert findings[0].severity == sp.SKIPPED


# ── run() exit-code matrix ────────────────────────────────────────────────────

def _run_dispatch(toplevel="/repo", branch="feat/x"):
    """side_effect for _run covering the calls run() makes before the probes."""
    def inner(cmd, cwd=None, timeout=10):
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "--show-toplevel":
            return (0, toplevel, "")
        if "--abbrev-ref" in cmd:
            return (0, branch, "")
        if cmd[:1] == ["ps"]:
            return (1, "", "")  # short-circuit ancestor walk
        return (0, "", "")
    return inner


class TestRunExitCodes:
    def test_critical_finding_yields_conflict(self):
        with patch.object(sp, "_run", side_effect=_run_dispatch()), \
             patch.object(sp, "PROBES", (lambda ctx: [sp.Finding("x", sp.CRITICAL, "boom")],)):
            assert sp.run() == CONFLICT

    def test_only_warnings_yields_success(self):
        with patch.object(sp, "_run", side_effect=_run_dispatch()), \
             patch.object(sp, "PROBES", (lambda ctx: [sp.Finding("x", sp.WARNING, "meh")],)):
            assert sp.run() == SUCCESS

    def test_clear_yields_success(self):
        with patch.object(sp, "_run", side_effect=_run_dispatch()), \
             patch.object(sp, "PROBES", (lambda ctx: [],)):
            assert sp.run() == SUCCESS

    def test_not_a_git_repo_yields_usage_error(self):
        with patch.object(sp, "_run", return_value=(128, "", "not a repo")):
            assert sp.run() == USAGE_ERROR

    def test_probe_exception_does_not_crash(self):
        def boom(ctx):
            raise RuntimeError("kaboom")

        with patch.object(sp, "_run", side_effect=_run_dispatch()), \
             patch.object(sp, "PROBES", (boom,)):
            # exception captured as SKIPPED, no critical -> SUCCESS
            assert sp.run() == SUCCESS
