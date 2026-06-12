"""Unit tests for scripts/merge_train.py (LIA-193).

Two levels: (1) run_train orchestration — sequencing, dry-run, the
--execute/--approve-admin-merge gating, stop-on-first-failure — by faking the
step helpers; (2) low-level helpers — the stale-autobump safety guard and
worktree parsing — by faking subprocess via `_run`.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1]


def load_mt():
    if "merge_train" in sys.modules:
        return sys.modules["merge_train"]
    spec = importlib.util.spec_from_file_location("merge_train", _SCRIPTS / "merge_train.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["merge_train"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mt():
    return load_mt()


class _Recorder:
    """Callable that records its calls and returns a fixed (ok, detail) tuple."""

    def __init__(self, ret=(True, "ok")):
        self.calls = []
        self.ret = ret

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ret


def _stub_resolution(mt, monkeypatch, branch="feat/x", wt=Path("/tmp/wt")):
    monkeypatch.setattr(mt, "_pr_branch", lambda pr: branch)
    monkeypatch.setattr(mt, "_worktree_for_branch", lambda b: wt)


# ── run_train orchestration ──────────────────────────────────────────────────


def test_dry_run_performs_no_mutations(mt, monkeypatch):
    _stub_resolution(mt, monkeypatch)
    rebase, ci, verify, merge = _Recorder(), _Recorder(), _Recorder(), _Recorder()
    monkeypatch.setattr(mt, "_rebase_and_push", rebase)
    monkeypatch.setattr(mt, "_wait_required_ci", ci)
    monkeypatch.setattr(mt, "_verify_mergeable", verify)
    monkeypatch.setattr(mt, "_admin_merge", merge)

    results = mt.run_train([709], execute=False, approve=False, repo_root=Path("/repo"))

    assert results[0]["phase"] == "dry-run"
    assert not any(r.get("merged") for r in results)
    assert rebase.calls == ci.calls == verify.calls == merge.calls == []


def test_execute_without_approve_stops_before_merge(mt, monkeypatch):
    _stub_resolution(mt, monkeypatch)
    rebase, ci, verify, merge = _Recorder(), _Recorder(), _Recorder(), _Recorder()
    monkeypatch.setattr(mt, "_rebase_and_push", rebase)
    monkeypatch.setattr(mt, "_wait_required_ci", ci)
    monkeypatch.setattr(mt, "_verify_mergeable", verify)
    monkeypatch.setattr(mt, "_admin_merge", merge)

    results = mt.run_train([709], execute=True, approve=False, repo_root=Path("/repo"))

    # Rebase / CI / verify all ran; the merge did NOT.
    assert len(rebase.calls) == 1 and len(ci.calls) == 1 and len(verify.calls) == 1
    assert merge.calls == []
    assert results[0]["phase"] == "stop-before-merge"
    assert results[0]["merged"] is False
    assert results[0]["ok"] is True


def test_execute_with_approve_merges(mt, monkeypatch):
    _stub_resolution(mt, monkeypatch)
    merge = _Recorder((True, "merged (squash, admin)"))
    monkeypatch.setattr(mt, "_rebase_and_push", _Recorder())
    monkeypatch.setattr(mt, "_wait_required_ci", _Recorder())
    monkeypatch.setattr(mt, "_verify_mergeable", _Recorder())
    monkeypatch.setattr(mt, "_admin_merge", merge)
    audits = []

    def _ok_audit(root, msg):
        audits.append(msg)
        return True

    monkeypatch.setattr(mt, "_audit", _ok_audit)

    results = mt.run_train([709], execute=True, approve=True, repo_root=Path("/repo"))

    assert len(merge.calls) == 1
    assert results[0]["merged"] is True
    assert results[0]["phase"] == "merge"
    assert len(audits) == 1  # the merge was audit-logged


def test_audit_failure_blocks_merge(mt, monkeypatch):
    # The audit record is the authorization trail; if it can't be written, the
    # merge must NOT happen.
    _stub_resolution(mt, monkeypatch)
    monkeypatch.setattr(mt, "_rebase_and_push", _Recorder())
    monkeypatch.setattr(mt, "_wait_required_ci", _Recorder())
    monkeypatch.setattr(mt, "_verify_mergeable", _Recorder())
    merge = _Recorder()
    monkeypatch.setattr(mt, "_admin_merge", merge)
    monkeypatch.setattr(mt, "_audit", lambda root, msg: False)  # write fails

    results = mt.run_train([709], execute=True, approve=True, repo_root=Path("/repo"))

    assert merge.calls == []  # merge refused
    assert results[0]["ok"] is False
    assert results[0]["merged"] is False
    assert "audit" in results[0]["detail"].lower()


def test_stop_on_first_failure_halts_remaining_prs(mt, monkeypatch):
    _stub_resolution(mt, monkeypatch)
    monkeypatch.setattr(mt, "_rebase_and_push", _Recorder())
    monkeypatch.setattr(mt, "_wait_required_ci", _Recorder((False, "required CI red")))
    verify, merge = _Recorder(), _Recorder()
    monkeypatch.setattr(mt, "_verify_mergeable", verify)
    monkeypatch.setattr(mt, "_admin_merge", merge)

    results = mt.run_train([709, 710, 711], execute=True, approve=True, repo_root=Path("/repo"))

    # First PR fails at CI; the train stops — PRs 710/711 are never attempted.
    assert len(results) == 1
    assert results[0]["pr"] == 709 and results[0]["ok"] is False and results[0]["phase"] == "ci"
    assert verify.calls == [] and merge.calls == []


def test_missing_worktree_fails_closed(mt, monkeypatch):
    monkeypatch.setattr(mt, "_pr_branch", lambda pr: "feat/x")
    monkeypatch.setattr(mt, "_worktree_for_branch", lambda b: None)
    merge = _Recorder()
    monkeypatch.setattr(mt, "_admin_merge", merge)

    results = mt.run_train([709], execute=True, approve=True, repo_root=Path("/repo"))

    assert results[0]["ok"] is False
    assert "no local worktree" in results[0]["detail"]
    assert merge.calls == []


def test_main_approve_without_execute_is_usage_error(mt):
    assert mt.main(["709", "--approve-admin-merge"]) == mt.USAGE_ERROR


# ── low-level helpers ────────────────────────────────────────────────────────


def _fake_run(responder):
    """Build a `_run` replacement from a (argv -> (rc, stdout, stderr)) responder."""

    def run(argv, cwd=None, timeout=None):
        rc, out, err = responder(argv)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=err)

    return run


def test_stale_autobump_detected_when_subject_and_patterns_only(mt, monkeypatch):
    def responder(argv):
        if "log" in argv:
            return 0, mt.AUTOBUMP_SUBJECT + "\n", ""
        if "show" in argv:
            return 0, "patterns/eval-change.md\npatterns/documentation.md\n", ""
        return 0, "", ""

    monkeypatch.setattr(mt, "_run", _fake_run(responder))
    assert mt._head_is_stale_autobump(Path("/wt")) is True


def test_stale_autobump_not_detected_for_wrong_subject(mt, monkeypatch):
    def responder(argv):
        if "log" in argv:
            return 0, "fix(memory): real change\n", ""
        if "show" in argv:
            return 0, "patterns/eval-change.md\n", ""
        return 0, "", ""

    monkeypatch.setattr(mt, "_run", _fake_run(responder))
    assert mt._head_is_stale_autobump(Path("/wt")) is False


def test_stale_autobump_not_detected_when_touches_non_pattern(mt, monkeypatch):
    # Safety guard: exact subject but a .py file present → must NOT be dropped.
    def responder(argv):
        if "log" in argv:
            return 0, mt.AUTOBUMP_SUBJECT + "\n", ""
        if "show" in argv:
            return 0, "patterns/eval-change.md\nscripts/memory_indexer.py\n", ""
        return 0, "", ""

    monkeypatch.setattr(mt, "_run", _fake_run(responder))
    assert mt._head_is_stale_autobump(Path("/wt")) is False


def test_worktree_for_branch_parses_porcelain(mt, monkeypatch):
    porcelain = (
        "worktree /Users/x/deus\nHEAD abc\nbranch refs/heads/main\n\n"
        "worktree /Users/x/deus/.claude/worktrees/feat-y\nHEAD def\n"
        "branch refs/heads/feat/y\n\n"
    )
    monkeypatch.setattr(mt, "_run", _fake_run(lambda argv: (0, porcelain, "")))
    assert mt._worktree_for_branch("feat/y") == Path("/Users/x/deus/.claude/worktrees/feat-y")
    assert mt._worktree_for_branch("feat/missing") is None


def test_rebase_and_push_full_path_with_bump_and_repush(mt, monkeypatch):
    # The one destructive path: stale autobump present → reset → rebase ok →
    # first push aborted by the drift hook → second push succeeds.
    seen = {"reset": False, "pushes": 0}

    def responder(argv):
        if "fetch" in argv:
            return 0, "", ""
        if "log" in argv:
            return 0, mt.AUTOBUMP_SUBJECT + "\n", ""
        if "show" in argv:
            return 0, "patterns/eval-change.md\n", ""
        if "reset" in argv:
            seen["reset"] = True
            return 0, "", ""
        if "rebase" in argv:
            return 0, "", ""
        if "push" in argv:
            seen["pushes"] += 1
            if seen["pushes"] == 1:
                return 1, "", "drift-check: bump committed. Aborting push"
            return 0, "", ""
        return 0, "", ""

    monkeypatch.setattr(mt, "_run", _fake_run(responder))
    ok, detail = mt._rebase_and_push(Path("/wt"), "feat/x")

    assert ok is True
    assert seen["reset"] is True  # the stale autobump was dropped
    assert seen["pushes"] == 2    # first push aborted, re-pushed once
    assert "re-push" in detail


def test_verify_mergeable_polls_through_unknown(mt, monkeypatch):
    # GitHub reports UNKNOWN right after a push, then settles to MERGEABLE.
    seq = iter([
        {"state": "OPEN", "mergeable": "UNKNOWN", "reviewDecision": "REVIEW_REQUIRED"},
        {"state": "OPEN", "mergeable": "UNKNOWN", "reviewDecision": "REVIEW_REQUIRED"},
        {"state": "OPEN", "mergeable": "MERGEABLE", "reviewDecision": "REVIEW_REQUIRED"},
    ])
    monkeypatch.setattr(mt, "_gh_json", lambda *a, **k: next(seq))
    monkeypatch.setattr(mt.time, "sleep", lambda s: None)
    ok, detail = mt._verify_mergeable(709, retries=3, delay=0)
    assert ok is True
    assert "MERGEABLE" in detail


def test_verify_mergeable_retries_transient_gh_failure(mt, monkeypatch):
    # A transient gh read failure (non-dict) is retried, same as UNKNOWN.
    seq = iter([
        None,  # gh read failed
        {"state": "OPEN", "mergeable": "MERGEABLE", "reviewDecision": "REVIEW_REQUIRED"},
    ])
    monkeypatch.setattr(mt, "_gh_json", lambda *a, **k: next(seq))
    monkeypatch.setattr(mt.time, "sleep", lambda s: None)
    ok, detail = mt._verify_mergeable(709, retries=3, delay=0)
    assert ok is True
    assert "MERGEABLE" in detail


def test_verify_mergeable_conflicting_fails_fast(mt, monkeypatch):
    sleeps = []
    monkeypatch.setattr(mt, "_gh_json", lambda *a, **k: {"state": "OPEN", "mergeable": "CONFLICTING"})
    monkeypatch.setattr(mt.time, "sleep", lambda s: sleeps.append(s))
    ok, detail = mt._verify_mergeable(709)
    assert ok is False
    assert "CONFLICTING" in detail
    assert sleeps == []  # definitive state → no polling


def test_verify_mergeable_gives_up_after_persistent_unknown(mt, monkeypatch):
    monkeypatch.setattr(mt, "_gh_json", lambda *a, **k: {"state": "OPEN", "mergeable": "UNKNOWN"})
    monkeypatch.setattr(mt.time, "sleep", lambda s: None)
    ok, detail = mt._verify_mergeable(709, retries=3, delay=0)
    assert ok is False
    assert "did not settle" in detail


def test_wait_required_ci_delegates_to_helper(mt, monkeypatch):
    """_wait_required_ci now delegates to ci.wait_for_checks.wait_for_required_checks,
    forwarding the PR and poll interval (no more `gh ... --watch`)."""
    import ci.wait_for_checks as wfc

    captured = {}

    def fake_wait(pr, *, interval, timeout, retries=5):
        captured["pr"] = pr
        captured["interval"] = interval
        captured["timeout"] = timeout
        return True, "all 6 required checks green"

    monkeypatch.setattr(wfc, "wait_for_required_checks", fake_wait)
    ok, detail = mt._wait_required_ci(709, interval=30)
    assert ok is True
    assert captured["pr"] == 709
    assert captured["interval"] == 30
    assert captured["timeout"] == mt._CI_WATCH_TIMEOUT
