"""Tests for scripts/session_preflight.py — the concurrent-session collision detector.

Each probe is a pure function over a Context, so the probe matrix is exercised by
building a Context and monkeypatching the module-level git/session/pid helpers. The
exit-code contract and agent-native output are exercised through ``main()``.
"""
import json
import os
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import session_preflight as sp  # noqa: E402
from _exit_codes import CONFLICT, SUCCESS, USAGE_ERROR  # noqa: E402

TOP = "/repo/top"
NOW_MS = 1_000_000_000_000


def _ctx(**over):
    base = dict(
        toplevel=TOP,
        branch="feat/x",
        window_min=30,
        self_session_ids=set(),
        self_pids=set(),
        now_ms=NOW_MS,
    )
    base.update(over)
    return sp.Context(**base)


def _session(**over):
    s = dict(
        sessionId="other-sid",
        pid=4242,
        cwd="/repo/top",
        updatedAt=NOW_MS - 1000,
        status="busy",
    )
    s.update(over)
    return s


# ── _resolve_window_min ──────────────────────────────────────────────────────
class TestResolveWindow:
    def test_cli_value_wins(self, monkeypatch):
        monkeypatch.setenv("DEUS_PREFLIGHT_WINDOW_MIN", "99")
        assert sp._resolve_window_min(5) == 5

    def test_env_used_when_no_cli(self, monkeypatch):
        monkeypatch.setenv("DEUS_PREFLIGHT_WINDOW_MIN", "45")
        assert sp._resolve_window_min(None) == 45

    @pytest.mark.parametrize("bad", ["abc", "0", "-3", ""])
    def test_bad_env_falls_back_to_default(self, monkeypatch, bad):
        monkeypatch.setenv("DEUS_PREFLIGHT_WINDOW_MIN", bad)
        assert sp._resolve_window_min(None) == sp.DEFAULT_WINDOW_MIN

    def test_no_env_default(self, monkeypatch):
        monkeypatch.delenv("DEUS_PREFLIGHT_WINDOW_MIN", raising=False)
        assert sp._resolve_window_min(None) == sp.DEFAULT_WINDOW_MIN

    def test_nonpositive_cli_ignored(self, monkeypatch):
        monkeypatch.delenv("DEUS_PREFLIGHT_WINDOW_MIN", raising=False)
        assert sp._resolve_window_min(0) == sp.DEFAULT_WINDOW_MIN


# ── _run_git resilience ──────────────────────────────────────────────────────
class TestRunGitResilience:
    @pytest.mark.parametrize("exc", [OSError("boom"), ValueError("embedded null byte")])
    def test_tolerates_subprocess_errors(self, monkeypatch, exc):
        def raise_it(*a, **k):
            raise exc

        monkeypatch.setattr(sp.subprocess, "run", raise_it)
        rc, out, err = sp._run_git(["status"])
        assert rc == 1 and out == ""


# ── probe_live_session_same_tree ─────────────────────────────────────────────
class TestProbeLiveSession:
    def _patch(self, monkeypatch, sessions, top_map=None, alive=True):
        monkeypatch.setattr(sp, "_load_sessions", lambda: iter(sessions))
        monkeypatch.setattr(sp, "_git_toplevel", lambda cwd: (top_map or {}).get(cwd, cwd))
        monkeypatch.setattr(sp, "_pid_alive", lambda pid: alive)

    def test_live_same_tree_is_critical(self, monkeypatch):
        self._patch(monkeypatch, [_session()])
        out = sp.probe_live_session_same_tree(_ctx())
        assert len(out) == 1
        assert out[0].severity == sp.CRITICAL
        assert out[0].code == "live_session_same_tree"

    def test_stale_session_ignored(self, monkeypatch):
        old = _session(updatedAt=NOW_MS - 31 * 60 * 1000)  # outside 30m window
        self._patch(monkeypatch, [old])
        assert sp.probe_live_session_same_tree(_ctx()) == []

    def test_dead_pid_ignored(self, monkeypatch):
        self._patch(monkeypatch, [_session()], alive=False)
        assert sp.probe_live_session_same_tree(_ctx()) == []

    def test_stale_updatedat_but_fresh_heartbeat_is_live(self, monkeypatch):
        # updatedAt is 40m old (outside the 30m window) but the file heartbeat mtime
        # is recent -> a long-busy session must still be detected.
        s = _session(updatedAt=NOW_MS - 40 * 60 * 1000)
        s["_mtime_ms"] = NOW_MS - 5000
        self._patch(monkeypatch, [s])
        out = sp.probe_live_session_same_tree(_ctx())
        assert len(out) == 1 and out[0].severity == sp.CRITICAL

    def test_stale_both_timestamps_ignored(self, monkeypatch):
        s = _session(updatedAt=NOW_MS - 40 * 60 * 1000)
        s["_mtime_ms"] = NOW_MS - 40 * 60 * 1000
        self._patch(monkeypatch, [s])
        assert sp.probe_live_session_same_tree(_ctx()) == []

    def test_different_tree_ignored(self, monkeypatch):
        s = _session(cwd="/other/tree")
        self._patch(monkeypatch, [s], top_map={"/other/tree": "/other/tree"})
        assert sp.probe_live_session_same_tree(_ctx()) == []

    def test_self_excluded_by_session_id(self, monkeypatch):
        self._patch(monkeypatch, [_session(sessionId="me")])
        assert sp.probe_live_session_same_tree(_ctx(self_session_ids={"me"})) == []

    def test_self_excluded_by_pid(self, monkeypatch):
        self._patch(monkeypatch, [_session(pid=777)])
        assert sp.probe_live_session_same_tree(_ctx(self_pids={777})) == []

    def test_missing_keys_skipped(self, monkeypatch):
        self._patch(monkeypatch, [{"sessionId": "x"}, {"cwd": "/repo/top"}])  # no cwd / no updatedAt
        assert sp.probe_live_session_same_tree(_ctx()) == []

    def test_idle_sibling_not_flagged(self, monkeypatch):
        # The false-positive class this probe exists to fix: an idle sibling's
        # heartbeat/mtime stays fresh just from being open, with no actual
        # concurrent editing happening.
        self._patch(monkeypatch, [_session(status="idle")])
        assert sp.probe_live_session_same_tree(_ctx()) == []

    def test_waiting_sibling_is_critical(self, monkeypatch):
        self._patch(monkeypatch, [_session(status="waiting")])
        out = sp.probe_live_session_same_tree(_ctx())
        assert len(out) == 1 and out[0].severity == sp.CRITICAL

    def test_missing_status_is_critical(self, monkeypatch):
        s = _session()
        del s["status"]
        self._patch(monkeypatch, [s])
        out = sp.probe_live_session_same_tree(_ctx())
        assert len(out) == 1 and out[0].severity == sp.CRITICAL

    def test_unknown_status_is_critical(self, monkeypatch):
        # Fail toward still-flagging: an exact idle-only exclusion, not an
        # allowlist, so any future/unrecognized status still counts.
        self._patch(monkeypatch, [_session(status="zombie")])
        out = sp.probe_live_session_same_tree(_ctx())
        assert len(out) == 1 and out[0].severity == sp.CRITICAL


# ── probe_branch_in_another_worktree ─────────────────────────────────────────
class TestProbeBranchWorktree:
    def _porcelain(self, *blocks):
        return 0, "\n\n".join(blocks) + "\n", ""

    def test_branch_in_other_worktree_is_critical(self, monkeypatch):
        out_txt = self._porcelain(
            f"worktree {TOP}\nbranch refs/heads/main",
            "worktree /repo/wt2\nbranch refs/heads/feat/x",
        )
        monkeypatch.setattr(sp, "_run_git", lambda *a, **k: out_txt)
        out = sp.probe_branch_in_another_worktree(_ctx())
        assert len(out) == 1
        assert out[0].code == "branch_in_other_worktree"
        assert "/repo/wt2" in out[0].detail

    def test_branch_only_in_current_top_not_flagged(self, monkeypatch):
        out_txt = self._porcelain(f"worktree {TOP}\nbranch refs/heads/feat/x")
        monkeypatch.setattr(sp, "_run_git", lambda *a, **k: out_txt)
        assert sp.probe_branch_in_another_worktree(_ctx()) == []

    def test_detached_head_noop(self, monkeypatch):
        monkeypatch.setattr(sp, "_run_git", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git should not run")))
        assert sp.probe_branch_in_another_worktree(_ctx(branch=None)) == []

    def test_git_failure_yields_nothing(self, monkeypatch):
        monkeypatch.setattr(sp, "_run_git", lambda *a, **k: (1, "", "boom"))
        assert sp.probe_branch_in_another_worktree(_ctx()) == []


# ── summarize_sibling_worktrees ──────────────────────────────────────────────
class TestSummarizeSiblingWorktrees:
    def _porcelain(self, *blocks):
        return "\n\n".join(blocks) + "\n"

    def _fake_run_git(self, worktree_stdout, log_stdout="subject\x1f2 hours ago"):
        returns = {
            "worktree": (0, worktree_stdout, ""),
            "log": (0, log_stdout, ""),
        }

        def fake(args, **kwargs):
            return returns[args[0]]

        return fake

    def test_multiple_siblings_listed(self, monkeypatch):
        text = self._porcelain(
            f"worktree {TOP}\nbranch refs/heads/main",
            "worktree /repo/wt2\nbranch refs/heads/feat/a",
            "worktree /repo/wt3\nbranch refs/heads/feat/b",
        )
        monkeypatch.setattr(sp, "_run_git", self._fake_run_git(text))
        out = sp.summarize_sibling_worktrees(_ctx())
        assert len(out) == 2
        paths = {w["path"] for w in out}
        assert paths == {os.path.realpath("/repo/wt2"), os.path.realpath("/repo/wt3")}
        for w in out:
            assert w["last_commit_subject"] == "subject"
            assert w["last_commit_age"] == "2 hours ago"
        branches = {w["branch"] for w in out}
        assert branches == {"feat/a", "feat/b"}

    def test_self_excluded(self, monkeypatch):
        text = self._porcelain(f"worktree {TOP}\nbranch refs/heads/main")
        monkeypatch.setattr(sp, "_run_git", self._fake_run_git(text))
        assert sp.summarize_sibling_worktrees(_ctx()) == []

    def test_detached_head_sibling_branch_none(self, monkeypatch):
        text = self._porcelain(
            f"worktree {TOP}\nbranch refs/heads/main",
            "worktree /repo/wt2\ndetached",
        )
        monkeypatch.setattr(sp, "_run_git", self._fake_run_git(text))
        out = sp.summarize_sibling_worktrees(_ctx())
        assert len(out) == 1
        assert out[0]["branch"] is None

    def test_bare_repo_entry_no_branch_line_no_crash(self, monkeypatch):
        text = self._porcelain(
            f"worktree {TOP}\nbranch refs/heads/main",
            "worktree /repo/bare.git\nbare",
        )
        monkeypatch.setattr(sp, "_run_git", self._fake_run_git(text))
        out = sp.summarize_sibling_worktrees(_ctx())
        assert len(out) == 1
        assert out[0]["branch"] is None

    def test_locked_prunable_lines_ignored(self, monkeypatch):
        text = self._porcelain(
            f"worktree {TOP}\nbranch refs/heads/main",
            "worktree /repo/wt2\nbranch refs/heads/feat/a\nlocked\nprunable gone",
        )
        monkeypatch.setattr(sp, "_run_git", self._fake_run_git(text))
        out = sp.summarize_sibling_worktrees(_ctx())
        assert len(out) == 1
        assert out[0]["branch"] == "feat/a"

    def test_git_worktree_list_failure_yields_empty(self, monkeypatch):
        monkeypatch.setattr(sp, "_run_git", lambda *a, **k: (1, "", "boom"))
        assert sp.summarize_sibling_worktrees(_ctx()) == []

    def test_log_failure_still_returns_entry_with_nulls(self, monkeypatch):
        text = self._porcelain(
            f"worktree {TOP}\nbranch refs/heads/main",
            "worktree /repo/wt2\nbranch refs/heads/feat/a",
        )
        returns = {"worktree": (0, text, ""), "log": (1, "", "boom")}
        monkeypatch.setattr(sp, "_run_git", lambda args, **k: returns[args[0]])
        out = sp.summarize_sibling_worktrees(_ctx())
        assert len(out) == 1
        assert out[0]["last_commit_subject"] is None
        assert out[0]["last_commit_age"] is None


# ── probe_open_pr_for_branch ─────────────────────────────────────────────────
class TestProbeOpenPr:
    def _patch_gh(self, monkeypatch, returncode=0, stdout="[]", raises=None):
        class _Proc:
            def __init__(self):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = ""

        def fake_run(*a, **k):
            if raises:
                raise raises
            return _Proc()

        monkeypatch.setattr(sp.subprocess, "run", fake_run)

    def test_open_pr_is_warning(self, monkeypatch):
        self._patch_gh(monkeypatch, stdout=json.dumps([{"number": 5, "title": "wip"}]))
        out = sp.probe_open_pr_for_branch(_ctx())
        assert len(out) == 1
        assert out[0].severity == sp.WARNING
        assert "#5" in out[0].detail

    def test_gh_missing_skipped(self, monkeypatch):
        self._patch_gh(monkeypatch, raises=FileNotFoundError("gh"))
        assert sp.probe_open_pr_for_branch(_ctx()) == []

    def test_no_prs_no_findings(self, monkeypatch):
        self._patch_gh(monkeypatch, stdout="[]")
        assert sp.probe_open_pr_for_branch(_ctx()) == []

    def test_detached_head_noop(self, monkeypatch):
        # gh must not even run when branch is None
        monkeypatch.setattr(sp.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError()))
        assert sp.probe_open_pr_for_branch(_ctx(branch=None)) == []


# ── probe_recent_uncommitted ─────────────────────────────────────────────────
class TestProbeUncommitted:
    def test_recent_edit_is_warning(self, monkeypatch, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        monkeypatch.setattr(sp, "_run_git", lambda *a, **k: (0, " M a.txt\n", ""))
        out = sp.probe_recent_uncommitted(_ctx(toplevel=str(tmp_path)))
        assert len(out) == 1
        assert out[0].code == "recent_uncommitted_edits"

    def test_old_edit_not_flagged(self, monkeypatch, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("x")
        old = (NOW_MS / 1000) - 31 * 60  # 31 min old, window 30
        os.utime(f, (old, old))
        monkeypatch.setattr(sp, "_run_git", lambda *a, **k: (0, " M a.txt\n", ""))
        assert sp.probe_recent_uncommitted(_ctx(toplevel=str(tmp_path))) == []

    def test_rename_uses_new_path(self, monkeypatch, tmp_path):
        f = tmp_path / "new.txt"
        f.write_text("x")
        monkeypatch.setattr(sp, "_run_git", lambda *a, **k: (0, 'R  old.txt -> new.txt\n', ""))
        out = sp.probe_recent_uncommitted(_ctx(toplevel=str(tmp_path)))
        assert len(out) == 1


# ── _pid_alive cross-platform ────────────────────────────────────────────────
class TestPidAliveCrossPlatform:
    def test_non_posix_assumes_alive(self, monkeypatch):
        monkeypatch.setattr(sp.os, "name", "nt")
        called = {"k": False}

        def boom(*a):
            called["k"] = True
            raise AssertionError("os.kill must not run on non-posix")

        monkeypatch.setattr(sp.os, "kill", boom)
        assert sp._pid_alive(123456) is True
        assert called["k"] is False

    def test_posix_dead_pid(self, monkeypatch):
        monkeypatch.setattr(sp.os, "name", "posix")

        def raise_lookup(pid, sig):
            raise ProcessLookupError()

        monkeypatch.setattr(sp.os, "kill", raise_lookup)
        assert sp._pid_alive(999999) is False

    def test_posix_permission_error_means_alive(self, monkeypatch):
        monkeypatch.setattr(sp.os, "name", "posix")

        def raise_perm(pid, sig):
            raise PermissionError()

        monkeypatch.setattr(sp.os, "kill", raise_perm)
        assert sp._pid_alive(1) is True


# ── _build_context env self-exclusion ────────────────────────────────────────
class TestBuildContextEnvSid:
    def _build(self, monkeypatch):
        import argparse

        monkeypatch.setattr(sp, "_git_toplevel", lambda path: TOP)
        monkeypatch.setattr(sp, "_run_git", lambda *a, **k: (0, "main\n", ""))
        # "self" collides with Namespace.__init__'s own first arg, even via
        # **kwargs -- set it with setattr like argparse itself does.
        args = argparse.Namespace(self_pid=None, window_min=None)
        setattr(args, "self", None)
        return sp._build_context(args)

    def test_claude_code_session_id_lands_in_self_ids(self, monkeypatch):
        # CLAUDE_CODE_SESSION_ID is the variable Claude Code actually exports;
        # it must feed the self-exclusion set.
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "me-primary")
        assert "me-primary" in self._build(monkeypatch).self_session_ids

    def test_legacy_claude_session_id_still_honored(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "me-legacy")
        assert "me-legacy" in self._build(monkeypatch).self_session_ids

    def test_no_env_yields_empty_self_ids(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        assert self._build(monkeypatch).self_session_ids == set()


# ── exit-code contract + agent-native output via main() ──────────────────────
class TestMainContract:
    def _patch_main(self, monkeypatch, findings):
        monkeypatch.setattr(sp, "_build_context", lambda args: _ctx())
        monkeypatch.setattr(sp, "PROBES", [lambda ctx: list(findings)])

    def test_critical_exits_conflict(self, monkeypatch, capsys):
        self._patch_main(monkeypatch, [sp.Finding(sp.CRITICAL, "c", "d")])
        assert sp.main(["--json"]) == CONFLICT
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "CONFLICT"
        assert payload["exit_code"] == CONFLICT

    def test_warning_only_exits_success(self, monkeypatch, capsys):
        self._patch_main(monkeypatch, [sp.Finding(sp.WARNING, "w", "d")])
        assert sp.main(["--json"]) == SUCCESS
        assert json.loads(capsys.readouterr().out)["status"] == "WARN"

    def test_clean_exits_success(self, monkeypatch, capsys):
        self._patch_main(monkeypatch, [])
        assert sp.main(["--json"]) == SUCCESS
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "OK"
        assert out["findings"] == []

    def test_not_a_repo_is_usage_error(self, monkeypatch, capsys):
        monkeypatch.setattr(sp, "_git_toplevel", lambda path: None)
        monkeypatch.setenv("DEUS_AGENT_NATIVE", "0")
        assert sp.main([]) == USAGE_ERROR

    def test_agent_native_auto_json(self, monkeypatch, capsys):
        self._patch_main(monkeypatch, [])
        monkeypatch.setenv("DEUS_AGENT_NATIVE", "1")
        assert sp.main([]) == SUCCESS  # no --json flag
        json.loads(capsys.readouterr().out)  # parses => auto-JSON fired

    def test_select_projects_fields(self, monkeypatch, capsys):
        self._patch_main(monkeypatch, [])
        sp.main(["--json", "--select", "status,exit_code"])
        out = json.loads(capsys.readouterr().out)
        assert set(out.keys()) == {"status", "exit_code"}

    def test_human_output_when_not_json(self, monkeypatch, capsys):
        self._patch_main(monkeypatch, [sp.Finding(sp.CRITICAL, "live_session_same_tree", "d")])
        monkeypatch.setenv("DEUS_AGENT_NATIVE", "0")
        assert sp.main([]) == CONFLICT
        text = capsys.readouterr().out
        assert "CONFLICT" in text and "exit 6" in text

    def test_include_worktrees_flag_adds_key_without_changing_exit_code(
        self, monkeypatch, capsys
    ):
        fixed = [{"path": "/repo/wt2", "branch": "feat/a",
                   "last_commit_subject": "s", "last_commit_age": "1 day ago"}]
        self._patch_main(monkeypatch, [sp.Finding(sp.WARNING, "w", "d")])
        monkeypatch.setattr(sp, "summarize_sibling_worktrees", lambda ctx: list(fixed))

        code_without = sp.main(["--json"])
        payload_without = json.loads(capsys.readouterr().out)

        code_with = sp.main(["--json", "--include-worktrees"])
        payload_with = json.loads(capsys.readouterr().out)

        assert code_with == code_without
        assert payload_with["status"] == payload_without["status"]
        assert payload_with["exit_code"] == payload_without["exit_code"]
        assert "sibling_worktrees" not in payload_without
        assert payload_with["sibling_worktrees"] == fixed

    def test_include_worktrees_human_output_appends_lines(self, monkeypatch, capsys):
        fixed = [{"path": "/repo/wt2", "branch": "feat/a",
                   "last_commit_subject": "s", "last_commit_age": "1 day ago"}]
        self._patch_main(monkeypatch, [])
        monkeypatch.setattr(sp, "summarize_sibling_worktrees", lambda ctx: list(fixed))
        monkeypatch.setenv("DEUS_AGENT_NATIVE", "0")
        assert sp.main(["--include-worktrees"]) == SUCCESS
        text = capsys.readouterr().out
        assert "Sibling worktrees:" in text
        assert "/repo/wt2" in text and "feat/a" in text


# ── --critical-only probe selection ──────────────────────────────────────────
class TestCriticalOnly:
    def test_flag_runs_only_critical_probes(self, monkeypatch, capsys):
        ran = []
        crit = lambda ctx: (ran.append("crit") or [])  # noqa: E731
        warn = lambda ctx: (ran.append("warn") or [])  # noqa: E731
        monkeypatch.setattr(sp, "_build_context", lambda args: _ctx())
        monkeypatch.setattr(sp, "CRITICAL_PROBES", [crit])
        monkeypatch.setattr(sp, "PROBES", [crit, warn])
        sp.main(["--critical-only", "--json"])
        assert ran == ["crit"]

    def test_no_flag_runs_all_probes(self, monkeypatch, capsys):
        ran = []
        crit = lambda ctx: (ran.append("crit") or [])  # noqa: E731
        warn = lambda ctx: (ran.append("warn") or [])  # noqa: E731
        monkeypatch.setattr(sp, "_build_context", lambda args: _ctx())
        monkeypatch.setattr(sp, "CRITICAL_PROBES", [crit])
        monkeypatch.setattr(sp, "PROBES", [crit, warn])
        sp.main(["--json"])
        assert ran == ["crit", "warn"]

    def test_network_probe_excluded_from_critical_set(self):
        # main() selects CRITICAL_PROBES verbatim, so membership IS the guarantee
        # that the network gh-PR probe (and the mtime probe) never run with the flag.
        assert sp.probe_open_pr_for_branch not in sp.CRITICAL_PROBES
        assert sp.probe_recent_uncommitted not in sp.CRITICAL_PROBES
        assert sp.probe_live_session_same_tree in sp.CRITICAL_PROBES
        assert sp.probe_branch_in_another_worktree in sp.CRITICAL_PROBES
