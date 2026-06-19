"""Tests for scripts/cogate.py — the one-command warden co-gate marker.

The GPT half (codex_warden.main) is FAKED so no codex CLI / subscription quota is touched;
the fake records the ``<role>@gpt`` verdict into the same resolved bucket the real driver
would, so the wrapper's readback + combined-outcome logic is exercised end to end. The Claude
half (mark_warden) runs for real against a temporary git repo.

This is a background session (CLAUDE_JOB_DIR is set in the ambient env), so every test that
depends on bg-vs-interactive detection sets/clears CLAUDE_JOB_DIR explicitly via monkeypatch —
never relying on the ambient value.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import cogate
from _exit_codes import INTERNAL_ERROR, SUCCESS, USAGE_ERROR


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return tmp_path


def _fake_gpt(verdict: str = "SHIP"):
    """Stand-in for codex_warden.main: record <role>@gpt into the --worktree-root bucket
    (mirroring the real driver) and return 0. Records every argv it was called with."""
    calls: list[list[str]] = []

    def _main(argv):
        calls.append(list(argv))
        role = argv[argv.index("--role") + 1]
        wt = Path(argv[argv.index("--worktree-root") + 1])
        with cogate.whooks.worktree_override(wt):
            mr = cogate.whooks.primary_repo_root(wt)
            cogate.whooks.record_script_verdict(mr, cogate.store_key(role, "gpt"), verdict, "fake gpt")
        return SUCCESS

    _main.calls = calls
    return _main


def _read_gpt(repo_root: Path, role: str):
    with cogate.whooks.worktree_override(repo_root):
        return cogate.whooks.read_claude_verdict(repo_root, role), \
            (cogate.whooks._read_verdicts(cogate.whooks.primary_repo_root(repo_root))
             .get(cogate.store_key(role, "gpt")) or {}).get("verdict")


# (a) happy path — claude SHIP + gpt SHIP → both marked, combined PASS
def test_happy_path_both_ship(git_repo, monkeypatch):
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)  # interactive
    monkeypatch.setattr(cogate.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cogate.codex_warden, "main", _fake_gpt("SHIP"))
    rc = cogate.main(["--role", "code-reviewer", "--claude-verdict", "SHIP",
                      "--claude-reason", "no blocking issues"])
    assert rc == SUCCESS
    claude_v, gpt_v = _read_gpt(git_repo, "code-reviewer")
    assert claude_v == "SHIP" and gpt_v == "SHIP"
    assert (git_repo / ".claude" / ".code-reviewed").exists()  # marker touched (Claude side)


# (b) --worktree-root routes BOTH marks into the target's bucket, not the cwd's
def test_worktree_root_routes_both_marks(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
    cwd_repo = tmp_path / "cwd"
    target = tmp_path / "target"
    subprocess.run(["git", "init", "-q", str(cwd_repo)], check=True)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    monkeypatch.setattr(cogate.cr.cfr, "repo_root", lambda: str(cwd_repo))
    monkeypatch.setattr(cogate.codex_warden, "main", _fake_gpt("SHIP"))
    rc = cogate.main(["--role", "plan-reviewer", "--claude-verdict", "SHIP",
                      "--claude-reason", "sound", "--worktree-root", str(target)])
    assert rc == SUCCESS
    claude_t, gpt_t = _read_gpt(target, "plan-reviewer")
    assert claude_t == "SHIP" and gpt_t == "SHIP"
    # cwd bucket untouched
    claude_c, gpt_c = _read_gpt(cwd_repo, "plan-reviewer")
    assert claude_c is None and gpt_c is None


# (c) bg-session + Claude TRIVIAL → REFUSED, and GPT is NOT run (abort-before-gpt)
def test_bg_trivial_refused_aborts_before_gpt(git_repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_JOB_DIR", str(git_repo))  # exactly what _is_bg_session() checks
    fake = _fake_gpt("SHIP")
    monkeypatch.setattr(cogate.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cogate.codex_warden, "main", fake)
    rc = cogate.main(["--role", "code-reviewer", "--claude-verdict", "TRIVIAL",
                      "--claude-reason", "tiny"])
    assert rc == USAGE_ERROR          # mark_warden returns 2 in a bg session
    assert fake.calls == []           # GPT half never invoked


# (d) unknown / Claude-only role rejected at argparse
def test_unknown_role_rejected(monkeypatch):
    with pytest.raises(SystemExit) as ei:
        cogate.main(["--role", "threat-modeler", "--claude-verdict", "SHIP",
                     "--claude-reason", "x"])
    assert ei.value.code == 2  # argparse usage error


# (e) role→marker mapping is exactly the 3 GPT-wired roles
def test_role_to_marker_mapping():
    assert cogate.ROLE_TO_MARKER == {
        "plan-reviewer": "plan-reviewed",
        "code-reviewer": "code-reviewed",
        "ai-eng-warden": "ai-eng-reviewed",
    }


# (f) INTERACTIVE accepted Claude TRIVIAL + GPT SHIP → combined PASS (exit 0), NOT a false fail
def test_interactive_trivial_plus_ship_passes(git_repo, monkeypatch):
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)  # interactive → TRIVIAL allowed
    monkeypatch.setattr(cogate.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cogate.codex_warden, "main", _fake_gpt("SHIP"))
    rc = cogate.main(["--role", "ai-eng-warden", "--claude-verdict", "TRIVIAL",
                      "--claude-reason", "cosmetic"])
    assert rc == SUCCESS
    claude_v, gpt_v = _read_gpt(git_repo, "ai-eng-warden")
    assert claude_v == "TRIVIAL" and gpt_v == "SHIP"


# (g) --gpt-timeout is forwarded to codex_warden.main as --timeout
def test_gpt_timeout_forwarded(git_repo, monkeypatch):
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
    fake = _fake_gpt("SHIP")
    monkeypatch.setattr(cogate.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cogate.codex_warden, "main", fake)
    rc = cogate.main(["--role", "code-reviewer", "--claude-verdict", "SHIP",
                      "--claude-reason", "ok", "--gpt-timeout", "123"])
    assert rc == SUCCESS
    argv = fake.calls[0]
    assert "--timeout" in argv and argv[argv.index("--timeout") + 1] == "123.0"


# GPT REVISE → combined BLOCK (the gate would block); exit non-zero
def test_gpt_revise_blocks(git_repo, monkeypatch):
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
    monkeypatch.setattr(cogate.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cogate.codex_warden, "main", _fake_gpt("REVISE"))
    rc = cogate.main(["--role", "code-reviewer", "--claude-verdict", "SHIP",
                      "--claude-reason", "ok"])
    assert rc == INTERNAL_ERROR


# --skip-gpt marks Claude only and never invokes the GPT half
def test_skip_gpt_marks_claude_only(git_repo, monkeypatch):
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)
    fake = _fake_gpt("SHIP")
    monkeypatch.setattr(cogate.cr.cfr, "repo_root", lambda: str(git_repo))
    monkeypatch.setattr(cogate.codex_warden, "main", fake)
    rc = cogate.main(["--role", "code-reviewer", "--claude-verdict", "SHIP",
                      "--claude-reason", "ok", "--skip-gpt"])
    assert rc == SUCCESS
    assert fake.calls == []
    claude_v, _ = _read_gpt(git_repo, "code-reviewer")
    assert claude_v == "SHIP"
