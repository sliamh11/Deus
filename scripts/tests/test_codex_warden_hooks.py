from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from argparse import Namespace
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "codex_warden_hooks.py"


def load_hooks():
    spec = importlib.util.spec_from_file_location("codex_warden_hooks", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["codex_warden_hooks"] = module
    spec.loader.exec_module(module)
    return module


def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "config", "user.email", "test@example.invalid"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    (repo / ".claude").mkdir()
    return repo


def apply_patch_event(repo: Path, path: str) -> dict:
    return {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "model": "gpt-test",
        "permission_mode": "default",
        "session_id": "s",
        "tool_name": "apply_patch",
        "tool_use_id": "tool",
        "transcript_path": None,
        "turn_id": "turn",
        "tool_input": {
            "command": f"*** Begin Patch\n*** Update File: {path}\n@@\n-old\n+new\n*** End Patch\n"
        },
    }


def bash_event(repo: Path, command: str) -> dict:
    return {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "model": "gpt-test",
        "permission_mode": "default",
        "session_id": "s",
        "tool_name": "Bash",
        "tool_use_id": "tool",
        "transcript_path": None,
        "turn_id": "turn",
        "tool_input": {"command": command},
    }


def prompt_event(repo: Path, prompt: str) -> dict:
    return {
        "cwd": str(repo),
        "hook_event_name": "UserPromptSubmit",
        "model": "gpt-test",
        "permission_mode": "default",
        "session_id": "s",
        "transcript_path": None,
        "turn_id": "turn",
        "prompt": prompt,
    }


def tool_event(repo: Path, tool_name: str, tool_input: dict | None = None) -> dict:
    return {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "model": "gpt-test",
        "permission_mode": "default",
        "session_id": "s",
        "tool_name": tool_name,
        "tool_use_id": "tool",
        "transcript_path": None,
        "turn_id": "turn",
        "tool_input": tool_input or {},
    }


def test_plan_review_gate_blocks_apply_patch_without_marker(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")

    rc = hooks.run_plan_review_gate(apply_patch_event(repo, "src/app.ts"), repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    assert "plan-reviewer" in specific["permissionDecisionReason"]


def test_plan_review_gate_allows_after_marker(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")
    (repo / ".claude" / ".plan-reviewed").touch()

    rc = hooks.run_plan_review_gate(apply_patch_event(repo, "src/app.ts"), repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_plan_review_gate_blocks_gitignored_target_without_marker(tmp_path, capsys):
    """Regression: gitignored Edit targets no longer bypass the gate.

    Prior to this fix, `_managed_paths` returned an empty `paths` list when
    every event-path was filtered (e.g., by `.gitignore`), and the gate
    short-circuited with `if not paths: return 0`. Now the gate fires
    regardless of post-filter path emptiness, as long as cwd is inside a
    worktree and the marker is absent.

    Note: hooks return rc=0 on deny too — the deny decision is communicated
    via JSON on stdout, not via exit code. `rc == 0` is consistent with both
    pass-through and BLOCK; the `permissionDecision` field distinguishes them.

    Transitive proof that `_warden_enabled` is True for bare `git_repo`:
    `test_plan_review_gate_blocks_apply_patch_without_marker` (above) also
    uses a bare git_repo and reaches the BLOCK path. If the warden were
    disabled, both tests would silently return 0 with no deny JSON, and
    the deny-assertion would fail.
    """
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    # Pattern matches *.local.json. The file at src/app.local.json is then
    # gitignored, so _managed_paths filters it out.
    (repo / ".gitignore").write_text("*.local.json\n", encoding="utf-8")
    (repo / "src" / "app.local.json").write_text("{}\n", encoding="utf-8")
    # No `.warden-verdicts.json` (so the no-marker else-branch fires).

    rc = hooks.run_plan_review_gate(apply_patch_event(repo, "src/app.local.json"), repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    reason = specific["permissionDecisionReason"]
    assert "no plan-reviewer approval marker" in reason
    # The new hint surfaces the empty-paths case to the agent.
    # `filtered target` hint surfaces the empty-paths block (vs the
    # normal "Targets:" listing when paths survive filtering).
    assert "filtered target" in reason


def test_plan_review_gate_blocks_worktree_excluded_target_without_marker(tmp_path, capsys):
    """Regression: edits inside .claude/worktrees/ no longer bypass the gate.

    This is the actual session-bug scenario — subagent worktree edits at
    `.claude/worktrees/<name>/...` were being filtered by `_is_excluded`
    (which rejects paths under `marker_dir/worktrees`), causing
    `_managed_paths` to return empty `paths` and the gate to short-circuit.
    Fixed by re-ordering: marker check first, worktree-presence second,
    then BLOCK regardless of post-filter path emptiness.
    """
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "worktrees" / "foo" / "src").mkdir(parents=True)
    (repo / ".claude" / "worktrees" / "foo" / "src" / "file.ts").write_text(
        "old\n", encoding="utf-8",
    )
    # No `.warden-verdicts.json` (so the no-marker else-branch fires).

    rc = hooks.run_plan_review_gate(
        apply_patch_event(repo, ".claude/worktrees/foo/src/file.ts"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    reason = specific["permissionDecisionReason"]
    assert "no plan-reviewer approval marker" in reason
    # `filtered target` hint surfaces the empty-paths block (vs the
    # normal "Targets:" listing when paths survive filtering).
    assert "filtered target" in reason


def test_plan_review_gate_returns_zero_outside_worktree(tmp_path, capsys):
    """Event from cwd outside any git worktree → Python gate passes silently.

    Pins the non-worktree early-exit. Without this, the empty-paths fix
    could regress in the other direction (firing the gate everywhere).

    LIA-77 scope note: this Python gate is intentionally scoped to deus
    worktrees. The user-level bash hook (~/.claude/hooks/plan-review-gate.sh)
    handles non-git and non-wardens-repo directories by falling back to the
    deus marker. This test pins the Python gate boundary; it is not a gap.
    """
    hooks = load_hooks()
    outside = tmp_path / "outside"
    outside.mkdir()
    # NOT a git repo. `_managed_paths` returns (None, []) and the gate
    # short-circuits with return 0. No `.plan-reviewed` marker required.

    event = apply_patch_event(outside, "any/path.ts")

    rc = hooks.run_plan_review_gate(event, outside)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_plan_review_gate_returns_zero_for_outside_worktree_target(tmp_path, capsys):
    """Regression: cwd-in-worktree + target-outside-worktree must not BLOCK (PR #430 over-fire)."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    outside_target = tmp_path / "outside" / "plan.md"
    outside_target.parent.mkdir(parents=True)
    outside_target.write_text("# plan\n", encoding="utf-8")
    # cwd is inside the worktree (`repo`); target is outside it entirely.
    # No `.plan-reviewed` marker — pre-fix this would BLOCK.

    rc = hooks.run_plan_review_gate(
        apply_patch_event(repo, str(outside_target)),
        repo,
    )

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_plan_review_gate_returns_zero_for_home_plans_target(tmp_path, capsys):
    """Regression: editing `~/.claude/plans/<plan>.md` from worktree cwd must not BLOCK."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    fake_home = tmp_path / "fake_home"
    plan_target = fake_home / ".claude" / "plans" / "plan-xyz.md"
    plan_target.parent.mkdir(parents=True)
    plan_target.write_text("# plan content\n", encoding="utf-8")

    rc = hooks.run_plan_review_gate(
        apply_patch_event(repo, str(plan_target)),
        repo,
    )

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_plan_review_gate_still_blocks_mixed_targets_with_in_worktree_path(
    tmp_path, capsys,
):
    """PR #430 invariant: any in-worktree raw path keeps the gate firing, even when mixed with outside-worktree targets."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / ".gitignore").write_text("*.local.json\n", encoding="utf-8")
    (repo / "src" / "app.local.json").write_text("{}\n", encoding="utf-8")
    outside_target = tmp_path / "outside" / "x.md"
    outside_target.parent.mkdir(parents=True)
    outside_target.write_text("# x\n", encoding="utf-8")
    # Build a multi-file apply_patch command — PATCH_FILE_RE extracts
    # both via the `*** Update File:` regex (codex_warden_hooks.py:234).
    multi_patch = (
        "*** Begin Patch\n"
        f"*** Update File: src/app.local.json\n"
        "@@\n-{}\n+{\"k\": 1}\n"
        f"*** Update File: {outside_target}\n"
        "@@\n-# x\n+# y\n"
        "*** End Patch\n"
    )
    event = tool_event(repo, "apply_patch", {"command": multi_patch})

    rc = hooks.run_plan_review_gate(event, repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    specific = output["hookSpecificOutput"]
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "deny"
    reason = specific["permissionDecisionReason"]
    assert "no plan-reviewer approval marker" in reason


def test_code_review_gate_blocks_git_commit_without_marker(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.run_code_review_gate(bash_event(repo, "git commit -m test"), repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "code-reviewer" in reason


def test_admin_merge_gate_blocks_without_exact_approval(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _green_ci(hooks, monkeypatch)  # CI check is incidental; this gates approval-marker logic

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh pr merge 294 --squash --admin"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "fresh explicit approval" in reason
    assert "approve-admin-merge" in reason


def test_admin_merge_gate_blocks_with_gh_global_repo_flag(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _green_ci(hooks, monkeypatch)  # CI check is incidental; this gates approval-marker logic

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh --repo owner/repo pr merge 294 --squash --admin"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "fresh explicit approval" in reason


def test_admin_merge_gate_blocks_with_gh_short_repo_flag(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _green_ci(hooks, monkeypatch)  # CI check is incidental; this gates admin-command detection

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh -R owner/repo pr merge 294 --squash --admin"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_admin_merge_gate_blocks_equals_form_admin_flag(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _green_ci(hooks, monkeypatch)  # CI check is incidental; this gates admin-command detection

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh pr merge 294 --squash --admin=true"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_admin_merge_gate_blocks_absolute_gh_path(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _green_ci(hooks, monkeypatch)  # CI check is incidental; this gates admin-command detection

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "/opt/homebrew/bin/gh pr merge 294 --squash --admin=true"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_admin_merge_gate_blocks_windows_gh_exe_path(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _green_ci(hooks, monkeypatch)  # CI check is incidental; this gates admin-command detection

    rc = hooks.run_admin_merge_gate(
        bash_event(
            repo,
            r'"C:\Program Files\GitHub CLI\gh.exe" pr merge 294 --admin',
        ),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_admin_merge_detection_handles_windows_shell_tokenization(monkeypatch):
    hooks = load_hooks()
    monkeypatch.setattr(hooks.os, "name", "nt")

    assert hooks._is_admin_merge_command(
        r'"C:\Program Files\GitHub CLI\gh.exe" pr merge 294 --admin'
    )


def test_admin_merge_gate_allows_exact_approved_command_and_consumes_marker(
    tmp_path, capsys, monkeypatch
):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _green_ci(hooks, monkeypatch)  # approve_admin_merge + gate both check CI; mock it green
    command = "gh pr merge 294 --squash --admin"

    assert hooks.approve_admin_merge(command, repo) == 0
    assert (repo / ".claude" / ".admin-merge-approved").exists()
    rc = hooks.run_admin_merge_gate(bash_event(repo, command), repo)

    assert rc == 0
    assert (repo / ".claude" / ".admin-merge-approved").exists() is False
    output = capsys.readouterr().out
    assert "Approved one admin merge command" in output
    assert "permissionDecision" not in output


def test_admin_merge_gate_rejects_stale_marker_for_different_command(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _green_ci(hooks, monkeypatch)  # approve_admin_merge + gate both check CI; mock it green

    assert hooks.approve_admin_merge("gh pr merge 294 --squash --admin", repo) == 0
    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh pr merge 295 --squash --admin"),
        repo,
    )

    assert rc == 0
    assert (repo / ".claude" / ".admin-merge-approved").exists() is False
    output = capsys.readouterr().out
    assert "permissionDecision" in output


def test_admin_merge_gate_ignores_normal_merge_without_admin(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh pr merge 294 --squash"),
        repo,
    )

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_session_init_clears_admin_merge_marker(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    marker = repo / ".claude" / ".admin-merge-approved"
    marker.write_text("{}", encoding="utf-8")

    assert hooks.run_session_init(repo) == 0

    assert not marker.exists()


def test_plan_mode_invalidator_clears_marker_for_exit_plan_mode(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    marker = repo / ".claude" / ".plan-reviewed"
    marker.touch()

    assert hooks.run_plan_mode_invalidator(tool_event(repo, "ExitPlanMode"), repo) == 0

    assert not marker.exists()


def test_plan_mode_invalidator_clears_marker_for_plan_agent(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    marker = repo / ".claude" / ".plan-reviewed"
    marker.touch()

    assert (
        hooks.run_plan_mode_invalidator(
            tool_event(repo, "Agent", {"subagent_type": "Plan"}), repo
        )
        == 0
    )

    assert not marker.exists()


def test_plan_mode_invalidator_clears_marker_for_spawn_agent_plan(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    marker = repo / ".claude" / ".plan-reviewed"
    marker.touch()

    assert (
        hooks.run_plan_mode_invalidator(
            tool_event(repo, "spawn_agent", {"agent_type": "Plan"}), repo
        )
        == 0
    )

    assert not marker.exists()


def test_plan_mode_invalidator_clears_marker_for_plan_prompt(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    marker = repo / ".claude" / ".plan-reviewed"
    marker.touch()

    assert hooks.run_plan_mode_invalidator(prompt_event(repo, "/plan first"), repo) == 0

    assert not marker.exists()


def test_code_review_invalidator_clears_marker_after_edit(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")
    marker = repo / ".claude" / ".code-reviewed"
    marker.touch()

    rc = hooks.run_code_review_invalidator(apply_patch_event(repo, "src/app.ts"), repo)

    assert rc == 0
    assert not marker.exists()


def test_code_review_invalidator_preserves_marker_on_gitignored_edit(tmp_path):
    """Gitignored edits return empty paths → marker must survive."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / ".gitignore").write_text("*.local.json\n", encoding="utf-8")
    (repo / "src" / "app.local.json").write_text("{}\n", encoding="utf-8")
    marker = repo / ".claude" / ".code-reviewed"
    marker.touch()

    rc = hooks.run_code_review_invalidator(
        apply_patch_event(repo, "src/app.local.json"), repo,
    )

    assert rc == 0
    assert marker.exists()


def test_code_review_invalidator_preserves_marker_on_worktree_excluded_edit(tmp_path):
    """Edits inside `.claude/worktrees/<sub>/...` are filtered → marker must survive."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "worktrees" / "foo" / "src").mkdir(parents=True)
    (repo / ".claude" / "worktrees" / "foo" / "src" / "file.ts").write_text(
        "old\n", encoding="utf-8",
    )
    marker = repo / ".claude" / ".code-reviewed"
    marker.touch()

    rc = hooks.run_code_review_invalidator(
        apply_patch_event(repo, ".claude/worktrees/foo/src/file.ts"), repo,
    )

    assert rc == 0
    assert marker.exists()


def test_code_review_invalidator_does_not_clear_marker_outside_worktree(tmp_path):
    """Event from cwd outside any git worktree → marker survives.

    Mirror of the verification-invalidator outside-worktree pin; pins
    that vault and non-repo edits do not over-invalidate.
    """
    hooks = load_hooks()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".claude").mkdir()
    marker = outside / ".claude" / ".code-reviewed"
    marker.touch()

    rc = hooks.run_code_review_invalidator(
        apply_patch_event(outside, "any/path.ts"), outside,
    )

    assert rc == 0
    assert marker.exists()


def test_threat_model_gate_warns_for_security_paths(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "auth.ts").write_text("old\n", encoding="utf-8")

    rc = hooks.run_threat_model_gate(apply_patch_event(repo, "src/auth.ts"), repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert "threat-modeler" in output["systemMessage"]


def test_threat_model_gate_warns_on_worktree_excluded_security_path(tmp_path, capsys):
    """Regression: subagent worktree edits on security paths now warn.

    Pre-fix: `_managed_paths` filtered `.claude/worktrees/<sub>/...` via
    `_is_excluded`, so `paths` was empty and the gate short-circuited at
    `if not paths`. Result: NO `[threat-model-gate]` warning fired even
    though the user just edited `auth.ts` in a subagent worktree.
    Post-fix: SECURITY_PATH_RE runs against raw `_event_paths` within the
    worktree, bypassing `_managed_paths`.
    """
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "worktrees" / "foo" / "src").mkdir(parents=True)
    (repo / ".claude" / "worktrees" / "foo" / "src" / "auth.ts").write_text(
        "old\n", encoding="utf-8",
    )

    rc = hooks.run_threat_model_gate(
        apply_patch_event(repo, ".claude/worktrees/foo/src/auth.ts"), repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    msg = output["systemMessage"]
    assert "[threat-model-gate]" in msg
    assert "auth.ts" in msg


def test_threat_model_gate_warns_on_gitignored_security_path(tmp_path, capsys):
    """Regression: gitignored security file edits now warn.

    Mirror of the worktree-excluded case for the `.gitignore` filter
    branch — gitignored auth/oauth/credential files (e.g., local
    dev-only OAuth state) should still trigger the threat-modeler
    warning.
    """
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / ".gitignore").write_text("*.auth.json\n", encoding="utf-8")
    (repo / "src" / "oauth.auth.json").write_text("{}\n", encoding="utf-8")

    rc = hooks.run_threat_model_gate(
        apply_patch_event(repo, "src/oauth.auth.json"), repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    msg = output["systemMessage"]
    assert "[threat-model-gate]" in msg
    assert "oauth.auth.json" in msg


def test_threat_model_gate_silent_for_non_security_in_filtered_location(tmp_path, capsys):
    """Regression guard against over-warning.

    A filtered-path edit that does NOT match SECURITY_PATH_RE must NOT
    fire the warning. Without this test, the empty-paths fix could
    regress in the other direction by warning on every filtered-path
    edit regardless of content. README.md doesn't match the regex
    (no auth/session/credential/token/etc. token).
    """
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "worktrees" / "foo" / "src").mkdir(parents=True)
    (repo / ".claude" / "worktrees" / "foo" / "src" / "README.md").write_text(
        "docs\n", encoding="utf-8",
    )

    rc = hooks.run_threat_model_gate(
        apply_patch_event(repo, ".claude/worktrees/foo/src/README.md"), repo,
    )

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_threat_model_gate_silent_outside_worktree(tmp_path, capsys):
    """Event from cwd outside any git worktree → no warning.

    Pins the non-worktree early-exit even when the path name matches
    SECURITY_PATH_RE — the gate should not fire on edits to non-Deus
    projects.
    """
    hooks = load_hooks()
    outside = tmp_path / "outside"
    outside.mkdir()
    # NOT a git repo. `_worktree_for_cwd` returns None.

    rc = hooks.run_threat_model_gate(
        apply_patch_event(outside, "src/auth.ts"), outside,
    )

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_path_leak_detector_warns_for_home_path(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "docs").mkdir()
    path = repo / "docs" / "note.md"
    path.write_text(f"path={Path.home() / 'secret'}\n", encoding="utf-8")

    rc = hooks.run_path_leak_detector(apply_patch_event(repo, "docs/note.md"), repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert "absolute path" in output["systemMessage"]


def test_stop_checkpoint_forwards_event(monkeypatch, tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "stop_hook.py").write_text("print('ok')\n", encoding="utf-8")
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)

    assert hooks.run_stop_checkpoint({"hook_event_name": "Stop"}, repo) == 0
    assert calls
    assert calls[0][0][0][1] == str(repo / "scripts" / "stop_hook.py")


def test_memory_tree_hook_forwards_event(monkeypatch, tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "memory_tree_hook.py").write_text("", encoding="utf-8")
    (repo / "INFRA.md").write_text("old\n", encoding="utf-8")
    calls = []

    def fake_forward(event, script):
        calls.append((event, script))
        return 0

    monkeypatch.setattr(hooks, "_run_forwarded_hook", fake_forward)

    assert hooks.run_memory_tree_hook(apply_patch_event(repo, "INFRA.md"), repo) == 0
    assert calls[0][1] == repo / "scripts" / "memory_tree_hook.py"
    assert calls[0][0]["tool_input"]["file_path"] == str(repo / "INFRA.md")


def test_catchup_freshness_is_silent_without_trigger(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    assert hooks.run_catchup_freshness(prompt_event(repo, "hello"), repo) == 0

    assert capsys.readouterr().out == ""


def test_catchup_freshness_uses_configured_vault(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    vault = tmp_path / "vault"
    today = hooks.dt.datetime.now().strftime("%Y-%m-%d")
    (vault / "Session-Logs" / today).mkdir(parents=True)
    (vault / "Session-Logs" / today / "session.md").write_text("", encoding="utf-8")
    (vault / "Checkpoints").mkdir()
    (vault / "Checkpoints" / "checkpoint.md").write_text("", encoding="utf-8")
    (vault / "CLAUDE.md").write_text("pending:\n  - [ ] task\n", encoding="utf-8")
    monkeypatch.setenv("DEUS_VAULT_PATH", str(vault))

    assert hooks.run_catchup_freshness(prompt_event(repo, "/resume"), repo) == 0

    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "session.md" in context
    assert "checkpoint.md" in context
    assert "task" in context
    assert "Brain Dump" not in context


def test_catchup_freshness_warns_without_vault(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.delenv("DEUS_VAULT_PATH", raising=False)
    monkeypatch.setenv("DEUS_CONFIG_PATH", str(tmp_path / "missing.json"))

    assert hooks.run_catchup_freshness(prompt_event(repo, "/resume"), repo) == 0

    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "vault path unknown" in context
    assert "Brain Dump" not in context


def test_memory_retrieval_is_silent_when_tree_missing(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    assert hooks.run_memory_retrieval(prompt_event(repo, "remember this"), repo) == 0

    assert capsys.readouterr().out == ""


def test_memory_retrieval_abstains_on_fell_back_nonzero(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "memory_tree.py").write_text("", encoding="utf-8")

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 1, stdout='{"fell_back": true, "results": []}'
        )

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)

    assert hooks.run_memory_retrieval(prompt_event(repo, "remember this"), repo) == 0

    assert capsys.readouterr().out == ""


def test_memory_retrieval_injects_vault_result(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    vault = tmp_path / "vault"
    (repo / "scripts").mkdir()
    (repo / "scripts" / "memory_tree.py").write_text("", encoding="utf-8")
    (vault / "Notes").mkdir(parents=True)
    (vault / "Notes" / "fact.md").write_text("useful memory\n", encoding="utf-8")
    monkeypatch.setenv("DEUS_VAULT_PATH", str(vault))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps(
                {
                    "fell_back": False,
                    "confidence": 0.9,
                    "results": [{"path": "Notes/fact.md", "score": 0.8}],
                }
            ),
        )

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)

    assert hooks.run_memory_retrieval(prompt_event(repo, "remember this"), repo) == 0

    output = json.loads(capsys.readouterr().out)
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "useful memory" in context
    assert "Brain Dump" not in context


def test_memory_retrieval_omits_abstain_flag_unless_env_set(monkeypatch, tmp_path):
    """#766: the subprocess query omits --abstain unless DEUS_TREE_ABSTAIN is set,
    so memory_tree's resolution chain (env -> learned artifact -> provider default)
    owns the threshold instead of a hook-local 0.45 hardcode."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "memory_tree.py").write_text("", encoding="utf-8")

    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["argv"] = list(args[0])
        return subprocess.CompletedProcess(
            args[0], 0, stdout=json.dumps({"fell_back": True})
        )

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)

    monkeypatch.delenv("DEUS_TREE_ABSTAIN", raising=False)
    hooks.run_memory_retrieval(prompt_event(repo, "remember this"), repo)
    assert "--abstain" not in captured["argv"]

    # Empty / whitespace is treated as unset (aligned with the main-thread hook).
    monkeypatch.setenv("DEUS_TREE_ABSTAIN", "  ")
    hooks.run_memory_retrieval(prompt_event(repo, "remember this"), repo)
    assert "--abstain" not in captured["argv"]

    monkeypatch.setenv("DEUS_TREE_ABSTAIN", "0.37")
    hooks.run_memory_retrieval(prompt_event(repo, "remember this"), repo)
    argv = captured["argv"]
    assert "--abstain" in argv
    assert argv[argv.index("--abstain") + 1] == "0.37"


def test_memory_retrieval_blocks_vault_path_traversal(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    vault = tmp_path / "vault"
    (repo / "scripts").mkdir()
    (repo / "scripts" / "memory_tree.py").write_text("", encoding="utf-8")
    vault.mkdir()
    (tmp_path / "secret.md").write_text("secret outside vault\n", encoding="utf-8")
    monkeypatch.setenv("DEUS_VAULT_PATH", str(vault))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps(
                {
                    "fell_back": False,
                    "results": [{"path": "../secret.md", "score": 0.8}],
                }
            ),
        )

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)

    assert hooks.run_memory_retrieval(prompt_event(repo, "remember this"), repo) == 0

    assert capsys.readouterr().out == ""


def test_memory_retrieval_blocks_auto_memory_path_traversal(
    monkeypatch, tmp_path, capsys
):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    auto_root = tmp_path / "auto-memory"
    (repo / "scripts").mkdir()
    (repo / "scripts" / "memory_tree.py").write_text("", encoding="utf-8")
    auto_root.mkdir()
    (tmp_path / "secret.md").write_text("secret outside auto memory\n", encoding="utf-8")
    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", str(auto_root))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0],
            0,
            stdout=json.dumps(
                {
                    "fell_back": False,
                    "results": [{"path": "auto-memory/../secret.md", "score": 0.8}],
                }
            ),
        )

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)

    assert hooks.run_memory_retrieval(prompt_event(repo, "remember this"), repo) == 0

    assert capsys.readouterr().out == ""


def test_orchestrator_preflight_silent_by_default(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    assert hooks.run_orchestrator_preflight(prompt_event(repo, "/resume"), repo) == 0

    assert capsys.readouterr().out == ""


def test_orchestrator_preflight_silent_on_non_darwin(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.setenv("DEUS_CODEX_ORCHESTRATOR_PREFLIGHT", "1")
    monkeypatch.setattr(hooks.platform, "system", lambda: "Linux")

    assert hooks.run_orchestrator_preflight(prompt_event(repo, "/resume"), repo) == 0

    assert capsys.readouterr().out == ""


def test_orchestrator_preflight_warns_when_opted_in_without_label(
    monkeypatch, tmp_path, capsys
):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.setenv("DEUS_CODEX_ORCHESTRATOR_PREFLIGHT", "1")
    monkeypatch.setattr(hooks.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("DEUS_HEALTHCHECK_LABEL", raising=False)

    assert hooks.run_orchestrator_preflight(prompt_event(repo, "/resume"), repo) == 0

    output = json.loads(capsys.readouterr().out)
    assert "DEUS_HEALTHCHECK_LABEL" in output["hookSpecificOutput"]["additionalContext"]


def test_install_check_and_uninstall_preserve_unrelated_hooks(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text("[features]\nmulti_agent = true\n", encoding="utf-8")
    hooks_json = codex_home / "hooks.json"
    hooks_json.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 unrelated.py",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    args = Namespace(
        repo_root=repo,
        codex_home=codex_home,
        config=config,
        hooks_json=hooks_json,
        script_path=hooks.SCRIPT if hasattr(hooks, "SCRIPT") else SCRIPT,
        python="python3",
        dry_run=False,
    )
    assert hooks.install(args) == 0
    assert "codex_hooks = true" in config.read_text(encoding="utf-8")

    installed = json.loads(hooks_json.read_text(encoding="utf-8"))
    assert installed["hooks"]["Stop"][0]["hooks"][0]["command"] == "python3 unrelated.py"
    commands = [
        handler["command"]
        for groups in installed["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    assert any("codex_warden_hooks.py" in command for command in commands)
    assert "Edit|Write|MultiEdit|apply_patch" in json.dumps(installed)
    assert any("stop-checkpoint" in command for command in commands)
    assert any("memory-retrieval" in command for command in commands)

    assert hooks.check(args) == 0
    assert "installed" in capsys.readouterr().out

    uninstall_args = Namespace(**vars(args), disable_feature=False)
    assert hooks.uninstall(uninstall_args) == 0
    uninstalled = json.loads(hooks_json.read_text(encoding="utf-8"))
    remaining_commands = [
        handler["command"]
        for groups in uninstalled["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    assert remaining_commands == ["python3 unrelated.py"]
    assert "codex_hooks = true" in config.read_text(encoding="utf-8")


def test_install_dry_run_does_not_write_files(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text("model = \"gpt-test\"\n", encoding="utf-8")
    hooks_json = codex_home / "hooks.json"

    args = Namespace(
        repo_root=repo,
        codex_home=codex_home,
        config=config,
        hooks_json=hooks_json,
        script_path=SCRIPT,
        python="python3",
        dry_run=True,
    )

    assert hooks.install(args) == 0
    assert config.read_text(encoding="utf-8") == "model = \"gpt-test\"\n"
    assert not hooks_json.exists()


def test_install_upgrades_existing_managed_hook_interpreter(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text("[features]\ncodex_hooks = true\n", encoding="utf-8")
    hooks_json = codex_home / "hooks.json"
    old_command = (
        f"/usr/bin/env python3 {repo / 'scripts' / 'codex_warden_hooks.py'} "
        f"run plan-review-gate --repo-root {repo}"
    )
    hooks_json.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Edit|Write|apply_patch",
                            "hooks": [
                                {"type": "command", "command": old_command}
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    args = Namespace(
        repo_root=repo,
        codex_home=codex_home,
        config=config,
        hooks_json=hooks_json,
        script_path=SCRIPT,
        python="python3",
        dry_run=False,
    )

    assert hooks.install(args) == 0
    installed = json.loads(hooks_json.read_text(encoding="utf-8"))
    commands = [
        handler["command"]
        for groups in installed["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    assert old_command not in commands
    assert any("python3 " in command for command in commands)
    assert hooks.check(args) == 0


def test_install_uses_custom_script_path(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    hooks_json = codex_home / "hooks.json"
    custom_script = tmp_path / "stable" / "codex_warden_hooks.py"
    custom_script.parent.mkdir()
    custom_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    args = Namespace(
        repo_root=repo,
        codex_home=codex_home,
        config=config,
        hooks_json=hooks_json,
        script_path=custom_script,
        python="python3",
        dry_run=False,
    )

    assert hooks.install(args) == 0
    installed = json.loads(hooks_json.read_text(encoding="utf-8"))
    commands = [
        handler["command"]
        for groups in installed["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    assert all(str(custom_script) in command for command in commands)
    assert hooks.check(args) == 0


def test_check_fails_for_missing_script_path(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    config.write_text("[features]\ncodex_hooks = true\n", encoding="utf-8")
    hooks_json = codex_home / "hooks.json"
    hooks_json.write_text('{"hooks": {}}\n', encoding="utf-8")

    args = Namespace(
        repo_root=repo,
        codex_home=codex_home,
        config=config,
        hooks_json=hooks_json,
        script_path=tmp_path / "missing.py",
        python="python3",
        dry_run=False,
    )

    assert hooks.check(args) == 1
    assert "script-path" in capsys.readouterr().out


def test_load_json_reports_malformed_hooks_json(tmp_path):
    hooks = load_hooks()
    hooks_json = tmp_path / "hooks.json"
    hooks_json.write_text("{not-json", encoding="utf-8")

    try:
        hooks._load_json(hooks_json)
    except ValueError as exc:
        assert "invalid JSON" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_uninstall_allows_missing_script_path(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    hooks_json = codex_home / "hooks.json"
    missing_script = tmp_path / "missing.py"
    managed_command = (
        f"python3 {missing_script} run plan-review-gate --repo-root {repo} "
        f"--script-path {missing_script}"
    )
    hooks_json.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Edit|Write|MultiEdit|apply_patch",
                            "hooks": [{"type": "command", "command": managed_command}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    args = Namespace(
        repo_root=repo,
        codex_home=codex_home,
        config=config,
        hooks_json=hooks_json,
        script_path=missing_script,
        python="python3",
        dry_run=False,
        disable_feature=False,
    )

    assert hooks.uninstall(args) == 0
    assert json.loads(hooks_json.read_text(encoding="utf-8"))["hooks"] == {}


# ── Verdict tracking & mark subcommand ────────────────────────────────────────


def test_mark_creates_marker_and_audit_log(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    result = hooks.mark_warden("plan-reviewed", "SHIP", "tests pass", repo)
    assert result == 0
    assert (repo / ".claude" / ".plan-reviewed").exists()

    verdicts = json.loads((repo / ".claude" / ".warden-verdicts.json").read_text())
    assert verdicts["plan-reviewer"]["verdict"] == "SHIP"

    log = (repo / ".claude" / ".warden-log").read_text()
    assert "plan-reviewer" in log
    assert "SHIP" in log


def test_mark_blocks_trivial_after_revise(tmp_path, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)
    monkeypatch.setenv("DEUS_WARDEN_BYPASS_LOG", str(tmp_path / "bypass.jsonl"))
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)

    hooks._write_verdict(repo, "code-reviewer", "REVISE", "issues found", "agent")

    result = hooks.mark_warden("code-reviewed", "TRIVIAL", "just a typo", repo)
    assert result == 2
    assert not (repo / ".claude" / ".code-reviewed").exists()


def test_mark_allows_ship_after_revise(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    hooks._write_verdict(repo, "code-reviewer", "REVISE", "issues found", "agent")

    result = hooks.mark_warden("code-reviewed", "SHIP", "fixed all issues", repo)
    assert result == 0
    assert (repo / ".claude" / ".code-reviewed").exists()


def test_verdict_tracker_detects_ship(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    event = {
        "cwd": str(repo),
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "code-reviewer"},
        "tool_response": "## Verdict: SHIP\n\nNo blocking issues.",
    }
    hooks.run_verdict_tracker(event, repo)

    verdicts = json.loads((repo / ".claude" / ".warden-verdicts.json").read_text())
    assert verdicts["code-reviewer"]["verdict"] == "SHIP"


def test_verdict_tracker_detects_revise(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    event = {
        "cwd": str(repo),
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "plan-reviewer"},
        "tool_response": "## Verdict: REVISE\n\nTwo blocking issues.",
    }
    hooks.run_verdict_tracker(event, repo)

    verdicts = json.loads((repo / ".claude" / ".warden-verdicts.json").read_text())
    assert verdicts["plan-reviewer"]["verdict"] == "REVISE"


def test_verdict_tracker_ignores_non_warden_agents(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    event = {
        "cwd": str(repo),
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "Explore"},
        "tool_response": "Found 3 files.",
    }
    hooks.run_verdict_tracker(event, repo)
    assert not (repo / ".claude" / ".warden-verdicts.json").exists()


def _repo_with_worktree(tmp_path: Path, name: str = "wt-a", subdir: str = ".claude/worktrees") -> tuple[Path, Path]:
    """A git repo with one commit and one linked worktree under ``subdir``."""
    repo = git_repo(tmp_path)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "--no-verify"],
        cwd=repo, check=True, stdout=subprocess.DEVNULL,
    )
    wt = repo / subdir / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", f"branch-{name}"],
        cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return repo, wt.resolve()


def _bucket_dir(repo: Path, wt: Path) -> Path:
    import hashlib

    wt_id = hashlib.sha1(str(wt.resolve()).encode()).hexdigest()[:12]
    return repo / ".claude" / "worktree-markers" / wt_id


def test_verdict_tracker_parses_dict_content_block_list(tmp_path):
    # Live-harness shape: tool_response.content is a LIST of text blocks.
    # str() on it repr-escapes newlines and the ^-anchored verdict regex never
    # matches — verified live 2026-07-04 (real dispatch captured by the bash
    # hook but missed by this tracker).
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    event = {
        "cwd": str(repo),
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "code-reviewer"},
        "tool_response": {
            "content": [{"type": "text", "text": "## Verdict: SHIP\n\nNo blocking issues."}]
        },
    }
    hooks.run_verdict_tracker(event, repo)

    verdicts = json.loads((repo / ".claude" / ".warden-verdicts.json").read_text())
    assert verdicts["code-reviewer"]["verdict"] == "SHIP"


def test_worktree_from_prompt_single_match(tmp_path):
    hooks = load_hooks()
    repo, wt = _repo_with_worktree(tmp_path)
    tool_input = {"subagent_type": "code-reviewer", "prompt": f"Review the diff in the worktree at {wt}."}
    assert hooks._worktree_from_prompt(tool_input, repo) == wt


def test_worktree_from_prompt_no_path_returns_none(tmp_path):
    hooks = load_hooks()
    repo, _ = _repo_with_worktree(tmp_path)
    tool_input = {"subagent_type": "code-reviewer", "prompt": "Review the working-tree diff."}
    assert hooks._worktree_from_prompt(tool_input, repo) is None


def test_worktree_from_prompt_ambiguous_returns_none(tmp_path):
    hooks = load_hooks()
    repo, wt_a = _repo_with_worktree(tmp_path)
    wt_b = repo / ".claude" / "worktrees" / "wt-b"
    subprocess.run(
        ["git", "worktree", "add", str(wt_b), "-b", "branch-wt-b"],
        cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    prompt = f"Compare the worktree at {wt_a} against the one at {wt_b.resolve()}."
    tool_input = {"subagent_type": "code-reviewer", "prompt": prompt}
    assert hooks._worktree_from_prompt(tool_input, repo) is None


def test_worktree_from_prompt_unregistered_path_returns_none(tmp_path):
    hooks = load_hooks()
    repo, _ = _repo_with_worktree(tmp_path)
    fake = repo / ".claude" / "worktrees" / "not-registered"
    tool_input = {"subagent_type": "code-reviewer", "prompt": f"Review the worktree at {fake}."}
    assert hooks._worktree_from_prompt(tool_input, repo) is None


def test_worktree_from_prompt_prefix_sibling_not_ambiguous(tmp_path):
    # gpt round-1 finding: wt-a is a string prefix of wt-a-extended; a prompt
    # naming ONLY the longer path must route to it, not report ambiguity.
    hooks = load_hooks()
    repo, _wt_a = _repo_with_worktree(tmp_path, name="wt-a")
    wt_ext = repo / ".claude" / "worktrees" / "wt-a-extended"
    subprocess.run(
        ["git", "worktree", "add", str(wt_ext), "-b", "branch-wt-a-extended"],
        cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    tool_input = {
        "subagent_type": "code-reviewer",
        "prompt": f"Review the diff in the worktree at {wt_ext.resolve()}.",
    }
    assert hooks._worktree_from_prompt(tool_input, repo) == wt_ext.resolve()


def test_worktree_from_prompt_unregistered_longer_sibling_not_credited(tmp_path):
    # gpt round-2 finding: a prompt naming an UNREGISTERED longer sibling
    # (wt-a-extended when only wt-a is registered) must not credit wt-a —
    # the reviewed tree was never wt-a. Boundary check, not registry shadowing.
    hooks = load_hooks()
    repo, wt_a = _repo_with_worktree(tmp_path, name="wt-a")
    ghost = f"{wt_a}-extended"
    tool_input = {
        "subagent_type": "code-reviewer",
        "prompt": f"Review the diff in the worktree at {ghost}.",
    }
    assert hooks._worktree_from_prompt(tool_input, repo) is None


def test_worktree_from_prompt_dotted_sibling_not_credited(tmp_path):
    # wt-a.backup must not credit wt-a ("." followed by a path char is a
    # continuation), while a sentence-ending period is a boundary.
    hooks = load_hooks()
    repo, wt_a = _repo_with_worktree(tmp_path, name="wt-a")
    tool_input = {
        "subagent_type": "code-reviewer",
        "prompt": f"Compare against the snapshot at {wt_a}.backup for context.",
    }
    assert hooks._worktree_from_prompt(tool_input, repo) is None


def test_worktree_from_prompt_file_inside_worktree_counts(tmp_path):
    hooks = load_hooks()
    repo, wt = _repo_with_worktree(tmp_path)
    tool_input = {
        "subagent_type": "code-reviewer",
        "prompt": f"Look at {wt}/scripts/foo.py in that worktree.",
    }
    assert hooks._worktree_from_prompt(tool_input, repo) == wt


def test_worktree_from_prompt_outside_claude_worktrees(tmp_path):
    # Round-1 plan-review regression case: worktrees are NOT all under
    # .claude/worktrees/ (e.g. data/worktrees/LIA-124 in the live repo).
    hooks = load_hooks()
    repo, wt = _repo_with_worktree(tmp_path, name="LIA-124", subdir="data/worktrees")
    tool_input = {"subagent_type": "code-reviewer", "prompt": f"Review the diff in the worktree at {wt}."}
    assert hooks._worktree_from_prompt(tool_input, repo) == wt


def test_verdict_tracker_routes_to_prompt_worktree_exclusively(tmp_path):
    # gpt round-3 finding: crediting the event-cwd bucket with a verdict about
    # ANOTHER worktree's diff is wrong-credit — the routed bucket is the ONLY
    # write when the prompt names a different worktree.
    hooks = load_hooks()
    repo, wt = _repo_with_worktree(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True, exist_ok=True)

    event = {
        "cwd": str(repo),
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "code-reviewer",
            "prompt": f"Review the working-tree diff in the worktree at {wt}.",
        },
        "tool_response": "## Verdict: SHIP\n\nNo blocking issues.",
    }
    hooks.run_verdict_tracker(event, repo)

    routed = json.loads((_bucket_dir(repo, wt) / ".warden-verdicts.json").read_text())
    assert routed["code-reviewer"]["verdict"] == "SHIP"
    assert "routed to reviewed worktree" in routed["code-reviewer"]["reason"]
    assert not (repo / ".claude" / ".warden-verdicts.json").exists()


def test_verdict_tracker_never_credits_unnamed_worktree(tmp_path):
    # ai-eng finding: a prompt naming ONLY worktree B must not credit worktree
    # A (the launch cwd) — gate isolation for the bucket the event cwd is in.
    hooks = load_hooks()
    repo, wt_a = _repo_with_worktree(tmp_path, name="wt-a")
    wt_b = repo / ".claude" / "worktrees" / "wt-b"
    subprocess.run(
        ["git", "worktree", "add", str(wt_b), "-b", "branch-wt-b"],
        cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wt_b = wt_b.resolve()
    (repo / ".claude" / "wardens").mkdir(parents=True, exist_ok=True)

    event = {
        "cwd": str(wt_a),
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "code-reviewer",
            "prompt": f"Review the diff in the worktree at {wt_b}.",
        },
        "tool_response": "## Verdict: SHIP\n\nNo blocking issues.",
    }
    hooks.run_verdict_tracker(event, repo)

    routed = json.loads((_bucket_dir(repo, wt_b) / ".warden-verdicts.json").read_text())
    assert routed["code-reviewer"]["verdict"] == "SHIP"
    assert not (_bucket_dir(repo, wt_a) / ".warden-verdicts.json").exists()
    assert not (repo / ".claude" / ".warden-verdicts.json").exists()


def test_verdict_tracker_flat_only_without_prompt_worktree(tmp_path):
    hooks = load_hooks()
    repo, wt = _repo_with_worktree(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True, exist_ok=True)

    event = {
        "cwd": str(repo),
        "hook_event_name": "PostToolUse",
        "tool_name": "Agent",
        "tool_input": {"subagent_type": "code-reviewer", "prompt": "Review the working-tree diff."},
        "tool_response": "## Verdict: SHIP\n\nNo blocking issues.",
    }
    hooks.run_verdict_tracker(event, repo)

    flat = json.loads((repo / ".claude" / ".warden-verdicts.json").read_text())
    assert flat["code-reviewer"]["verdict"] == "SHIP"
    assert not (_bucket_dir(repo, wt) / ".warden-verdicts.json").exists()


def test_plan_review_gate_shows_revise_escalation(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)
    (repo / "src").mkdir()
    (repo / "src" / "foo.ts").write_text("export const foo = 1;")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        stdout=subprocess.DEVNULL,
    )

    hooks._write_verdict(repo, "plan-reviewer", "REVISE", "blocking issue", "agent")

    event = apply_patch_event(repo, "src/foo.ts")
    hooks.run_plan_review_gate(event, repo)
    out = capsys.readouterr().out
    assert "REVISE" in out
    assert "Trivial-change bypass" not in out


def test_code_review_gate_shows_revise_escalation(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    hooks._write_verdict(repo, "code-reviewer", "REVISE", "blocking issue", "agent")

    event = bash_event(repo, "git commit -m test")
    hooks.run_code_review_gate(event, repo)
    out = capsys.readouterr().out
    assert "REVISE" in out
    assert "Trivial-commit bypass" not in out


# ── TRIVIAL bypass enforcement (B + C + D) ──────────────────────────────────


def test_mark_blocks_trivial_after_block(tmp_path, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)
    monkeypatch.setenv("DEUS_WARDEN_BYPASS_LOG", str(tmp_path / "bypass.jsonl"))
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)

    hooks._write_verdict(repo, "code-reviewer", "BLOCK", "critical issues", "agent")

    result = hooks.mark_warden("code-reviewed", "TRIVIAL", "just a typo", repo)
    assert result == 2
    assert not (repo / ".claude" / ".code-reviewed").exists()


def test_mark_blocks_trivial_in_bg_session(tmp_path, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)
    monkeypatch.setenv("DEUS_WARDEN_BYPASS_LOG", str(tmp_path / "bypass.jsonl"))
    monkeypatch.setenv("CLAUDE_JOB_DIR", str(tmp_path / "job"))

    hooks._write_verdict(repo, "plan-reviewer", "SHIP", "all good", "agent")

    result = hooks.mark_warden("plan-reviewed", "TRIVIAL", "just a comment fix", repo)
    assert result == 2
    assert not (repo / ".claude" / ".plan-reviewed").exists()


def test_mark_allows_trivial_interactive_no_prior_verdict(tmp_path, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)
    monkeypatch.setenv("DEUS_WARDEN_BYPASS_LOG", str(tmp_path / "bypass.jsonl"))
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)

    result = hooks.mark_warden("plan-reviewed", "TRIVIAL", "typo fix", repo)
    assert result == 0
    assert (repo / ".claude" / ".plan-reviewed").exists()


def test_mark_allows_trivial_interactive_after_ship(tmp_path, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)
    monkeypatch.setenv("DEUS_WARDEN_BYPASS_LOG", str(tmp_path / "bypass.jsonl"))
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)

    hooks._write_verdict(repo, "plan-reviewer", "SHIP", "all good", "agent")

    result = hooks.mark_warden("plan-reviewed", "TRIVIAL", "typo fix", repo)
    assert result == 0
    assert (repo / ".claude" / ".plan-reviewed").exists()


def test_bypass_log_written_on_trivial_success(tmp_path, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)
    log_path = tmp_path / "bypass.jsonl"
    monkeypatch.setenv("DEUS_WARDEN_BYPASS_LOG", str(log_path))
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)

    hooks.mark_warden("code-reviewed", "TRIVIAL", "just a typo", repo)

    assert log_path.exists()
    entry = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert entry["warden"] == "code-reviewer"
    assert entry["verdict"] == "TRIVIAL"
    assert entry["session_type"] == "interactive"
    assert entry["reason"] == "just a typo"
    assert "timestamp" in entry
    assert "cwd" in entry
    assert "diff_stats" in entry


def test_bypass_log_written_on_trivial_refusal(tmp_path, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)
    log_path = tmp_path / "bypass.jsonl"
    monkeypatch.setenv("DEUS_WARDEN_BYPASS_LOG", str(log_path))
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)

    hooks._write_verdict(repo, "code-reviewer", "REVISE", "issues", "agent")
    hooks.mark_warden("code-reviewed", "TRIVIAL", "just a typo", repo)

    assert log_path.exists()
    entry = json.loads(log_path.read_text(encoding="utf-8").strip())
    assert entry["warden"] == "code-reviewer"
    assert entry["verdict"] == "REFUSED"
    assert entry["session_type"] == "interactive"


# ── Verification gate ────────────────────────────────────────────────────────


def test_verification_gate_blocks_git_commit_without_marker(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.run_verification_gate(bash_event(repo, "git commit -m test"), repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "verification-gate" in reason


def test_verification_gate_allows_after_marker(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    # Gate now reads from .warden-verdicts.json; touching the file alone is
    # not sufficient — write the JSON SHIP verdict as the mark command does.
    hooks._write_verdict(repo, "verification-gate", "SHIP", "all good", "mark")
    (repo / ".claude" / ".verified").touch()

    rc = hooks.run_verification_gate(bash_event(repo, "git commit -m test"), repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_verification_gate_shows_revise_escalation(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    hooks._write_verdict(repo, "verification-gate", "REVISE", "incomplete", "agent")

    event = bash_event(repo, "git commit -m test")
    hooks.run_verification_gate(event, repo)
    out = capsys.readouterr().out
    assert "REVISE" in out
    assert "Trivial-commit bypass" not in out


# ── GIT_COMMIT_RE (LIA-518: broadened commit-gate trigger) ──────────────────
#
# First direct coverage for this regex — previously exercised only indirectly
# through the three gate functions' own tests. Cases mirror the ticket's
# listed bypass forms plus the false-positive/negative battery and ReDoS
# checks worked out across plan review.


@pytest.mark.parametrize(
    "command,expected",
    [
        # Ticket-listed bypass forms (previously all False negatives)
        ("git commit -m x", True),
        ("git --no-pager commit", True),
        ("git -C /a -C b commit", True),
        ("git -C '/path with spaces' commit -m x", True),
        ('git -C "/path with spaces" commit -m x', True),
        ("env git commit", True),
        ("GIT_DIR=/foo git commit", True),
        ("sudo git commit", True),
        ("echo hi\ngit commit -m x", True),
        ("foo; git commit -m x", True),
        ("foo && git commit -m x", True),
        ("foo || git commit -m x", True),
        # Indented multiline (round 2 — the line-start anchor must allow
        # leading horizontal whitespace, not just true line-start)
        ("echo ready\n  git commit", True),
        ("echo ready\n\tgit commit", True),
        ("  git commit -m x", True),
        ("echo ready\ngit --no-pager commit", True),
        ("echo ready\n  sudo git commit", True),
        # Empty / quoted env-var values (round 4)
        ("FOO= git commit", True),
        ("FOO='two words' git commit", True),
        ('FOO="two words" git commit', True),
        # --long=value global flag form
        ("git --git-dir=/foo commit", True),
        # Other single-letter global flags (must stay matched -- CodeQL's ReDoS fix
        # narrowed the generic short-flag branch to exclude only C/c, not every letter;
        # -P is literally the short form of --no-pager, this ticket's own headline case)
        ("git -p commit", True),
        ("git -P commit", True),
        ("git -q commit", True),
        ("git -s commit", True),
        # Existing behavior preserved
        ("git commit -- file.py", True),
        ("git -C /tmp/repo commit", True),
        ("cd foo && git commit", True),
        # Negative cases — must NOT match
        ("git commitment", False),
        ("git log --oneline", False),
        ("git add commit.txt", False),
        ("cat commit.txt", False),
        ("git show HEAD:committee.md", False),
        ("mygit commit", False),
        ('git log --grep="git commit"', False),
        ("git status", False),
        ("git committed-files", False),
        ("echo git commit", False),
        # Known, accepted, non-regressing limitation (LIA-518 round 4): quoted
        # values are only recognized for -C's own argument; -c doesn't parse
        # embedded/partial shell quoting (never supported this at all, so no
        # regression — see the docstring above GIT_COMMIT_RE for rationale).
        ('git -c user.name="John Doe" commit', False),
    ],
)
def test_git_commit_re(command, expected):
    hooks = load_hooks()
    assert bool(hooks.GIT_COMMIT_RE.search(command)) is expected


def test_git_commit_re_heredoc_mention_is_a_known_accepted_over_trigger():
    # LIA-518: adding re.MULTILINE means a heredoc merely MENTIONING "git
    # commit" on its own line (not an actual invocation) now also matches —
    # a deliberately accepted new false-positive class, traded for closing
    # the real gate-bypass. This gate's purpose is "never silently let a real
    # commit skip review": a spurious block is a minor, recoverable
    # annoyance; a missed gate is a silent, unrecoverable bypass. This test
    # documents the tradeoff as intentional so a future edit doesn't "fix" it
    # back into a false negative.
    hooks = load_hooks()
    heredoc = (
        "cat <<'EOF' > README.md\n"
        "Remember to run:\n"
        "git commit -m \"your message\"\n"
        "EOF"
    )
    assert hooks.GIT_COMMIT_RE.search(heredoc) is not None


def test_git_commit_re_no_redos_on_consecutive_newlines():
    # LIA-518 round 3: `^\s*` (an earlier, reverted candidate) was O(n^2) on
    # a long run of consecutive newlines under re.MULTILINE — `^` matches at
    # every line and `\s*` re-walks the remaining run each time. The shipped
    # regex uses `^[ \t]*` (horizontal whitespace only) specifically to avoid
    # this. Regression guard: must stay well under a second even at 200k
    # consecutive newlines.
    hooks = load_hooks()
    payload = ("\n" * 200_000) + "notgit"
    start = time.perf_counter()
    hooks.GIT_COMMIT_RE.search(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"GIT_COMMIT_RE took {elapsed:.2f}s on 200k consecutive newlines"


def test_git_commit_re_no_redos_on_unterminated_quote_chain():
    # LIA-518 round 4: guards the new quoted-value alternatives (env-var
    # values, -C's path) against a backtracking blowup on a long run of
    # unterminated quote-opens.
    hooks = load_hooks()
    payload = "git " + ("-C 'unterminated " * 20_000) + "notcommit"
    start = time.perf_counter()
    hooks.GIT_COMMIT_RE.search(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"GIT_COMMIT_RE took {elapsed:.2f}s on an unterminated-quote chain"


def test_git_commit_re_no_redos_on_ambiguous_flag_alternation():
    # CodeQL py/redos (caught in CI on this PR, high severity): the flags-
    # repetition group originally included a generic `-[A-Za-z]` short-flag
    # alternative alongside the dedicated `-C`/`-c` branches. Since -C and -c
    # are themselves single letters, EVERY `-C`/`-c` token could be consumed
    # two different ways (via its own branch, or via the generic short-flag
    # branch), and with many repetitions the number of ways to partition the
    # string between the two interpretations grows exponentially. Confirmed
    # exploitable pre-fix: this exact payload at 2000 reps was still fast
    # (linear scaling makes it a poor illustration on its own), but the same
    # shape at just 25 repetitions took 11+ seconds pre-fix. Fixed by
    # narrowing the generic short-flag branch to exclude ONLY `C`/`c`
    # (`-[A-BD-Za-bd-z]`), not removing it outright -- an earlier attempt at
    # this fix removed the branch entirely and silently regressed coverage
    # for every other single-letter global flag, including -P (the short
    # form of --no-pager, this ticket's own headline case) -- see
    # test_git_commit_re for the -p/-P/-q/-s positive-match coverage that
    # guards against re-losing this. Regression guard here: must stay fast
    # at 2000+ repetitions, far past where the pre-fix version was already
    # unusable.
    hooks = load_hooks()
    payload = "&git " + ("-C -A " * 2000) + "nope"
    start = time.perf_counter()
    hooks.GIT_COMMIT_RE.search(payload)
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"GIT_COMMIT_RE took {elapsed:.2f}s on an ambiguous -C/-A chain"


def test_git_commit_re_no_redos_on_ambiguous_quoted_value_alternation():
    # CodeQL py/redos (caught in CI on this PR, high severity): the -C and
    # env-var value patterns originally fell back to a bare `\S+`/`\S*` token
    # that could ALSO match already-quoted content (e.g. `"x"` has no
    # whitespace, so `\S+` matches it just as well as the quoted alternative
    # does) -- two alternatives matching the same span is the same
    # exponential-ambiguity shape as the flag-alternation bug above. Fixed by
    # excluding quote characters from the bare-token fallback
    # (`[^'"\s]+`/`[^'"\s]*`), making the alternatives mutually exclusive.
    hooks = load_hooks()
    payload_c = "git -C " + ('"" -C ' * 2000) + "nope"
    payload_env = "&git " + ('"" A=' * 2000) + "nope"
    for label, payload in (("-C value", payload_c), ("env value", payload_env)):
        start = time.perf_counter()
        hooks.GIT_COMMIT_RE.search(payload)
        elapsed = time.perf_counter() - start
        assert elapsed < 1.0, f"GIT_COMMIT_RE took {elapsed:.2f}s on ambiguous {label} alternation"


def test_verification_gate_blocks_no_pager_commit(tmp_path, capsys):
    # End-to-end proof (not just regex-unit-level) that the LIA-518 fix
    # closes a real gate bypass in practice: --no-pager was a confirmed
    # false negative on the pre-fix regex.
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.run_verification_gate(bash_event(repo, "git --no-pager commit -m test"), repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "verification-gate" in reason


def test_verification_invalidator_clears_marker_after_edit(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")
    marker = repo / ".claude" / ".verified"
    marker.touch()

    rc = hooks.run_verification_invalidator(apply_patch_event(repo, "src/app.ts"), repo)

    assert rc == 0
    assert not marker.exists()


def test_verification_invalidator_preserves_marker_on_gitignored_edit(tmp_path):
    """Gitignored edits return empty paths → `.verified` must survive."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / ".gitignore").write_text("*.local.json\n", encoding="utf-8")
    (repo / "src" / "app.local.json").write_text("{}\n", encoding="utf-8")
    marker = repo / ".claude" / ".verified"
    marker.touch()

    rc = hooks.run_verification_invalidator(
        apply_patch_event(repo, "src/app.local.json"), repo,
    )

    assert rc == 0
    assert marker.exists()


def test_verification_invalidator_preserves_marker_on_worktree_excluded_edit(tmp_path):
    """Edits inside `.claude/worktrees/<sub>/...` are filtered → `.verified` must survive."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "worktrees" / "foo" / "src").mkdir(parents=True)
    (repo / ".claude" / "worktrees" / "foo" / "src" / "file.ts").write_text(
        "old\n", encoding="utf-8",
    )
    marker = repo / ".claude" / ".verified"
    marker.touch()

    rc = hooks.run_verification_invalidator(
        apply_patch_event(repo, ".claude/worktrees/foo/src/file.ts"), repo,
    )

    assert rc == 0
    assert marker.exists()


def test_verification_invalidator_does_not_clear_marker_outside_worktree(tmp_path):
    """Event from cwd outside any git worktree → marker survives.

    Pins the non-worktree early-exit. Without this, the empty-paths fix
    could regress in the other direction (invalidating everywhere — e.g.,
    every `/compress` write to the vault would clear `.verified`).
    """
    hooks = load_hooks()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".claude").mkdir()
    marker = outside / ".claude" / ".verified"
    marker.touch()

    rc = hooks.run_verification_invalidator(
        apply_patch_event(outside, "any/path.ts"), outside,
    )

    assert rc == 0
    assert marker.exists()


def test_session_init_clears_verified_marker(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    marker = repo / ".claude" / ".verified"
    marker.touch()

    assert hooks.run_session_init(repo) == 0

    assert not marker.exists()


def test_mark_verified_creates_marker(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    result = hooks.mark_warden("verified", "SHIP", "all claims verified", repo)
    assert result == 0
    assert (repo / ".claude" / ".verified").exists()

    verdicts = json.loads((repo / ".claude" / ".warden-verdicts.json").read_text())
    assert verdicts["verification-gate"]["verdict"] == "SHIP"


# ── _sync_atom_kinds_on_init tests ────────────────────────────────────────────

def test_sync_atom_kinds_on_init_skips_when_env_unset(tmp_path, monkeypatch):
    """No subprocess is spawned when DEUS_AUTO_MEMORY_DIR is unset."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.delenv("DEUS_AUTO_MEMORY_DIR", raising=False)

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    hooks._sync_atom_kinds_on_init(repo)

    assert calls == []


def test_sync_atom_kinds_on_init_skips_when_script_missing(tmp_path, monkeypatch):
    """No subprocess is spawned when memory_tree.py does not exist in repo."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    # Point to a real dir but with no memory_tree.py script
    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", str(tmp_path / "atoms"))
    # Ensure repo has no scripts/memory_tree.py
    # (git_repo creates a bare repo in tmp_path/repo — no scripts dir)

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    hooks._sync_atom_kinds_on_init(repo)

    assert calls == []


def test_sync_atom_kinds_on_init_skips_when_db_missing(tmp_path, monkeypatch):
    """No subprocess is spawned when the DB file does not yet exist."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    # Create a fake memory_tree.py so the script-existence check passes
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "memory_tree.py").write_text("# stub")

    atoms_dir = tmp_path / "atoms"
    atoms_dir.mkdir()
    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", str(atoms_dir))
    # Point DB to a path that does not exist
    monkeypatch.setenv("DEUS_MEMORY_TREE_DB", str(tmp_path / "nonexistent.db"))

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(args)
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    hooks._sync_atom_kinds_on_init(repo)

    assert calls == []


def test_sync_atom_kinds_on_init_reports_fixed_atoms(tmp_path, monkeypatch, capsys):
    """Stderr message emitted when sync reports stale atoms were fixed."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "memory_tree.py").write_text("# stub")

    atoms_dir = tmp_path / "atoms"
    atoms_dir.mkdir()
    db_path = tmp_path / "memory_tree.db"
    db_path.touch()

    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", str(atoms_dir))
    monkeypatch.setenv("DEUS_MEMORY_TREE_DB", str(db_path))

    fake_output = json.dumps({
        "fixed": [["stale_atom.md", "knowledge", "standard"]],
        "unchanged": 5,
        "missing_in_db": [],
        "no_kind_in_file": [],
        "read_errors": [],
    })

    class FakeResult:
        returncode = 0
        stdout = fake_output
        stderr = ""

    monkeypatch.setattr(hooks.subprocess, "run", lambda *a, **kw: FakeResult())
    hooks._sync_atom_kinds_on_init(repo)

    captured = capsys.readouterr()
    assert "stale_atom.md" in captured.err
    assert "1" in captured.err


def test_sync_atom_kinds_on_init_silent_on_subprocess_error(tmp_path, monkeypatch, capsys):
    """Subprocess failure is caught; stderr warning emitted; no exception raised."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "memory_tree.py").write_text("# stub")

    atoms_dir = tmp_path / "atoms"
    atoms_dir.mkdir()
    db_path = tmp_path / "memory_tree.db"
    db_path.touch()

    monkeypatch.setenv("DEUS_AUTO_MEMORY_DIR", str(atoms_dir))
    monkeypatch.setenv("DEUS_MEMORY_TREE_DB", str(db_path))

    def broken_run(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(hooks.subprocess, "run", broken_run)
    # Must not raise
    hooks._sync_atom_kinds_on_init(repo)

    captured = capsys.readouterr()
    assert "sync-atom-kinds failed" in captured.err


def test_run_session_init_still_clears_markers_with_sync(tmp_path, monkeypatch):
    """run_session_init returns 0 and clears markers even when sync runs."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    marker = repo / ".claude" / ".plan-reviewed"
    marker.touch()

    # Disable sync by leaving DEUS_AUTO_MEMORY_DIR unset
    monkeypatch.delenv("DEUS_AUTO_MEMORY_DIR", raising=False)

    assert hooks.run_session_init(repo) == 0
    assert not marker.exists()


# ── CI status helper (_check_ci_status) ─────────────────────────────────────


_REAL_SUBPROCESS_RUN = subprocess.run


def _make_gh_run(checks: list[dict] | None = None, returncode: int = 0, stderr: str = ""):
    """Return a fake ``subprocess.run`` that intercepts ``gh pr checks`` calls.

    All other subprocess calls (e.g. ``git init``) are forwarded to the real
    ``subprocess.run`` so that test fixtures still work correctly.
    """

    def fake_run(cmd, *args, **kwargs):
        # Intercept only ``gh pr checks`` invocations
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 3
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "pr"
            and cmd[2] == "checks"
        ):
            stdout = json.dumps(checks) if checks is not None else ""
            return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    return fake_run


_PROTECTION_NOT_PLAN_LIMITED = (
    '{"message": "Branch not protected", "status": "404"}'
)
_PROTECTION_PLAN_LIMITED = (
    '{"message": "Upgrade to GitHub Pro or make this repository public '
    'to enable this feature.", "status": "403"}'
)
_PROTECTION_AUTH_DENIED = (
    '{"message": "Resource not accessible by integration", "status": "403"}'
)


def _make_gh_run_split(
    required_checks,
    all_checks,
    *,
    required_rc=0,
    all_rc=0,
    protection_stdout=_PROTECTION_NOT_PLAN_LIMITED,
    protection_rc=1,
):
    """Fake ``subprocess.run`` returning different ``gh pr checks`` results for
    the ``--required`` query vs the unfiltered query (LIA-144 fail-closed path),
    plus a ``gh api .../protection`` intercept (defaults to a non-plan-limited
    404 so any test reaching the branch-protection probe still fails closed
    unless it opts into a plan-limited response).
    """

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 3
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "pr"
            and cmd[2] == "checks"
        ):
            if "--required" in cmd:
                payload, rc = required_checks, required_rc
            else:
                payload, rc = all_checks, all_rc
            stdout = json.dumps(payload) if payload is not None else ""
            return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 2
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "api"
        ):
            return subprocess.CompletedProcess(
                cmd, protection_rc, stdout=protection_stdout, stderr=""
            )
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    return fake_run


def test_check_ci_status_uses_required_flag(monkeypatch):
    # LIA-144: the gate must query only branch-protection-required checks.
    hooks = load_hooks()
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 3
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "pr"
            and cmd[2] == "checks"
        ):
            captured["cmd"] = list(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"bucket": "pass", "name": "ci"}]), stderr=""
            )
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    status, _ = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_GREEN
    # Strict positional assertion — --required must be passed (not just present
    # somewhere by accident), scoping the query to required checks only.
    assert captured["cmd"] == [
        "gh", "pr", "checks", "123", "--json", "bucket,name", "--required",
    ]


def test_check_ci_status_no_repo_argv_unchanged(monkeypatch):
    # Backward-compat guard: omitting `repo` must produce byte-identical argv
    # to today (no trailing --repo), independent of the assertion above.
    hooks = load_hooks()
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 3
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "pr"
            and cmd[2] == "checks"
        ):
            captured["cmd"] = list(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"bucket": "pass", "name": "ci"}]), stderr=""
            )
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    hooks._check_ci_status("123")
    assert "--repo" not in captured["cmd"]


def test_check_ci_status_explicit_repo_scopes_gh_call(monkeypatch):
    # Fixes the confirmed cross-repo bug: when the gated command carries an
    # explicit repo, the gate's own internal gh call must be scoped to it,
    # not to whatever repo the cwd's git remote happens to resolve.
    hooks = load_hooks()
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 3
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "pr"
            and cmd[2] == "checks"
        ):
            captured["cmd"] = list(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"bucket": "pass", "name": "ci"}]), stderr=""
            )
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    status, _ = hooks._check_ci_status("14", repo="owner/other-repo")
    assert status == hooks._CI_STATUS_GREEN
    assert captured["cmd"] == [
        "gh", "pr", "checks", "14", "--json", "bucket,name", "--required",
        "--repo", "owner/other-repo",
    ]


def test_check_ci_status_advisory_pending_does_not_block(monkeypatch):
    # Required checks all pass; advisory checks (TrueCourse etc.) still pending in
    # the unfiltered set. The gate sees only required → GREEN (no block).
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(
            required_checks=[{"bucket": "pass", "name": "ci"}],
            all_checks=[
                {"bucket": "pass", "name": "ci"},
                {"bucket": "pending", "name": "TrueCourse --diff vs main"},
            ],
        ),
    )
    status, _ = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_GREEN


def test_check_ci_status_required_pending_blocks(monkeypatch):
    # A pending REQUIRED check still blocks.
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(
            required_checks=[{"bucket": "pending", "name": "ci"}],
            all_checks=[{"bucket": "pending", "name": "ci"}],
            required_rc=8,
            all_rc=8,
        ),
    )
    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_PENDING
    assert "ci" in detail


def test_check_ci_status_no_required_but_checks_present_fails_closed(monkeypatch):
    # --required returns nothing, but the PR has (advisory) checks → ambiguous →
    # NO_REQUIRED, which must block (fail closed). Branch protection responds
    # with a genuine, non-plan-limited 404 (full-featured repo, no protection
    # configured) — proves the non-plan-limited ambiguous case is unaffected
    # by the plan-limited fallback added below.
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(
            required_checks=None,
            all_checks=[{"bucket": "pass", "name": "advisory-only"}],
            protection_stdout=_PROTECTION_NOT_PLAN_LIMITED,
            protection_rc=1,
        ),
    )
    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_NO_REQUIRED
    assert "none are branch-protection-required" in detail
    assert hooks._ci_block_reason("123", status, detail) is not None


def test_check_ci_status_plan_limited_all_green_becomes_green(monkeypatch):
    # Private repo without GitHub Pro: branch protection 403s in the known
    # plan-limitation shape. Unfiltered checks are all green → the gate must
    # fall back to GREEN instead of failing closed on NO_REQUIRED.
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(
            required_checks=None,
            all_checks=[{"bucket": "pass", "name": "ci"}],
            protection_stdout=_PROTECTION_PLAN_LIMITED,
            protection_rc=1,
        ),
    )
    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_GREEN
    assert "plan-limited fallback" in detail


def test_check_ci_status_plan_limited_red_still_blocks(monkeypatch):
    # Same plan-limited repo, but a real check is failing — must still block.
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(
            required_checks=None,
            all_checks=[{"bucket": "fail", "name": "ci"}],
            protection_stdout=_PROTECTION_PLAN_LIMITED,
            protection_rc=1,
        ),
    )
    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_RED
    assert "plan-limited fallback" in detail
    assert hooks._ci_block_reason("123", status, detail) is not None


def test_check_ci_status_plan_limited_pending_blocks(monkeypatch):
    # Plan-limited repo, a check still running — must still block as pending.
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(
            required_checks=None,
            all_checks=[{"bucket": "pending", "name": "ci"}],
            protection_stdout=_PROTECTION_PLAN_LIMITED,
            protection_rc=1,
        ),
    )
    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_PENDING
    assert hooks._ci_block_reason("123", status, detail) is not None


def test_check_ci_status_plan_limited_advisory_only_failure_is_excluded(monkeypatch):
    # The discriminating case: a plan-limited repo where the ONLY non-green
    # check is a known-advisory one (TrueCourse, cancelled — bucket "cancel",
    # which is in _BUCKET_FAIL). Before the advisory-exclusion fallback this
    # would incorrectly block; after it, the excluded check must not count.
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(
            required_checks=None,
            all_checks=[
                {"bucket": "cancel", "name": "TrueCourse --diff vs main"},
                {"bucket": "pass", "name": "ci"},
            ],
            protection_stdout=_PROTECTION_PLAN_LIMITED,
            protection_rc=1,
        ),
    )
    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_GREEN
    assert "plan-limited fallback" in detail


def test_check_ci_status_plan_limited_advisory_exclusion_does_not_mask_real_failure(
    monkeypatch,
):
    # @oracle: authored blind to the implementation from the ticket spec —
    # excluding a known-advisory check must never mask a genuinely failing
    # non-advisory check. A wrong GREEN here would let a merge land over red
    # CI, so this is the dangerous-direction discriminator.
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(
            required_checks=None,
            all_checks=[
                {"bucket": "cancel", "name": "TrueCourse --diff vs main"},
                {"bucket": "fail", "name": "ci"},
            ],
            protection_stdout=_PROTECTION_PLAN_LIMITED,
            protection_rc=1,
        ),
    )
    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_RED
    assert "ci" in detail


def test_check_ci_status_plan_limited_only_check_is_advisory_is_no_checks(monkeypatch):
    # A PR whose ONLY check is the excluded advisory one — filtering must not
    # produce a vacuous GREEN (set() <= _BUCKET_PASS is trivially true on an
    # empty bucket set); it must classify as NO_CHECKS instead.
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(
            required_checks=None,
            all_checks=[{"bucket": "cancel", "name": "TrueCourse --diff vs main"}],
            protection_stdout=_PROTECTION_PLAN_LIMITED,
            protection_rc=1,
        ),
    )
    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_NO_CHECKS
    assert hooks._ci_block_reason("123", status, detail) is None


def test_classify_checks_default_exclude_names_unchanged(monkeypatch):
    # Regression guard: _classify_checks with exclude_names unset must be
    # byte-identical to classifying the raw list (no filtering).
    hooks = load_hooks()
    checks = [{"bucket": "pass", "name": "ci"}, {"bucket": "fail", "name": "lint"}]
    status, message, n = hooks._classify_checks(checks)
    assert status == hooks._CI_STATUS_RED
    assert "lint" in message
    assert n == 2


def test_check_ci_status_non_matching_403_stays_fail_closed(monkeypatch):
    # A 403 that is NOT the plan-limitation shape (e.g. a real auth/permission
    # denial) must NOT be treated as plan-limited — the substring gate must
    # reject it, preserving today's fail-closed NO_REQUIRED behavior.
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(
            required_checks=None,
            all_checks=[{"bucket": "pass", "name": "ci"}],
            protection_stdout=_PROTECTION_AUTH_DENIED,
            protection_rc=1,
        ),
    )
    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_NO_REQUIRED
    assert hooks._ci_block_reason("123", status, detail) is not None


def test_branch_protection_plan_limited_probe_error_fails_closed(monkeypatch):
    # If the branch-protection probe itself can't be run (gh missing, e.g.),
    # the helper must return False so the caller keeps failing closed.
    hooks = load_hooks()

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 2
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "api"
        ):
            raise FileNotFoundError("gh not found")
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    assert hooks._branch_protection_plan_limited(None, "main") is False


def test_check_ci_status_no_checks_anywhere_does_not_block(monkeypatch):
    # Genuinely zero checks (required AND unfiltered empty) → NO_CHECKS, no block
    # (unchanged pre-LIA-144 behaviour).
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run_split(required_checks=None, all_checks=None),
    )
    status, _ = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_NO_CHECKS
    assert hooks._ci_block_reason("123", status, "") is None


def test_ci_block_reason_no_required_is_distinct_and_blocking():
    hooks = load_hooks()
    reason = hooks._ci_block_reason(
        "123",
        hooks._CI_STATUS_NO_REQUIRED,
        "2 check(s) present but none are branch-protection-required",
    )
    assert reason is not None
    assert "fail-closed" in reason
    assert "no required checks" in reason.lower()


def test_admin_merge_gate_ci_check_uses_required_scoping(tmp_path, monkeypatch):
    # The PreToolUse hook path (run_admin_merge_gate) must also scope its CI
    # check to required-only — verified by capturing the gh argv it issues.
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    command = "gh pr merge 294 --squash --admin"
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 3
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "pr"
            and cmd[2] == "checks"
        ):
            captured["cmd"] = list(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"bucket": "pass", "name": "ci"}]), stderr=""
            )
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    assert hooks.approve_admin_merge(command, repo) == 0
    rc = hooks.run_admin_merge_gate(bash_event(repo, command), repo)
    assert rc == 0
    assert "--required" in captured["cmd"]


def test_check_ci_status_green(monkeypatch):
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run([{"bucket": "pass", "name": "ci"}, {"bucket": "skipping", "name": "opt"}]),
    )

    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_GREEN
    assert "passed" in detail


def test_check_ci_status_red(monkeypatch):
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run(
            [{"bucket": "fail", "name": "test-linux"}, {"bucket": "pass", "name": "lint"}],
            returncode=1,
        ),
    )

    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_RED
    assert "test-linux" in detail


def test_check_ci_status_pending(monkeypatch):
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run(
            [{"bucket": "pending", "name": "slow-check"}, {"bucket": "pass", "name": "lint"}],
            returncode=8,
        ),
    )

    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_PENDING
    assert "slow-check" in detail


def test_check_ci_status_no_checks_empty_list(monkeypatch):
    hooks = load_hooks()
    monkeypatch.setattr(hooks.subprocess, "run", _make_gh_run([]))

    status, _ = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_NO_CHECKS


def test_check_ci_status_no_checks_empty_output(monkeypatch):
    hooks = load_hooks()
    monkeypatch.setattr(hooks.subprocess, "run", _make_gh_run(None))

    status, _ = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_NO_CHECKS


def test_check_ci_status_gh_not_found(monkeypatch):
    hooks = load_hooks()

    def raise_file_not_found(*args, **kwargs):
        raise FileNotFoundError("gh not found")

    monkeypatch.setattr(hooks.subprocess, "run", raise_file_not_found)

    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_ERROR
    assert "gh CLI not found" in detail


def test_check_ci_status_timeout(monkeypatch):
    hooks = load_hooks()

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="gh", timeout=3)

    monkeypatch.setattr(hooks.subprocess, "run", raise_timeout)

    status, detail = hooks._check_ci_status("123", timeout=3)
    assert status == hooks._CI_STATUS_ERROR
    assert "timed out" in detail


def test_check_ci_status_malformed_json(monkeypatch):
    hooks = load_hooks()

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="not-json", stderr="")

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)

    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_ERROR
    assert "unparseable" in detail


def test_check_ci_status_bad_exit_code(monkeypatch):
    hooks = load_hooks()
    monkeypatch.setattr(
        hooks.subprocess, "run", _make_gh_run(None, returncode=2, stderr="auth error")
    )

    status, detail = hooks._check_ci_status("123")
    assert status == hooks._CI_STATUS_ERROR
    assert "2" in detail


# ── _extract_pr_ref ──────────────────────────────────────────────────────────


def test_extract_pr_ref_plain_number():
    hooks = load_hooks()
    assert hooks._extract_pr_ref("gh pr merge 294 --squash --admin") == "294"


def test_extract_pr_ref_with_repo_flag():
    hooks = load_hooks()
    assert hooks._extract_pr_ref("gh --repo owner/repo pr merge 295 --admin") == "295"


def test_extract_pr_ref_with_short_repo_flag():
    hooks = load_hooks()
    assert hooks._extract_pr_ref("gh -R owner/repo pr merge 296 --squash --admin") == "296"


def test_extract_pr_ref_no_ref_returns_none():
    hooks = load_hooks()
    # --admin flag before any positional arg
    assert hooks._extract_pr_ref("gh pr merge --admin") is None


def test_extract_pr_ref_flags_before_positional():
    hooks = load_hooks()
    assert hooks._extract_pr_ref("gh pr merge --squash 294") == "294"


def test_extract_pr_ref_admin_before_positional():
    hooks = load_hooks()
    assert hooks._extract_pr_ref("gh pr merge --admin 294") == "294"


def test_extract_pr_ref_flag_with_value_before_positional():
    hooks = load_hooks()
    assert hooks._extract_pr_ref("gh pr merge -R owner/repo 295 --admin") == "295"


def test_extract_pr_ref_body_flag_before_positional():
    hooks = load_hooks()
    assert hooks._extract_pr_ref('gh pr merge --squash -b "fix: blah" 294') == "294"


# ── _extract_repo_flag ───────────────────────────────────────────────────────


def test_extract_repo_flag_global_position():
    hooks = load_hooks()
    assert (
        hooks._extract_repo_flag("gh --repo owner/repo pr merge 294 --admin")
        == "owner/repo"
    )


def test_extract_repo_flag_short_flag_global_position():
    hooks = load_hooks()
    assert (
        hooks._extract_repo_flag("gh -R owner/repo pr merge 294 --admin")
        == "owner/repo"
    )


def test_extract_repo_flag_short_flag_attached_form():
    # gh's short-flag attached form: `-Rowner/repo` (no space).
    hooks = load_hooks()
    assert (
        hooks._extract_repo_flag("gh pr merge -Rowner/repo --admin 294")
        == "owner/repo"
    )


def test_extract_repo_flag_subcommand_local_position():
    # The shape production landing commands actually use:
    # `gh pr merge --repo owner/repo --admin --squash <n>`.
    hooks = load_hooks()
    assert (
        hooks._extract_repo_flag("gh pr merge --repo owner/repo --admin --squash 294")
        == "owner/repo"
    )


def test_extract_repo_flag_equals_form():
    hooks = load_hooks()
    assert (
        hooks._extract_repo_flag("gh pr merge --repo=owner/repo --admin 294")
        == "owner/repo"
    )


def test_extract_repo_flag_absent_returns_none():
    hooks = load_hooks()
    assert hooks._extract_repo_flag("gh pr merge --admin 294") is None


def test_extract_repo_flag_duplicate_last_wins():
    # Mirrors gh's own last-flag-wins precedence for repeated flags.
    hooks = load_hooks()
    assert (
        hooks._extract_repo_flag(
            "gh --repo first/repo pr merge --repo second/repo --admin 294"
        )
        == "second/repo"
    )


def test_extract_repo_flag_does_not_misread_other_flag_values():
    # A body value that happens to look flag-like must not be misread as
    # a repo flag -- `_FLAGS_WITH_VALUE` skips it correctly.
    hooks = load_hooks()
    assert (
        hooks._extract_repo_flag('gh pr merge --admin -b "--repo" 294') is None
    )


# ── CI gate integration: run_admin_merge_gate ────────────────────────────────


def test_admin_merge_gate_blocks_when_ci_red(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run([{"bucket": "fail", "name": "ci"}], returncode=1),
    )

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh pr merge 294 --squash --admin"), repo
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "CI is red" in reason
    assert "gh pr checks 294" in reason


def test_admin_merge_gate_scopes_ci_check_to_explicit_repo(monkeypatch, tmp_path, capsys):
    # Regression test for the confirmed cross-repo bug: a `gh pr merge --repo
    # <other>` command must be graded against THAT repo's CI, not whatever the
    # worktree's own git remote happens to resolve to. Simulate the exact
    # failure mode -- CI is genuinely GREEN on the named repo but would read
    # RED if the gate ever queried unscoped -- and confirm the gate allows
    # (pre-approved) using the scoped query, never the unscoped one.
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    command = "gh pr merge --repo owner/other-repo --admin --squash 14"
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 3
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "pr"
            and cmd[2] == "checks"
        ):
            captured["cmd"] = list(cmd)
            if "--repo" in cmd:
                # Scoped query: this is the real target repo, CI is green.
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=json.dumps([{"bucket": "pass", "name": "ci"}]), stderr=""
                )
            # Unscoped query: simulates the bug -- resolves to a DIFFERENT,
            # unrelated PR whose CI is red. If the gate ever calls gh without
            # --repo here, this branch fires and the test fails loudly below.
            return subprocess.CompletedProcess(
                cmd, 1, stdout=json.dumps([{"bucket": "fail", "name": "ci"}]), stderr=""
            )
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)

    marker = repo / ".claude" / ".admin-merge-approved"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"command_hash": hooks._command_hash(command), "command": command}),
        encoding="utf-8",
    )

    rc = hooks.run_admin_merge_gate(bash_event(repo, command), repo)

    assert rc == 0
    assert not marker.exists()  # consumed -- approval matched, no denial
    assert "permissionDecision" not in capsys.readouterr().out
    assert "--repo" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "owner/other-repo"


def test_admin_merge_gate_blocks_when_ci_pending(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run([{"bucket": "pending", "name": "slow"}], returncode=8),
    )

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh pr merge 294 --squash --admin"), repo
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "CI is pending" in reason


def test_admin_merge_gate_blocks_when_ci_unverifiable(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    def raise_for_gh(cmd, *args, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 3
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "pr"
            and cmd[2] == "checks"
        ):
            raise FileNotFoundError("gh not found")
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    monkeypatch.setattr(hooks.subprocess, "run", raise_for_gh)

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh pr merge 294 --squash --admin"), repo
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "could not be verified" in reason


def test_admin_merge_gate_allows_when_ci_green_with_approval(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    command = "gh pr merge 294 --squash --admin"
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run([{"bucket": "pass", "name": "ci"}]),
    )

    marker = repo / ".claude" / ".admin-merge-approved"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"command_hash": hooks._command_hash(command), "command": command}),
        encoding="utf-8",
    )

    rc = hooks.run_admin_merge_gate(bash_event(repo, command), repo)

    assert rc == 0
    # Marker consumed, no denial
    assert not marker.exists()
    out = capsys.readouterr().out
    assert "permissionDecision" not in out


def test_admin_merge_gate_allows_when_ci_green_no_approval_still_blocks(
    monkeypatch, tmp_path, capsys
):
    """Green CI but no approval marker → still blocked (for approval), not for CI."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run([{"bucket": "pass", "name": "ci"}]),
    )

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh pr merge 294 --squash --admin"), repo
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    # Should block for approval, NOT for CI
    assert "fresh explicit approval" in reason
    assert "CI is red" not in reason
    assert "CI is pending" not in reason


def test_admin_merge_gate_allows_when_no_checks(monkeypatch, tmp_path, capsys):
    """PRs with no checks configured should not be blocked by CI gate."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    command = "gh pr merge 294 --squash --admin"
    monkeypatch.setattr(hooks.subprocess, "run", _make_gh_run([]))

    marker = repo / ".claude" / ".admin-merge-approved"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"command_hash": hooks._command_hash(command), "command": command}),
        encoding="utf-8",
    )

    rc = hooks.run_admin_merge_gate(bash_event(repo, command), repo)

    assert rc == 0
    assert not marker.exists()
    out = capsys.readouterr().out
    assert "permissionDecision" not in out


# ── CI gate integration: approve_admin_merge ─────────────────────────────────


def test_approve_admin_merge_blocked_when_ci_red(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run([{"bucket": "fail", "name": "ci"}], returncode=1),
    )

    rc = hooks.approve_admin_merge("gh pr merge 294 --squash --admin", repo)

    assert rc == 1
    assert not (repo / ".claude" / ".admin-merge-approved").exists()


def test_approve_admin_merge_succeeds_when_ci_green(monkeypatch, tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.setattr(
        hooks.subprocess,
        "run",
        _make_gh_run([{"bucket": "pass", "name": "ci"}]),
    )

    rc = hooks.approve_admin_merge("gh pr merge 294 --squash --admin", repo)

    assert rc == 0
    assert (repo / ".claude" / ".admin-merge-approved").exists()
    out = capsys.readouterr().out
    assert "Approved" in out


# --- Cold-memory injection tests ---


def _pattern_file(repo: Path, name: str, governs: list[str], body: str = "") -> None:
    patterns_dir = repo / "patterns"
    patterns_dir.mkdir(exist_ok=True)
    frontmatter = "---\ngoverns:\n" + "".join(f"  - {g}\n" for g in governs) + "---\n"
    (patterns_dir / name).write_text(frontmatter + body, encoding="utf-8")


def _reset_cold_memory_state():
    hooks = load_hooks()
    hooks._PATTERN_ROUTES_CACHE = None
    hooks._INJECTED_DOCS.clear()


def test_cold_memory_injector_injects_matching_pattern(tmp_path, capsys):
    hooks = load_hooks()
    _reset_cold_memory_state()
    repo = git_repo(tmp_path)
    _pattern_file(repo, "channel-add.md", ["src/channels"], "Channel conventions here.")
    (repo / "src" / "channels").mkdir(parents=True)
    target = repo / "src" / "channels" / "telegram.ts"
    target.write_text("export {}", encoding="utf-8")

    event = apply_patch_event(repo, "src/channels/telegram.ts")
    rc = hooks.run_cold_memory_injector(event, repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert "channel-add" in output["systemMessage"]
    assert "Channel conventions here." in output["systemMessage"]


def test_cold_memory_injector_skips_unmatched_path(tmp_path, capsys):
    hooks = load_hooks()
    _reset_cold_memory_state()
    repo = git_repo(tmp_path)
    _pattern_file(repo, "channel-add.md", ["src/channels"], "Channel conventions.")
    (repo / "scripts").mkdir()
    target = repo / "scripts" / "build.py"
    target.write_text("print('hi')", encoding="utf-8")

    event = apply_patch_event(repo, "scripts/build.py")
    rc = hooks.run_cold_memory_injector(event, repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cold_memory_injector_respects_warden_disabled(tmp_path, capsys):
    hooks = load_hooks()
    _reset_cold_memory_state()
    repo = git_repo(tmp_path)
    _pattern_file(repo, "channel-add.md", ["src/channels"], "Channel conventions.")
    (repo / "src" / "channels").mkdir(parents=True)
    (repo / "src" / "channels" / "slack.ts").write_text("", encoding="utf-8")
    wardens_dir = repo / ".claude" / "wardens"
    wardens_dir.mkdir(parents=True, exist_ok=True)
    (wardens_dir / "config.json").write_text(
        json.dumps({"cold-memory-injector": {"enabled": False}}), encoding="utf-8"
    )

    event = apply_patch_event(repo, "src/channels/slack.ts")
    rc = hooks.run_cold_memory_injector(event, repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cold_memory_injector_most_specific_first(tmp_path, capsys):
    hooks = load_hooks()
    _reset_cold_memory_state()
    repo = git_repo(tmp_path)
    _pattern_file(repo, "general-code.md", ["src/"], "General rules.")
    _pattern_file(repo, "channel-add.md", ["src/channels"], "Channel rules.")
    (repo / "src" / "channels").mkdir(parents=True)
    target = repo / "src" / "channels" / "discord.ts"
    target.write_text("", encoding="utf-8")

    event = apply_patch_event(repo, "src/channels/discord.ts")
    rc = hooks.run_cold_memory_injector(event, repo)

    assert rc == 0
    out = capsys.readouterr().out
    output = json.loads(out)
    msg = output["systemMessage"]
    channel_idx = msg.index("channel-add")
    general_idx = msg.index("general-code")
    assert channel_idx < general_idx


def test_cold_memory_injector_caps_at_char_limit(tmp_path, capsys):
    hooks = load_hooks()
    _reset_cold_memory_state()
    repo = git_repo(tmp_path)
    large_body = "x" * 4000
    _pattern_file(repo, "channel-add.md", ["src/channels"], large_body)
    _pattern_file(repo, "general-code.md", ["src/"], "General rules.")
    (repo / "src" / "channels").mkdir(parents=True)
    target = repo / "src" / "channels" / "big.ts"
    target.write_text("", encoding="utf-8")

    event = apply_patch_event(repo, "src/channels/big.ts")
    rc = hooks.run_cold_memory_injector(event, repo)

    assert rc == 0
    out = capsys.readouterr().out
    output = json.loads(out)
    assert "more pattern(s) matched but omitted" in output["systemMessage"]


# --- Structural check tests ---


def _structural_config(repo: Path, checks: list[dict]) -> None:
    cold_dir = repo / ".claude" / "cold-memory"
    cold_dir.mkdir(parents=True, exist_ok=True)
    (cold_dir / "structural-checks.json").write_text(
        json.dumps({"checks": checks}), encoding="utf-8"
    )


@pytest.mark.parametrize(
    "rel,pattern,expected",
    [
        # ``**`` matches zero or more whole segments — the cross-version bug (LIA-308):
        # full_match (3.13+) returns True for all of these; the old 3.12 ``.match`` fallback
        # returned False for the zero-segment case, silently under-matching ``**`` globs.
        ("src/main.ts", "src/**/*.ts", True),       # ** matches ZERO segments
        ("src/a/main.ts", "src/**/*.ts", True),     # ** matches one
        ("src/a/b/c/d.ts", "src/**/*.ts", True),    # ** matches many
        ("src/foo.js", "src/**/*.ts", False),       # wrong extension
        ("docs/readme.md", "src/**/*.ts", False),   # wrong root
        ("packages/mcp-foo/src/a/b.ts", "packages/mcp-*/src/**/*.ts", True),  # real config glob
        # ``*`` never crosses a path separator:
        ("main.ts", "*.ts", True),
        ("a/main.ts", "*.ts", False),
        ("src/a/main.ts", "src/*.ts", False),
        # ``**/`` zero-or-more at the root:
        ("main.ts", "**/*.ts", True),
        ("a/b/c.ts", "**/*.ts", True),
        # trailing ``**`` (zero/one/many segments):
        ("src", "src/**", False),
        ("src/a", "src/**", True),
        ("src/a/b/c", "src/**", True),
        # character classes (incl. negation) — handled, not corrupted by re.escape:
        ("src/a.ts", "src/[abc].ts", True),
        ("src/d.ts", "src/[abc].ts", False),
        ("src/x.ts", "src/[!abc].ts", True),
        ("src/a.ts", "src/[!abc].ts", False),
        ("file1.txt", "file[0-9].txt", True),
        ("fileA.txt", "file[0-9].txt", False),
        # a LEADING '^' is a glob LITERAL (only '!' negates) — must NOT act as regex negation:
        ("a.ts", "[^abc].ts", True),    # 'a' is a member of the literal class {^,a,b,c}
        ("x.ts", "[^abc].ts", False),   # 'x' is not a member
        ("^.ts", "[^abc].ts", True),    # '^' itself is a member
        # unterminated '[' is treated as a literal (no crash, no over-match):
        ("a[b.ts", "a[b.ts", True),
        ("axb.ts", "a[b.ts", False),
    ],
)
def test_glob_match_full_match_semantics_all_pythons(rel, pattern, expected):
    """Regression guard for LIA-308: _glob_match must give full_match `**` semantics on
    EVERY Python (the old impl under-matched `**` on < 3.13). These expectations equal
    PurePath.full_match's verified output and must hold regardless of the runtime version."""
    hooks = load_hooks()
    assert hooks._glob_match(rel, pattern) is expected


def test_structural_check_warns_on_pattern_match(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _structural_config(repo, [
        {"id": "no-private-import", "glob": "src/**/*.ts", "pattern": "from.*src/private", "severity": "warn", "message": "No private imports"}
    ])
    (repo / "src").mkdir()
    target = repo / "src" / "main.ts"
    target.write_text("import { x } from '../src/private/foo'", encoding="utf-8")

    event = apply_patch_event(repo, "src/main.ts")
    rc = hooks.run_structural_check(event, repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert "no-private-import" in output["systemMessage"]


def test_structural_check_silent_on_no_match(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _structural_config(repo, [
        {"id": "no-private-import", "glob": "src/**/*.ts", "pattern": "from.*src/private", "severity": "warn", "message": "No private imports"}
    ])
    (repo / "src").mkdir()
    target = repo / "src" / "clean.ts"
    target.write_text("import { x } from './utils'", encoding="utf-8")

    event = apply_patch_event(repo, "src/clean.ts")
    rc = hooks.run_structural_check(event, repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_structural_check_respects_exclude_glob(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _structural_config(repo, [
        {"id": "no-private-import", "glob": "src/**/*.ts", "exclude_glob": "src/private/**", "pattern": "from.*src/private", "severity": "warn", "message": "No private imports"}
    ])
    (repo / "src" / "private").mkdir(parents=True)
    target = repo / "src" / "private" / "internal.ts"
    target.write_text("import { x } from '../src/private/shared'", encoding="utf-8")

    event = apply_patch_event(repo, "src/private/internal.ts")
    rc = hooks.run_structural_check(event, repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_structural_check_skips_missing_config(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "file.ts").write_text("anything", encoding="utf-8")

    event = apply_patch_event(repo, "src/file.ts")
    rc = hooks.run_structural_check(event, repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_structural_check_handles_bad_regex(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.setenv("DEUS_CODEX_HOOK_DEBUG", "1")
    _structural_config(repo, [
        {"id": "bad-regex", "glob": "src/**", "pattern": "[invalid(", "severity": "warn", "message": "Bad"}
    ])
    (repo / "src").mkdir()
    (repo / "src" / "file.ts").write_text("anything", encoding="utf-8")

    event = apply_patch_event(repo, "src/file.ts")
    rc = hooks.run_structural_check(event, repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_structural_check_respects_warden_disabled(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _structural_config(repo, [
        {"id": "test", "glob": "**", "pattern": ".", "severity": "warn", "message": "Always"}
    ])
    wardens_dir = repo / ".claude" / "wardens"
    wardens_dir.mkdir(parents=True, exist_ok=True)
    (wardens_dir / "config.json").write_text(
        json.dumps({"structural-check": {"enabled": False}}), encoding="utf-8"
    )
    (repo / "src").mkdir()
    (repo / "src" / "file.ts").write_text("anything", encoding="utf-8")

    event = apply_patch_event(repo, "src/file.ts")
    rc = hooks.run_structural_check(event, repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


# --- Placement guard tests ---


def _placement_config(repo: Path, rules: list[dict]) -> None:
    cold_dir = repo / ".claude" / "cold-memory"
    cold_dir.mkdir(parents=True, exist_ok=True)
    (cold_dir / "placement-rules.json").write_text(
        json.dumps({"rules": rules}), encoding="utf-8"
    )


def test_placement_guard_warns_new_file_wrong_location(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _placement_config(repo, [
        {"id": "channel-in-packages", "path_pattern": "^src/mcp-.*\\.ts$", "message": "Channels in packages/"}
    ])

    event = {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(repo / "src" / "mcp-discord.ts")},
    }
    rc = hooks.run_placement_guard(event, repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert "channel-in-packages" in output["systemMessage"]
    assert "Channels in packages/" in output["systemMessage"]


def test_placement_guard_silent_for_existing_file(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.setenv("DEUS_CODEX_HOOK_DEBUG", "1")
    _placement_config(repo, [
        {"id": "channel-in-packages", "path_pattern": "^src/mcp-.*\\.ts$", "message": "Channels in packages/"}
    ])
    (repo / "src").mkdir()
    (repo / "src" / "mcp-discord.ts").write_text("", encoding="utf-8")

    event = {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(repo / "src" / "mcp-discord.ts")},
    }
    rc = hooks.run_placement_guard(event, repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_placement_guard_skips_missing_config(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    event = {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(repo / "src" / "mcp-foo.ts")},
    }
    rc = hooks.run_placement_guard(event, repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_placement_guard_respects_warden_disabled(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    monkeypatch.setenv("DEUS_CODEX_HOOK_DEBUG", "1")
    _placement_config(repo, [
        {"id": "test", "path_pattern": ".*", "message": "Always"}
    ])
    wardens_dir = repo / ".claude" / "wardens"
    wardens_dir.mkdir(parents=True, exist_ok=True)
    (wardens_dir / "config.json").write_text(
        json.dumps({"placement-guard": {"enabled": False}}), encoding="utf-8"
    )

    event = {
        "cwd": str(repo),
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(repo / "new-file.ts")},
    }
    rc = hooks.run_placement_guard(event, repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


# --- Routing helper tests ---


def test_load_pattern_routes_parses_governs_frontmatter(tmp_path):
    hooks = load_hooks()
    _reset_cold_memory_state()
    repo = git_repo(tmp_path)
    _pattern_file(repo, "test.md", ["src/channels", "packages/mcp-test"])

    routes = hooks._load_pattern_routes(repo)

    prefixes = [r[0] for r in routes]
    assert "src/channels" in prefixes
    assert "packages/mcp-test" in prefixes


def test_load_pattern_routes_skips_empty_governs(tmp_path):
    hooks = load_hooks()
    _reset_cold_memory_state()
    repo = git_repo(tmp_path)
    patterns_dir = repo / "patterns"
    patterns_dir.mkdir()
    (patterns_dir / "empty.md").write_text("---\ngoverns: []\n---\nBody.\n", encoding="utf-8")

    routes = hooks._load_pattern_routes(repo)

    assert routes == []


def test_load_pattern_routes_sorted_by_specificity(tmp_path):
    hooks = load_hooks()
    _reset_cold_memory_state()
    repo = git_repo(tmp_path)
    _pattern_file(repo, "general.md", ["src/"])
    _pattern_file(repo, "specific.md", ["src/channels/telegram"])

    routes = hooks._load_pattern_routes(repo)

    assert routes[0][0] == "src/channels/telegram"
    assert routes[1][0] == "src/"


def test_match_pattern_docs_returns_most_specific_first(tmp_path):
    hooks = load_hooks()
    _reset_cold_memory_state()
    repo = git_repo(tmp_path)
    _pattern_file(repo, "general.md", ["src/"], "General.")
    _pattern_file(repo, "channel.md", ["src/channels"], "Channel.")
    (repo / "src" / "channels").mkdir(parents=True)
    target = repo / "src" / "channels" / "test.ts"
    target.write_text("", encoding="utf-8")

    routes = hooks._load_pattern_routes(repo)
    matched = hooks._match_pattern_docs([target], routes, repo)

    assert len(matched) == 2
    assert matched[0].stem == "channel"
    assert matched[1].stem == "general"


# --- Worktree path resolution tests (LIA-70) ---


def git_worktree(main_repo: Path, worktree_path: Path, branch: str = "feat/wt-test") -> Path:
    """Add a git worktree at *worktree_path* from *main_repo* on a new branch."""
    # Need at least one commit for `git worktree add` to work.
    (main_repo / "README.md").write_text("init\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=main_repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "commit", "-m", "init", "--allow-empty"],
        cwd=main_repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path)],
        cwd=main_repo,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return worktree_path


def test_plan_review_gate_blocks_edits_in_worktree_when_repo_root_is_main(
    tmp_path, capsys
):
    """LIA-70: gate fires correctly when cwd is a worktree and repo_root is the main repo.

    The historical failure mode: warden-shim.sh passed the worktree path as
    --repo-root. _worktree_for_cwd then compared (worktree/.git file) against
    (common .git dir) and found them not equal, so it returned None → every
    gate silently no-oped.

    After the fix the shim derives REPO_ROOT via --git-common-dir (the shared
    .git directory parent), so --repo-root always points at the main repo and
    _worktree_for_cwd succeeds.

    This test replicates that scenario at the Python layer: it passes the
    main_repo as repo_root and the worktree as cwd, confirming the gate blocks.
    """
    hooks = load_hooks()
    main_repo = git_repo(tmp_path)
    wt_path = tmp_path / "worktree"
    git_worktree(main_repo, wt_path)

    (wt_path / "src").mkdir(exist_ok=True)
    (wt_path / "src" / "app.ts").write_text("code\n", encoding="utf-8")

    # Simulate the shim passing main_repo as repo_root (post-fix behavior).
    event = {
        "cwd": str(wt_path),
        "hook_event_name": "PreToolUse",
        "tool_name": "Edit",
        "tool_use_id": "tool",
        "tool_input": {"file_path": str(wt_path / "src" / "app.ts")},
    }

    rc = hooks.run_plan_review_gate(event, main_repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    specific = output["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "plan-reviewer" in specific["permissionDecisionReason"]


def test_marker_in_wrong_location_when_repo_root_is_worktree_path(tmp_path, capsys):
    """LIA-70: when repo_root == worktree, markers are written to the worktree, not the main repo.

    This is the actual failure mode of the broken shim: the gate still fires
    (because _worktree_for_cwd has a `top == repo_root` short-circuit), but
    _marker(repo_root, ...) writes to worktree/.claude/ instead of
    main_repo/.claude/. So the worktree session's markers are isolated from
    the main-thread session — a SHIP mark in the main session does not clear
    the worktree's gate and vice versa.

    After the fix the shim derives REPO_ROOT from --git-common-dir so both
    the main and worktree sessions share the same marker directory.
    """
    hooks = load_hooks()
    main_repo = git_repo(tmp_path)
    wt_path = tmp_path / "worktree"
    git_worktree(main_repo, wt_path)
    (wt_path / ".claude").mkdir(exist_ok=True)

    # Simulate the broken shim — worktree path passed as repo_root.
    hooks.mark_warden("plan-reviewed", "SHIP", "LIA-70 baseline test", wt_path)

    # With the broken shim, marker lands in the worktree, not the main repo.
    assert (wt_path / ".claude" / ".plan-reviewed").exists()
    # The main repo's gate state is untouched — SHIP in worktree != SHIP in main.
    assert not (main_repo / ".claude" / ".plan-reviewed").exists()


def test_marker_written_to_main_repo_not_worktree(tmp_path):
    """LIA-70: markers are written to main_repo/.claude/, not worktree/.claude/.

    Ensures _marker(repo_root, ...) resolves into the shared main repo so
    worktree agents share the same gate state as the main-thread session.
    """
    hooks = load_hooks()
    main_repo = git_repo(tmp_path)
    wt_path = tmp_path / "worktree"
    git_worktree(main_repo, wt_path)
    (wt_path / ".claude").mkdir(exist_ok=True)  # worktree also has .claude/

    # mark_warden uses _marker(repo_root, ...) internally.
    hooks.mark_warden("plan-reviewed", "SHIP", "LIA-70 test", main_repo)

    # Marker must be in the main repo.
    assert (main_repo / ".claude" / ".plan-reviewed").exists()
    # Marker must NOT be written to the worktree.
    assert not (wt_path / ".claude" / ".plan-reviewed").exists()


# ---------------------------------------------------------------------------
# mark-batch + commit window tests
# ---------------------------------------------------------------------------

def test_mark_batch_creates_all_markers(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    rc = hooks.mark_batch_wardens(
        [
            "code-reviewed:SHIP:looks good",
            "ai-eng-reviewed:SHIP:no AI issues",
            "verified:SHIP:tests pass",
        ],
        repo,
    )

    assert rc == 0
    assert (repo / ".claude" / ".code-reviewed").exists()
    assert (repo / ".claude" / ".ai-eng-reviewed").exists()
    assert (repo / ".claude" / ".verified").exists()

    verdicts = json.loads((repo / ".claude" / ".warden-verdicts.json").read_text())
    assert verdicts["code-reviewer"]["verdict"] == "SHIP"
    assert verdicts["ai-eng-warden"]["verdict"] == "SHIP"
    assert verdicts["verification-gate"]["verdict"] == "SHIP"


def test_mark_batch_opens_commit_window(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    assert not hooks._in_commit_window(repo)

    hooks.mark_batch_wardens(["code-reviewed:SHIP:looks good"], repo)

    assert hooks._in_commit_window(repo)


def test_mark_batch_rejects_unknown_marker(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    rc = hooks.mark_batch_wardens(["no-such-warden:SHIP:reason"], repo)

    assert rc != 0
    # No marker files or commit window should have been written
    assert not (repo / ".claude" / ".commit-window").exists()


def test_mark_batch_rejects_malformed_spec(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    rc = hooks.mark_batch_wardens(["code-reviewed:SHIP"], repo)  # missing reason

    assert rc != 0
    assert not (repo / ".claude" / ".code-reviewed").exists()


def test_mark_batch_atomic_on_validation_failure(tmp_path):
    """If the second spec is invalid, the first marker must NOT be written."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    rc = hooks.mark_batch_wardens(
        [
            "code-reviewed:SHIP:valid",
            "no-such-warden:SHIP:invalid",
        ],
        repo,
    )

    assert rc != 0
    assert not (repo / ".claude" / ".code-reviewed").exists()
    assert not (repo / ".claude" / ".commit-window").exists()


def test_mark_batch_blocks_trivial_after_revise(tmp_path, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)
    monkeypatch.setenv("DEUS_WARDEN_BYPASS_LOG", str(tmp_path / "bypass.jsonl"))
    monkeypatch.delenv("CLAUDE_JOB_DIR", raising=False)

    hooks._write_verdict(repo, "code-reviewer", "REVISE", "issues found", "agent")

    rc = hooks.mark_batch_wardens(["code-reviewed:TRIVIAL:quick fix"], repo)

    assert rc == 2
    assert not (repo / ".claude" / ".code-reviewed").exists()


def test_mark_batch_colon_in_reason(tmp_path):
    """Colons inside the reason field must not break parsing."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    rc = hooks.mark_batch_wardens(
        ["code-reviewed:SHIP:LIA-98: workflow improvement"],
        repo,
    )

    assert rc == 0
    assert (repo / ".claude" / ".code-reviewed").exists()


def test_commit_window_blocks_code_review_invalidator(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")
    marker = repo / ".claude" / ".code-reviewed"
    marker.touch()

    # Open a commit window
    hooks._set_commit_window(repo)

    rc = hooks.run_code_review_invalidator(apply_patch_event(repo, "src/app.ts"), repo)

    assert rc == 0
    # Marker must survive because we are inside the commit window
    assert marker.exists()


def test_commit_window_blocks_verification_invalidator(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")
    marker = repo / ".claude" / ".verified"
    marker.touch()

    hooks._set_commit_window(repo)

    rc = hooks.run_verification_invalidator(apply_patch_event(repo, "src/app.ts"), repo)

    assert rc == 0
    assert marker.exists()


def test_expired_commit_window_does_not_block_invalidator(tmp_path, monkeypatch):
    """An expired commit window (> TTL) must NOT suppress invalidation."""
    import time

    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")
    marker = repo / ".claude" / ".code-reviewed"
    marker.touch()

    hooks._set_commit_window(repo)

    # Fake the commit-window file mtime to be in the past (TTL + 1 seconds ago)
    window_path = repo / ".claude" / ".commit-window"
    past = time.time() - (hooks.COMMIT_WINDOW_TTL_SECONDS + 1)
    import os
    os.utime(window_path, (past, past))

    rc = hooks.run_code_review_invalidator(apply_patch_event(repo, "src/app.ts"), repo)

    assert rc == 0
    # Window has expired — marker should be deleted as normal
    assert not marker.exists()


def test_session_init_clears_commit_window(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / ".claude" / "wardens").mkdir(parents=True)

    hooks._set_commit_window(repo)
    assert (repo / ".claude" / ".commit-window").exists()

    hooks.run_session_init(repo)

    assert not (repo / ".claude" / ".commit-window").exists()

# ── LIA-109: JSON-based gate reads and JSON-clearing invalidation ─────────────


def test_gate_reads_from_json_when_file_absent(tmp_path, capsys):
    """run_code_review_gate allows commit when JSON has SHIP verdict, even if
    the .code-reviewed marker file does not exist."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    # Write SHIP verdict to JSON — no marker file
    hooks._write_verdict(repo, "code-reviewer", "SHIP", "all good", "mark")

    rc = hooks.run_code_review_gate(bash_event(repo, "git commit -m test"), repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_gate_blocks_when_json_absent(tmp_path, capsys):
    """run_code_review_gate blocks when both JSON and marker file are absent."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.run_code_review_gate(bash_event(repo, "git commit -m test"), repo)

    assert rc == 0
    out = capsys.readouterr().out
    output = json.loads(out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "code-reviewer" in reason


def test_gate_blocks_when_json_verdict_is_not_ship(tmp_path, capsys):
    """run_code_review_gate blocks when JSON verdict is REVISE (not SHIP)."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    hooks._write_verdict(repo, "code-reviewer", "REVISE", "issues", "agent")

    rc = hooks.run_code_review_gate(bash_event(repo, "git commit -m test"), repo)

    assert rc == 0
    out = capsys.readouterr().out
    output = json.loads(out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "REVISE" in reason


def test_verification_gate_reads_from_json_when_file_absent(tmp_path, capsys):
    """run_verification_gate allows commit when JSON has SHIP verdict, even if
    the .verified marker file does not exist."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    hooks._write_verdict(repo, "verification-gate", "SHIP", "all good", "mark")

    rc = hooks.run_verification_gate(bash_event(repo, "git commit -m test"), repo)

    assert rc == 0
    assert capsys.readouterr().out == ""


def test_invalidator_clears_json_entry_code_review(tmp_path):
    """run_code_review_invalidator removes the code-reviewer entry from
    .warden-verdicts.json on a real source file edit."""

# ---------------------------------------------------------------------------
# memo-enricher tests
# ---------------------------------------------------------------------------

def edit_event(repo: Path, path: str) -> dict:
    """Construct a PostToolUse Edit event for a given file path."""
    return {
        "cwd": str(repo),
        "hook_event_name": "PostToolUse",
        "model": "gpt-test",
        "permission_mode": "default",
        "session_id": "s",
        "tool_name": "Edit",
        "tool_use_id": "tool",
        "transcript_path": None,
        "turn_id": "turn",
        "tool_input": {"file_path": path},
    }


def test_memo_enricher_creates_memo_on_first_edit(tmp_path):
    """First Edit creates .warden-memo.md with the edited file listed."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")

    rc = hooks.run_memo_enricher(edit_event(repo, "src/app.ts"), repo)

    assert rc == 0
    memo = repo / ".claude" / ".warden-memo.md"
    assert memo.exists(), "memo file should be created after an Edit"
    content = memo.read_text(encoding="utf-8")
    assert "`src/app.ts`" in content
    assert "## Warden Memo (auto-generated)" in content
    assert "### Edited Files" in content


def test_memo_enricher_appends_not_overwrites_on_second_edit(tmp_path):
    """Second Edit appends to the existing memo rather than replacing it."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "a.ts").write_text("export const a = 1;\n", encoding="utf-8")
    (repo / "src" / "b.ts").write_text("export const b = 2;\n", encoding="utf-8")

    hooks.run_memo_enricher(edit_event(repo, "src/a.ts"), repo)
    hooks.run_memo_enricher(edit_event(repo, "src/b.ts"), repo)

    memo = repo / ".claude" / ".warden-memo.md"
    content = memo.read_text(encoding="utf-8")
    # Both files must appear in the memo.
    assert "`src/a.ts`" in content
    assert "`src/b.ts`" in content


def test_memo_enricher_deduplicates_same_file(tmp_path):
    """Editing the same file twice does not produce duplicate entries."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("export const x = 1;\n", encoding="utf-8")

    hooks.run_memo_enricher(edit_event(repo, "src/app.ts"), repo)
    hooks.run_memo_enricher(edit_event(repo, "src/app.ts"), repo)

    memo = repo / ".claude" / ".warden-memo.md"
    content = memo.read_text(encoding="utf-8")
    # The path should appear exactly once in the Edited Files section.
    assert content.count("`src/app.ts`") == 1


def test_memo_enricher_detects_ts_importers(tmp_path):
    """Import graph is populated for .ts files that are imported from src/."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    lib_dir = repo / "src" / "lib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "util.ts").write_text("export const helper = () => {};\n", encoding="utf-8")
    # caller.ts imports from the lib
    (repo / "src" / "caller.ts").write_text(
        "import { helper } from './lib/util';\n", encoding="utf-8"
    )

    rc = hooks.run_memo_enricher(edit_event(repo, "src/lib/util.ts"), repo)

    assert rc == 0
    memo = repo / ".claude" / ".warden-memo.md"
    assert memo.exists()
    content = memo.read_text(encoding="utf-8")
    # The import graph should list caller.ts as an importer of util.ts.
    assert "### Import Graph" in content
    assert "caller.ts" in content


def test_memo_enricher_detects_py_importers(tmp_path):
    """Import graph is populated for .py files imported from evolution/ or scripts/."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "mymodule.py").write_text("def do_work(): pass\n", encoding="utf-8")
    evolution_dir = repo / "evolution"
    evolution_dir.mkdir(parents=True)
    (evolution_dir / "consumer.py").write_text(
        "from scripts import mymodule\n", encoding="utf-8"
    )

    rc = hooks.run_memo_enricher(edit_event(repo, "scripts/mymodule.py"), repo)

    assert rc == 0
    memo = repo / ".claude" / ".warden-memo.md"
    content = memo.read_text(encoding="utf-8")
    assert "### Import Graph" in content
    assert "consumer.py" in content


def test_memo_enricher_no_import_graph_for_unknown_extension(tmp_path):
    """Files with unrecognised extensions get an Edited Files entry but no Import Graph."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "config.json").write_text("{}\n", encoding="utf-8")

    rc = hooks.run_memo_enricher(edit_event(repo, "src/config.json"), repo)

    assert rc == 0
    memo = repo / ".claude" / ".warden-memo.md"
    content = memo.read_text(encoding="utf-8")
    assert "`src/config.json`" in content
    assert "### Import Graph" not in content


def test_memo_enricher_noop_outside_worktree(tmp_path):
    """Event from a cwd outside any git worktree is silently ignored."""
    hooks = load_hooks()
    outside = tmp_path / "outside"
    outside.mkdir()

    rc = hooks.run_memo_enricher(edit_event(outside, "src/app.ts"), outside)

    assert rc == 0
    assert not (outside / ".claude" / ".warden-memo.md").exists()


def test_memo_enricher_apply_patch_creates_memo(tmp_path):
    """apply_patch tool input is parsed correctly to extract the edited path."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")
    hooks._write_verdict(repo, "code-reviewer", "SHIP", "all good", "mark")

    verdicts_path = repo / ".claude" / ".warden-verdicts.json"
    assert verdicts_path.exists()
    data = json.loads(verdicts_path.read_text())
    assert "code-reviewer" in data

    rc = hooks.run_code_review_invalidator(
        apply_patch_event(repo, "src/app.ts"), repo
    )

    assert rc == 0
    data_after = json.loads(verdicts_path.read_text())
    assert "code-reviewer" not in data_after


def test_invalidator_clears_json_entry_verification(tmp_path):
    """run_verification_invalidator removes the verification-gate entry from
    .warden-verdicts.json on a real source file edit."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")
    hooks._write_verdict(repo, "verification-gate", "SHIP", "all good", "mark")

    verdicts_path = repo / ".claude" / ".warden-verdicts.json"
    data = json.loads(verdicts_path.read_text())
    assert "verification-gate" in data

    rc = hooks.run_verification_invalidator(
        apply_patch_event(repo, "src/app.ts"), repo
    )

    assert rc == 0
    data_after = json.loads(verdicts_path.read_text())
    assert "verification-gate" not in data_after


def test_git_add_does_not_trigger_invalidator(tmp_path):
    """run_code_review_invalidator skips invalidation when the Bash command is
    git add — staging is not a code-editing operation."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")
    marker = repo / ".claude" / ".code-reviewed"
    marker.touch()
    hooks._write_verdict(repo, "code-reviewer", "SHIP", "all good", "mark")

    # Simulate a bash event for "git add" — not an Edit/Write but same logic path
    rc = hooks.run_code_review_invalidator(
        bash_event(repo, "git add src/app.ts"), repo
    )

    assert rc == 0
    assert marker.exists(), "marker must survive a git add event"
    data = json.loads((repo / ".claude" / ".warden-verdicts.json").read_text())
    assert "code-reviewer" in data, "JSON entry must survive a git add event"


def test_git_add_does_not_trigger_verification_invalidator(tmp_path):
    """run_verification_invalidator skips invalidation when the Bash command is
    git add — staging is not a code-editing operation."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "app.ts").write_text("old\n", encoding="utf-8")
    marker = repo / ".claude" / ".verified"
    marker.touch()
    hooks._write_verdict(repo, "verification-gate", "SHIP", "all good", "mark")

    rc = hooks.run_verification_invalidator(
        bash_event(repo, "git add src/app.ts"), repo
    )

    assert rc == 0
    assert marker.exists(), "marker must survive a git add event"
    data = json.loads((repo / ".claude" / ".warden-verdicts.json").read_text())
    assert "verification-gate" in data, "JSON entry must survive a git add event"

    rc = hooks.run_memo_enricher(apply_patch_event(repo, "src/app.ts"), repo)

    assert rc == 0
    memo = repo / ".claude" / ".warden-memo.md"
    assert memo.exists()
    content = memo.read_text(encoding="utf-8")
    assert "`src/app.ts`" in content


def test_memo_enricher_format_correct(tmp_path):
    """Memo content follows the documented format exactly."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "widget.ts").write_text("export class Widget {}\n", encoding="utf-8")

    hooks.run_memo_enricher(edit_event(repo, "src/widget.ts"), repo)

    memo = repo / ".claude" / ".warden-memo.md"
    content = memo.read_text(encoding="utf-8")
    assert "## Warden Memo (auto-generated)" in content
    assert "### Edited Files" in content
    assert "- `src/widget.ts`" in content


def test_memo_enricher_section_ordering_stable_across_multi_edit(tmp_path):
    """### Edited Files always precedes ### Import Graph after multiple Edit events.

    Regression test for the bug where a second Edit (when both section headings
    already existed) would append new Edited Files bullet lines after the
    Import Graph section instead of before it.
    """
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    src = repo / "src"
    src.mkdir()
    lib_dir = src / "lib"
    lib_dir.mkdir()

    # util.ts has an importer so it generates an Import Graph entry.
    (lib_dir / "util.ts").write_text("export const helper = () => {};\n", encoding="utf-8")
    (src / "caller.ts").write_text(
        "import { helper } from './lib/util';\n", encoding="utf-8"
    )
    # widget.ts also has an importer.
    (lib_dir / "widget.ts").write_text("export class Widget {}\n", encoding="utf-8")
    (src / "page.ts").write_text(
        "import { Widget } from './lib/widget';\n", encoding="utf-8"
    )

    # First edit: util.ts — creates both sections.
    hooks.run_memo_enricher(edit_event(repo, "src/lib/util.ts"), repo)
    # Second edit: widget.ts — must stay in Edited Files, not bleed after Import Graph.
    hooks.run_memo_enricher(edit_event(repo, "src/lib/widget.ts"), repo)

    memo = repo / ".claude" / ".warden-memo.md"
    content = memo.read_text(encoding="utf-8")

    edited_pos = content.index("### Edited Files")
    import_pos = content.index("### Import Graph")

    # Invariant: all Edited Files content precedes the Import Graph heading.
    assert edited_pos < import_pos, (
        "### Edited Files must appear before ### Import Graph"
    )

    # Both file entries must be in the Edited Files section (before Import Graph).
    edited_section = content[edited_pos:import_pos]
    assert "`src/lib/util.ts`" in edited_section, (
        "util.ts entry missing from Edited Files section"
    )
    assert "`src/lib/widget.ts`" in edited_section, (
        "widget.ts entry missing from Edited Files section — was appended after Import Graph"
    )


# ---------------------------------------------------------------------------
# Codegraph citation check (advisory) -- replaces the retired transcript-
# scanning codegraph-first gate. run_codegraph_cite_check validates the
# symbols and file:line references cited in a PLAN against the live
# codegraph index and only ever advises (no permissionDecision key).
# ---------------------------------------------------------------------------


def _cite_repo(tmp_path: Path) -> Path:
    """A git repo with a seeded ``.codegraph/codegraph.db`` (real schema
    subset, all NOT NULL columns populated) for codegraph-cite-check tests.

    Seeds: ``resolveThing`` / ``hooks.resolveThing`` in ``scripts/x.py``
    (a real 40-line file on disk, lines 10-40 attributed to the symbol),
    ``DoomLoopDetector::record`` (also in ``scripts/x.py``), and a node
    pointing at ``scripts/gone.py`` -- deliberately NOT created on disk, so
    it exercises the branch-deleted-file guard.
    """
    repo = git_repo(tmp_path)
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    (repo / "scripts" / "x.py").write_text(
        "".join(f"line {i}\n" for i in range(1, 41)), encoding="utf-8"
    )
    codegraph_dir = repo / ".codegraph"
    codegraph_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(codegraph_dir / "codegraph.db"))
    conn.execute(
        """
        CREATE TABLE nodes (
            id TEXT NOT NULL,
            kind TEXT NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            language TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            start_column INTEGER NOT NULL,
            end_column INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """
    )
    conn.executemany(
        "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language, "
        "start_line, end_line, start_column, end_column, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ("1", "function", "resolveThing", "hooks.resolveThing",
             "scripts/x.py", "python", 10, 40, 0, 0, 0),
            ("2", "method", "record", "DoomLoopDetector::record",
             "scripts/x.py", "typescript", 1, 5, 0, 0, 0),
            ("3", "function", "goneFn", "gone.goneFn",
             "scripts/gone.py", "python", 1, 5, 0, 0, 0),
        ],
    )
    conn.commit()
    conn.close()
    return repo


def _plan_event(repo: Path, plan_text: str) -> dict:
    return tool_event(repo, "ExitPlanMode", {"plan": plan_text})


def test_cite_check_validated_citation_is_silent(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, "Use `resolveThing` here."), repo)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cite_check_prose_only_nudges_with_no_hook_specific_output(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "We will improve the widget and make things better."), repo
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert out, "expected a systemMessage nudge for a plan with zero code citations"
    payload = json.loads(out)
    assert "hookSpecificOutput" not in payload, (
        "PreToolUse schema (docs/SDK_DEEP_DIVE.md) permits only permissionDecision/"
        "updatedInput under hookSpecificOutput -- this advisory must use the "
        "top-level systemMessage field instead, never hookSpecificOutput"
    )
    assert "permissionDecision" not in json.dumps(payload), (
        "codegraph-cite-check must NEVER emit permissionDecision -- that is what "
        "makes this advisory non-blocking by construction"
    )
    assert "systemMessage" in payload


def test_cite_check_names_unresolved_symbol(tmp_path, capsys):
    # frobnicateWidget is unqualified -- structurally indistinguishable from
    # a legitimate unindexed prose term, so it's detected as unresolved but
    # does not gate the user-facing nudge. Confirm detection still happened
    # via the audit log, distinct from the user-facing suppression.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "Call `frobnicateWidget` to fix it."), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == ""
    log_path = repo / ".claude" / ".warden-log"
    last_line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert "0 validated, 1 unresolved, none high-confidence" in last_line


def test_cite_check_qualified_dotted_form_passes(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, "See `hooks.resolveThing`."), repo)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cite_check_no_bare_leaf_fallback_regression(tmp_path, capsys):
    # Critical regression test: a bare-leaf fallback would let
    # NonexistentModule.resolveThing validate merely because SOME
    # `resolveThing` exists somewhere, defeating the invented-name detector.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "Call `NonexistentModule.resolveThing`."), repo
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "NonexistentModule.resolveThing" in out, (
        "a qualified citation whose qualifier doesn't match any indexed symbol "
        "must be UNRESOLVED even though the bare leaf name exists elsewhere"
    )


def test_cite_check_cross_separator_keys_both_directions(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    # DoomLoopDetector::record is stored with "::"; cited with "." must still pass.
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "See `DoomLoopDetector.record`."), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == ""

    # hooks.resolveThing is stored with "."; cited with "::" must still pass.
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "See `hooks::resolveThing`."), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cite_check_double_colon_shape_admitted(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, "See `Foo::bar`."), repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Foo::bar" in out, (
        "the identifier grammar must admit :: as a qualifier separator, not "
        "silently drop a :: token as a non-citation"
    )


@pytest.mark.parametrize(
    "raw,line,expected_resolved",
    [
        ("scripts/x.py", 20, True),
        ("scripts/x.py", 500, False),
        ("scripts/x.py", 0, False),
        ("scripts/x.py", -1, False),
        ("scripts/nope.py", 5, False),
    ],
)
def test_cite_check_file_line_resolution(tmp_path, capsys, raw, line, expected_resolved):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, f"See {raw}:{line}."), repo)
    assert rc == 0
    out = capsys.readouterr().out
    if expected_resolved:
        assert out == "", f"{raw}:{line} should have resolved silently"
    else:
        assert f"{raw}:{line}" in out, f"{raw}:{line} should be reported unresolved"


def test_cite_check_worktree_file_not_in_main_checkout_resolves(tmp_path, capsys):
    # A file present in the WORKTREE but absent from repo_root must resolve
    # against wt, not repo_root -- proves the fingerprint uses the right root.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    (repo / "only_in_worktree.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "See only_in_worktree.py:2."), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cite_check_path_traversal_rejected(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    # _FILE_LINE_RE requires an extension (path.ext:LINE); "passwd" alone has
    # none, so use a cited path shaped like the file:line candidates this
    # check actually extracts.
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "See ../../etc/passwd.conf:1."), repo
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "../../etc/passwd.conf:1" in out, "a path escaping the checkout must be unresolved, not silently allowed"


def test_cite_check_deleted_file_identifier_unresolved(tmp_path, capsys):
    # goneFn's indexed file_path (scripts/gone.py) doesn't exist under wt --
    # a symbol whose file was deleted on this branch must not validate off a
    # stale index. goneFn is unqualified, so (like the unresolved-symbol test
    # above) this doesn't reach the user-facing nudge -- confirm the
    # deleted-file guard still worked via the audit log instead.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, "Call `goneFn`."), repo)
    assert rc == 0
    assert capsys.readouterr().out == ""
    log_path = repo / ".claude" / ".warden-log"
    last_line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert "0 validated, 1 unresolved, none high-confidence" in last_line


def test_cite_check_cwd_outside_worktree_is_silent(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    foreign = tmp_path / "foreign_repo"
    foreign.mkdir()
    subprocess.run(["git", "init"], cwd=foreign, check=True, stdout=subprocess.DEVNULL)
    event = _plan_event(repo, "Call `totallyInventedSymbolXYZ`.")
    event["cwd"] = str(foreign)
    rc = hooks.run_codegraph_cite_check(event, repo)
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "an event whose cwd is neither repo_root nor one of its worktrees must "
        "be a silent no-op, even though the plan cites an obviously-invalid symbol"
    )


def test_cite_check_missing_cwd_is_silent(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    event = _plan_event(repo, "Call `totallyInventedSymbolXYZ`.")
    del event["cwd"]
    rc = hooks.run_codegraph_cite_check(event, repo)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cite_check_no_codegraph_dir_is_silent(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)  # no .codegraph/ at all
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "Call `totallyInventedSymbolXYZ`."), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cite_check_garbage_db_is_silent(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    codegraph_dir = repo / ".codegraph"
    codegraph_dir.mkdir(parents=True, exist_ok=True)
    (codegraph_dir / "codegraph.db").write_bytes(b"not a real sqlite database")
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "Call `totallyInventedSymbolXYZ`."), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "tool_name,tool_input,hook_event_name",
    [
        ("Grep", {"pattern": "foo"}, "PreToolUse"),
        ("Glob", {"pattern": "*.py"}, "PreToolUse"),
        ("Edit", {"file_path": "x.py"}, "PreToolUse"),
        ("Bash", {"command": "ls"}, "PreToolUse"),
        ("Task", {"subagent_type": "general"}, "PreToolUse"),
    ],
)
def test_cite_check_non_matching_events_are_noop(
    tmp_path, capsys, tool_name, tool_input, hook_event_name
):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    event = tool_event(repo, tool_name, tool_input)
    event["hook_event_name"] = hook_event_name
    rc = hooks.run_codegraph_cite_check(event, repo)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cite_check_alias_inert_for_post_tool_use_exit_plan_mode(tmp_path, capsys):
    # The retired "codegraph-first-gate" name is now an alias to this same
    # function. A stale worktree wiring it to PostToolUse ExitPlanMode (the
    # gate never fired on PostToolUse) must stay a silent no-op.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    event = _plan_event(repo, "Call `totallyInventedSymbolXYZ`.")
    event["hook_event_name"] = "PostToolUse"
    rc = hooks.run_codegraph_cite_check(event, repo)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cite_check_runners_alias_identity():
    hooks = load_hooks()
    assert hooks.RUNNERS["codegraph-first-gate"] is hooks.RUNNERS["codegraph-cite-check"]


def test_cite_check_stoplist_and_min_length(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, "Just `run` `it`."), repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert out, "stoplisted/too-short unqualified tokens must not count as real citations"
    assert "cites no code symbols" in out


def test_cite_check_qualified_tokens_never_stoplisted(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    conn = sqlite3.connect(str(repo / ".codegraph" / "codegraph.db"))
    conn.execute(
        "INSERT INTO nodes (id, kind, name, qualified_name, file_path, language, "
        "start_line, end_line, start_column, end_column, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("4", "function", "get", "RuntimeRegistry::get", "scripts/x.py", "typescript", 1, 2, 0, 0, 0),
    )
    conn.commit()
    conn.close()

    # A qualified token whose final segment is a stoplisted word ("get") must
    # still validate -- the qualifier disambiguates it.
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, "See `RuntimeRegistry::get`."), repo)
    assert rc == 0
    assert capsys.readouterr().out == ""

    # An unseeded qualified token must be reported UNRESOLVED BY NAME, not
    # silently dropped as though it were a stoplisted word.
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, "See `TaskRunner.run`."), repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert "TaskRunner.run" in out


def test_cite_check_long_extension_file_line_recognized(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    (repo / "setup").mkdir()
    (repo / "setup" / "com.deus.gcal-keepalive.plist.template").write_text(
        "x\ny\n", encoding="utf-8"
    )
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "See setup/com.deus.gcal-keepalive.plist.template:1."), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "a 16-char-capped extension must still be recognized as a file:line "
        "candidate, not silently dropped (which would trigger a false "
        "'cites no code symbols' nudge on a properly grounded plan)"
    )


def test_cite_check_bare_prose_words_get_the_no_symbols_nudge(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "We'll update the `config` and the `widget`."), repo
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "cites no code symbols" in out, (
        "bare lowercase prose words in backticks are not citations (shape "
        "filter) -- must get the zero-candidates nudge, not a list of bogus "
        "unresolved symbols"
    )


def test_cite_check_mixed_real_and_unqualified_invented_stays_silent(tmp_path, capsys):
    # frobnicateWidget is unqualified, so this can't be told apart from a
    # legitimate unindexed prose term -- only a qualified-identifier or
    # file:line miss is high-confidence enough to gate the nudge (see the
    # measurement note above run_codegraph_cite_check's suppression check).
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "Use `resolveThing` and then `frobnicateWidget`."), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "an unqualified miss must not gate the nudge even alongside a "
        "validated real citation"
    )


def test_cite_check_candidate_cap(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    plan = " ".join(f"`invented{i}Symbol`" for i in range(100))
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert out
    named = out.count("invented")
    assert named <= 5, "at most 5 unresolved citations should be named in the nudge"


def test_cite_check_bare_filename_fallback_validates_real_file(tmp_path, capsys):
    # A bare filename like AGENTS.md passes the identifier shape filter
    # (contains a dot) but the index only has CODE nodes -- must validate via
    # the filesystem fallback rather than reporting a real file unresolved.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    (repo / "AGENTS.md").write_text("# Agents\n", encoding="utf-8")
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, "See `AGENTS.md`."), repo)
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_cite_check_bare_filename_fallback_still_rejects_nonexistent(tmp_path, capsys):
    # The fallback must only ADD validations for files that genuinely exist --
    # a fake filename must still be reported unresolved. It's filename-shaped
    # (a `.md` extension), though, so per _looks_like_uncheckable_qualified it
    # doesn't gate the user-facing nudge either -- same pattern as the
    # unresolved-symbol tests above, confirm detection via the audit log.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "See `NONEXISTENT_FILE.md`."), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == ""
    log_path = repo / ".claude" / ".warden-log"
    last_line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert "0 validated, 1 unresolved, none high-confidence" in last_line


def test_cite_check_separate_budgets_dont_starve_file_lines(tmp_path, capsys):
    # Each category needs its own independent candidate cap -- a shared
    # budget filled identifiers-first would zero out file:line validation
    # entirely on any plan with >= _MAX_CITE_CANDIDATES identifiers. Checked
    # via the audit log's counts, not the nudge text: the message only ever
    # names the first 5 unresolved citations, so with 40 fake identifiers
    # ahead of it, "scripts/nope.py:5" is validated/counted but never
    # displayed -- asserting against `out` here could never pass regardless
    # of whether the underlying fix is correct.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    # Case transition must come BEFORE any trailing digit -- "invented0Symbol"
    # has no adjacent lower-then-upper pair (the digit breaks it) and would be
    # silently filtered out by _looks_like_symbol's shape check entirely.
    idents = " ".join(f"`inventedSymbol{i}`" for i in range(hooks._MAX_CITE_CANDIDATES))
    plan = f"{idents} See scripts/x.py:20 and scripts/nope.py:5."
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert out, "expected a nudge given genuinely unresolved citations"

    log_path = repo / ".claude" / ".warden-log"
    last_line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert "41 unresolved, 1 validated" in last_line, (
        f"expected 40 fake identifiers + scripts/nope.py:5 = 41 unresolved and "
        f"scripts/x.py:20 = 1 validated in the audit log, got: {last_line!r} -- "
        "a starved file:line budget would show 40 unresolved, 0 validated "
        "(the two real file:line citations silently dropped from extraction "
        "entirely, not just left unresolved)"
    )


def test_cite_check_silences_mostly_validated_plan_with_unqualified_misses(tmp_path, capsys):
    # A well-grounded plan legitimately cites JSON fields/DB columns/locals/
    # event names that are not code symbols and never will be indexed --
    # flagging every one as "possibly invented" is noise, not signal. Uses
    # distinct real file:line citations (identical tokens dedupe within
    # _extract_citations, so repeating one identifier would not produce
    # multiple validated candidates).
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    real_lines = " ".join(f"scripts/x.py:{n}" for n in (5, 10, 15, 20, 25, 30, 35))
    plan = f"Use `resolveThing`, and see {real_lines}. Also uses `notAnIndexedSchemaField` for config."
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "unqualified misses alone (no qualified-symbol or file:line miss) "
        "must never gate the nudge, regardless of validated ratio"
    )


def test_cite_check_silences_low_ratio_plan_with_only_unqualified_misses(tmp_path, capsys):
    # Real-world ADRs measured directly: unqualified misses (env vars, tool
    # names, DB columns, stdlib APIs) dominate even well-grounded docs, often
    # pushing the validated fraction well below any reasonable threshold --
    # a ratio-based suppression fires on ~43/44 real docs for exactly this
    # reason. Only high-confidence misses (qualified identifiers, file:line)
    # should gate the nudge -- a low ratio driven purely by unqualified misses
    # must stay silent too, not just a high one.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    plan = "Use `resolveThing`. See scripts/x.py:5. Also uses `notIndexedFieldA`, `notIndexedFieldB`, `notIndexedFieldC`."
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "1 validated / 4 unresolved = 20% validated -- would have nudged "
        "under a ratio threshold, but every miss is unqualified so it must "
        "stay silent"
    )


def test_cite_check_silences_even_when_nothing_validates_and_all_unqualified(tmp_path, capsys):
    # An unqualified miss never gates the nudge on its own, even in the
    # extreme case where NOTHING in the plan validates -- these names are
    # structurally indistinguishable from legitimate unindexed prose terms
    # (schema fields, tool names, env vars). Deliberately zero real citations
    # here (distinct from the "no code symbols at all" branch above: these
    # DO look like symbols, they just don't resolve).
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    plan = "Use `frobnicateWidget` and `anotherInventedName` and `yetAnotherWidget`."
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "zero validated citations, but every miss is unqualified -- must "
        "still stay silent, not just when partially grounded"
    )


def test_cite_check_never_hides_a_qualified_miss(tmp_path, capsys):
    # A genuinely invented QUALIFIED symbol is a concrete, checkable claim --
    # unlike an unqualified miss, which is equally consistent with "this is a
    # schema field, not a code symbol at all" -- so it must never be diluted
    # away by a pile of legitimate unqualified prose terms.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    real_lines = " ".join(f"scripts/x.py:{n}" for n in (5, 10, 15, 20, 25, 30, 35))
    plan = f"See {real_lines}. Also call `SomeModule.inventedMethod`."  # 7 validated, 1 qualified miss
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert out, (
        "7/8 = 87.5% validated, but the one miss is a QUALIFIED symbol "
        "citation -- a high-confidence signal that must never be diluted "
        "away by a high overall validated fraction"
    )
    assert "SomeModule.inventedMethod" in out


def test_cite_check_never_hides_a_stale_file_line(tmp_path, capsys):
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    plan = "Use `resolveThing`, `hooks.resolveThing`, `DoomLoopDetector.record`. See scripts/x.py:500."
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert out, (
        "3/4 = 75% validated, but the one miss is a stale file:line "
        "citation -- a filesystem-checked, concrete claim that must never "
        "be diluted away"
    )
    assert "scripts/x.py:500" in out


def test_cite_check_ignores_ipv4_host_port_as_file_line(tmp_path, capsys):
    # _FILE_LINE_RE's generic path/extension grammar also matches
    # host:port shapes (confirmed live in-repo: docs/decisions/ mentions
    # 0.0.0.0:3005). Without the host:port filter this is extracted as
    # file="0.0.0.0" line=3005, which never resolves and -- being a
    # "file:line" miss -- is unconditionally high-confidence, forcing a
    # nudge regardless of how well-grounded the rest of the plan is.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    real_lines = " ".join(f"scripts/x.py:{n}" for n in (5, 10, 15, 20, 25, 30, 35))
    plan = f"Bind the observer to 0.0.0.0:3005 and 127.0.0.1:8080. See {real_lines}."
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "0.0.0.0:3005 / 127.0.0.1:8080 must never be extracted as file:line "
        "citations -- a well-grounded plan mentioning a bind address must "
        "not be forced to nudge"
    )


def test_cite_check_ignores_dns_host_port_as_file_line(tmp_path, capsys):
    # Same false-positive class as the IPv4 case above, but for a DNS
    # hostname: api.anthropic.com:443 shape-matches file="api.anthropic.com"
    # line=443. _IPV4_LIKE_RE doesn't catch this (it isn't a dotted-quad),
    # so it needs its own TLD-based filter.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    real_lines = " ".join(f"scripts/x.py:{n}" for n in (5, 10, 15, 20, 25, 30, 35))
    plan = f"Call api.anthropic.com:443 and api.openai.com:443. See {real_lines}."
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "api.anthropic.com:443 / api.openai.com:443 must never be extracted "
        "as file:line citations"
    )


def test_cite_check_still_resolves_real_markdown_file_line(tmp_path, capsys):
    # Regression guard for the DNS-host-port fix above: `md` is a real file
    # extension (also present in _FILENAME_EXTENSION_SUFFIXES for the
    # separate identifier-qualification filter), so _looks_like_host_port
    # must NOT treat it as a TLD -- a real citation like
    # "docs/decisions/ADR-001.md:20" must still extract and validate.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "ADR-001.md").write_text(
        "".join(f"line {i}\n" for i in range(1, 30)), encoding="utf-8"
    )
    rc = hooks.run_codegraph_cite_check(
        _plan_event(repo, "See docs/ADR-001.md:20."), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "a real markdown file:line citation must still validate cleanly, "
        "not be swallowed by the host:port filter"
    )


def test_cite_check_host_port_extraction_helper(tmp_path):
    hooks = load_hooks()
    assert hooks._looks_like_host_port("0.0.0.0") is True
    assert hooks._looks_like_host_port("127.0.0.1") is True
    assert hooks._looks_like_host_port("scripts/x.py") is False
    assert hooks._looks_like_host_port("AGENTS.md") is False
    assert hooks._looks_like_host_port("setup/com.deus.gcal-keepalive.plist.template") is False


def test_cite_check_uncheckable_qualified_helper(tmp_path):
    hooks = load_hooks()
    # Filename-shaped -- measured real false positives.
    assert hooks._looks_like_uncheckable_qualified("MEMORY_TREE.md") is True
    assert hooks._looks_like_uncheckable_qualified("hooks.json") is True
    assert hooks._looks_like_uncheckable_qualified("com.deus.plist") is True
    assert hooks._looks_like_uncheckable_qualified("llama.cpp") is True
    # Stdlib/builtin namespace roots -- measured real false positives.
    assert hooks._looks_like_uncheckable_qualified("os.replace") is True
    assert hooks._looks_like_uncheckable_qualified("Promise.all") is True
    assert hooks._looks_like_uncheckable_qualified("datetime.now") is True
    assert hooks._looks_like_uncheckable_qualified("std::process::Child::kill") is True
    assert hooks._looks_like_uncheckable_qualified("mpsc::channel") is True
    # Genuinely checkable qualified symbols must NOT be excluded -- this is
    # the exact dilution risk ai-eng-warden has caught before.
    assert hooks._looks_like_uncheckable_qualified("SomeModule.inventedMethod") is False
    assert hooks._looks_like_uncheckable_qualified("WardenRegistry.resolveBackendShim") is False
    assert hooks._looks_like_uncheckable_qualified("DoomLoopDetector.record") is False
    # Unqualified tokens are out of scope for this helper (already excluded
    # upstream by the `len(segments) > 1` check at the call site).
    assert hooks._looks_like_uncheckable_qualified("resolveThing") is False


def test_cite_check_silences_stdlib_and_filename_qualified_misses(tmp_path, capsys):
    # Live end-to-end regression for the second dogfooding round: a
    # well-grounded plan whose ONLY misses are stdlib/filename-shaped
    # qualified tokens must stay silent, not nudge on grounds that don't
    # actually distinguish real usage from invention.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    real_lines = " ".join(f"scripts/x.py:{n}" for n in (5, 10, 15, 20, 25, 30, 35))
    plan = (
        f"See {real_lines}. Uses `os.replace`, `Promise.all`, and cites "
        "`MEMORY_TREE.md` and `hooks.json` for context."
    )
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "stdlib-namespace and filename-shaped qualified misses must not gate "
        "the nudge -- they are structurally unresolvable regardless of "
        "whether they're real or invented"
    )


def test_cite_check_still_nudges_on_invented_qualified_symbol_amid_stdlib_noise(
    tmp_path, capsys
):
    # The true-positive guarantee: a genuinely invented qualified symbol must
    # still nudge even surrounded by legitimate stdlib/filename citations
    # that are individually excluded -- exclusion is per-token, not
    # ratio-based, so it can't dilute away a real invented symbol.
    hooks = load_hooks()
    repo = _cite_repo(tmp_path)
    plan = (
        "Uses `os.replace` and `Promise.all` and cites `MEMORY_TREE.md`. "
        "Also call `WardenRegistry.resolveBackendShim` to fix it."
    )
    rc = hooks.run_codegraph_cite_check(_plan_event(repo, plan), repo)
    assert rc == 0
    out = capsys.readouterr().out
    assert out, (
        "a genuinely invented qualified symbol must still nudge even amid "
        "stdlib/filename-shaped noise that's individually excluded from "
        "GATING the decision (each is still named in the message body -- "
        "exclusion only means it can't trigger the nudge on its own)"
    )
    assert "WardenRegistry.resolveBackendShim" in out


def _live_db_main_checkout() -> Path:
    # .codegraph/ only exists in the MAIN checkout (never a linked worktree --
    # same architecture check_codegraph_db_schema's _main_checkout_root
    # handles), so this test's own file location must be resolved the same
    # way, not assumed to already BE the main checkout.
    import drift_check
    return drift_check._main_checkout_root(Path(__file__).resolve().parents[2])


@pytest.mark.skipif(
    not (_live_db_main_checkout() / ".codegraph" / "codegraph.db").is_file(),
    reason="requires a real ~/deus/.codegraph/codegraph.db (absent in CI / fresh installs)",
)
def test_cite_check_live_db_integration(capsys):
    # Pins the schema assumption (_validate_identifiers' query shape) to
    # reality against the REAL index, alongside check_codegraph_db_schema.
    #
    # Deliberately does NOT copy the DB into a synthetic scratch repo: a
    # validated identifier also requires its indexed file_path to EXIST under
    # the worktree (the branch-deleted-file guard), so an empty scratch repo
    # with only the DB copied in would report every real symbol unresolved.
    # Runs directly against the real main checkout instead, where the
    # indexed file_path values genuinely exist on disk.
    hooks = load_hooks()
    main_repo = _live_db_main_checkout()
    rc = hooks.run_codegraph_cite_check(
        _plan_event(main_repo, "See `run_plan_review_gate`."), main_repo
    )
    assert rc == 0
    assert capsys.readouterr().out == "", (
        "a citation of a symbol genuinely present in this repo's own codebase "
        "must validate against the real, current index"
    )


def test_cite_check_hook_specs_wired_for_exit_plan_mode():
    hooks = load_hooks()
    matches = [
        spec for spec in hooks.HOOK_SPECS
        if spec.behavior == "codegraph-cite-check"
    ]
    assert len(matches) == 1
    assert matches[0].event == "PreToolUse"
    assert matches[0].matcher == "ExitPlanMode"
    assert not any(spec.behavior == "codegraph-first-gate" for spec in hooks.HOOK_SPECS), (
        "the retired gate must not have its own HOOK_SPECS entry -- only the "
        "RUNNERS alias exists, for stale-wiring compatibility"
    )


def test_settings_json_wires_codegraph_cite_check_not_search_blocking():
    settings_path = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    pre_tool_use = settings["hooks"]["PreToolUse"]

    cite_check_matchers = [
        block["matcher"] for block in pre_tool_use
        if any("codegraph-cite-check" in h["command"] for h in block["hooks"])
    ]
    assert cite_check_matchers, "codegraph-cite-check must be wired under PreToolUse"
    assert all("ExitPlanMode" in m for m in cite_check_matchers)

    for block in pre_tool_use:
        matcher = block.get("matcher", "")
        if matcher in ("Grep|Glob", "Bash") or matcher == "Grep|Glob|Bash":
            for h in block["hooks"]:
                assert "codegraph-first-gate" not in h["command"], (
                    f"stale codegraph-first-gate search-blocking wiring found under "
                    f"matcher {matcher!r}"
                )

    agents_dir = Path(__file__).resolve().parents[2] / ".claude" / "agents"
    for agent_file in agents_dir.glob("*.md"):
        text = agent_file.read_text(encoding="utf-8")
        assert "codegraph-first-gate" not in text or "HISTORICAL" in text, (
            f"{agent_file.name} references codegraph-first-gate outside a "
            "clearly-labeled historical example"
        )
        assert "codegraph_gated" not in text, (
            f"{agent_file.name} still has the retired codegraph_gated flag"
        )


# ---------------------------------------------------------------------------
# Per-worktree gate isolation (markers + verdict store)
# ---------------------------------------------------------------------------
# load_hooks() re-execs the module each call, so _WORKTREE_OVERRIDE /
# _WORKTREE_CACHE start fresh per test — no cross-test leakage.

def test_marker_and_verdict_isolated_across_worktrees(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    wt_a = tmp_path / "wt-a"; wt_a.mkdir()
    wt_b = tmp_path / "wt-b"; wt_b.mkdir()

    hooks._WORKTREE_OVERRIDE = wt_a
    marker_a = hooks._marker(repo, ".plan-reviewed")
    verdict_a = hooks._verdicts_path(repo)
    hooks._WORKTREE_OVERRIDE = wt_b
    marker_b = hooks._marker(repo, ".plan-reviewed")
    verdict_b = hooks._verdicts_path(repo)

    assert marker_a != marker_b
    assert verdict_a != verdict_b
    assert "worktree-markers" in str(marker_a)
    assert marker_a.name == ".plan-reviewed"
    assert verdict_a.name == ".warden-verdicts.json"


def test_main_repo_keeps_flat_paths(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    # worktree == repo_root → flat paths (back-compat).
    hooks._WORKTREE_OVERRIDE = repo
    assert hooks._marker(repo, ".plan-reviewed") == repo / ".claude" / ".plan-reviewed"
    assert hooks._verdicts_path(repo) == repo / ".claude" / ".warden-verdicts.json"


def test_non_gate_markers_stay_global_in_worktree(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    wt = tmp_path / "wt"; wt.mkdir()
    hooks._WORKTREE_OVERRIDE = wt
    # Excluded-from-namespacing markers resolve flat even inside a worktree.
    for name in (".migration-nudged", ".admin-merge-approved",
                 ".plan-scope.md", ".warden-memo.md"):
        assert hooks._marker(repo, name) == repo / ".claude" / name
    # ...but a gate marker IS namespaced in the same worktree.
    assert "worktree-markers" in str(hooks._marker(repo, ".plan-reviewed"))


def test_override_consistent_between_marker_and_verdict(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    wt = tmp_path / "wt"; wt.mkdir()
    hooks._WORKTREE_OVERRIDE = wt
    # marker + verdict for one worktree share the same bucket dir.
    assert hooks._marker(repo, ".plan-reviewed").parent == hooks._verdicts_path(repo).parent


def test_verdict_write_read_isolated_by_worktree(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    wt_a = tmp_path / "wt-a"; wt_a.mkdir()
    wt_b = tmp_path / "wt-b"; wt_b.mkdir()

    hooks._WORKTREE_OVERRIDE = wt_a
    hooks._write_verdict(repo, "code-reviewer", "SHIP", "isolation test")
    assert hooks._read_verdicts(repo).get("code-reviewer", {}).get("verdict") == "SHIP"

    # A different worktree must NOT see worktree-A's verdict.
    hooks._WORKTREE_OVERRIDE = wt_b
    assert "code-reviewer" not in hooks._read_verdicts(repo)


def test_cwd_derive_used_when_no_override(tmp_path, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    wt = tmp_path / "wt"; wt.mkdir()
    assert hooks._WORKTREE_OVERRIDE is None
    hooks._WORKTREE_CACHE.clear()  # ensure the monkeypatched resolver is consulted
    monkeypatch.setattr(hooks, "_worktree_for_cwd", lambda cwd, rr: wt)
    assert "worktree-markers" in str(hooks._marker(repo, ".plan-reviewed"))


def test_current_worktree_is_cached(tmp_path, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    calls = []
    monkeypatch.setattr(hooks, "_worktree_for_cwd",
                        lambda cwd, rr: calls.append(1) or rr)
    hooks._current_worktree(repo)
    hooks._current_worktree(repo)
    assert len(calls) == 1  # resolved once, then served from _WORKTREE_CACHE


def test_mark_cli_worktree_root_writes_namespaced(tmp_path):
    # End-to-end CLI path: main() -> _with_cli_worktree -> namespaced marker.
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    wt = tmp_path / "wt"; wt.mkdir()
    rc = hooks.main([
        "mark", "plan-reviewed", "SHIP", "via cli",
        "--repo-root", str(repo), "--worktree-root", str(wt),
    ])
    assert rc == 0
    # The flat (main-repo) marker must NOT be written...
    assert not (repo / ".claude" / ".plan-reviewed").exists()
    # ...the worktree bucket marker must be.
    hooks._WORKTREE_OVERRIDE = wt
    assert hooks._marker(repo, ".plan-reviewed").exists()
    # main() restored the override after the call.
    assert hooks.main is not None  # sanity; override reset happens in finally


# ── Admin-merge standing autonomy grant (#9a) ───────────────────────────────

import datetime as _dt  # noqa: E402  (section-local; mirrors hook's dt usage)


def _commit_repo(repo: Path) -> None:
    """Give the repo a born HEAD so `rev-parse --abbrev-ref HEAD` resolves."""
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _enable_standing(repo: Path, expiry_hours=24) -> None:
    wardens = repo / ".claude" / "wardens"
    wardens.mkdir(parents=True, exist_ok=True)
    (wardens / "config.json").write_text(
        json.dumps(
            {"admin-merge-gate": {"standing_grant": {"enabled": True, "expiry_hours": expiry_hours}}}
        ),
        encoding="utf-8",
    )


def _utc_iso(offset_hours: float = 0.0) -> str:
    return (
        _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=offset_hours)
    ).isoformat()


def _write_standing_marker(repo: Path, created_at: str, worktree_root: Path | None = None) -> Path:
    marker = repo / ".claude" / ".admin-merge-standing"
    marker.write_text(
        json.dumps({"worktree_root": str(worktree_root or repo), "created_at": created_at}),
        encoding="utf-8",
    )
    return marker


def _write_verdicts(repo: Path, verdicts: dict) -> None:
    data = {k: {"verdict": v, "ts": "t", "reason": "r", "source": "test"} for k, v in verdicts.items()}
    (repo / ".claude" / ".warden-verdicts.json").write_text(json.dumps(data), encoding="utf-8")


def _green_ci(hooks, monkeypatch) -> None:
    monkeypatch.setattr(hooks, "_check_ci_status", lambda *a, **k: (hooks._CI_STATUS_GREEN, "ok"))


def test_standing_grant_allows_when_ci_green_branch_match_verdicts_ship(
    tmp_path, capsys, monkeypatch
):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _commit_repo(repo)
    _green_ci(hooks, monkeypatch)
    _enable_standing(repo)
    marker = _write_standing_marker(repo, _utc_iso())
    _write_verdicts(repo, {"code-reviewer": "SHIP", "verification-gate": "SHIP"})

    rc = hooks.run_admin_merge_gate(bash_event(repo, "gh pr merge --admin"), repo)

    assert rc == 0
    assert capsys.readouterr().out == ""  # allowed: no deny JSON, no approval prompt
    assert marker.exists()  # standing grant is NOT consumed on use


def test_standing_grant_blocks_on_conditional_revise(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _commit_repo(repo)
    _green_ci(hooks, monkeypatch)
    _enable_standing(repo)
    marker = _write_standing_marker(repo, _utc_iso())
    _write_verdicts(
        repo,
        {"code-reviewer": "SHIP", "verification-gate": "SHIP", "ai-eng-warden": "REVISE"},
    )

    rc = hooks.run_admin_merge_gate(bash_event(repo, "gh pr merge --admin"), repo)

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "ai-eng-warden" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert marker.exists()  # not consumed — fix the warden and retry within the window


def test_standing_grant_blocks_on_missing_mandatory_verdict(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _commit_repo(repo)
    _green_ci(hooks, monkeypatch)
    _enable_standing(repo)
    _write_standing_marker(repo, _utc_iso())
    _write_verdicts(repo, {"code-reviewer": "SHIP"})  # verification-gate absent

    rc = hooks.run_admin_merge_gate(bash_event(repo, "gh pr merge --admin"), repo)

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "verification-gate" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_standing_grant_blocks_and_consumes_when_expired(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _commit_repo(repo)
    _green_ci(hooks, monkeypatch)
    _enable_standing(repo, expiry_hours=24)
    marker = _write_standing_marker(repo, _utc_iso(offset_hours=-48))
    _write_verdicts(repo, {"code-reviewer": "SHIP", "verification-gate": "SHIP"})

    rc = hooks.run_admin_merge_gate(bash_event(repo, "gh pr merge --admin"), repo)

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "expired" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert not marker.exists()  # expired marker is consumed


def test_standing_grant_ci_red_blocks_before_grant(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _commit_repo(repo)
    monkeypatch.setattr(hooks, "_check_ci_status", lambda *a, **k: (hooks._CI_STATUS_RED, "boom"))
    _enable_standing(repo)
    marker = _write_standing_marker(repo, _utc_iso())
    _write_verdicts(repo, {"code-reviewer": "SHIP", "verification-gate": "SHIP"})

    rc = hooks.run_admin_merge_gate(bash_event(repo, "gh pr merge --admin"), repo)

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "CI is red" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert marker.exists()  # CI gate fires before the standing block; marker untouched


def test_standing_grant_toggle_off_falls_through_to_one_shot(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _commit_repo(repo)
    _green_ci(hooks, monkeypatch)
    # No config => toggle off. A leftover standing marker must be ignored.
    marker = _write_standing_marker(repo, _utc_iso())
    _write_verdicts(repo, {"code-reviewer": "SHIP", "verification-gate": "SHIP"})

    rc = hooks.run_admin_merge_gate(bash_event(repo, "gh pr merge --admin"), repo)

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "fresh explicit approval" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert marker.exists()  # toggle off => standing logic skipped, marker untouched


def test_standing_grant_branch_mismatch_falls_through(tmp_path, capsys, monkeypatch):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _commit_repo(repo)
    _green_ci(hooks, monkeypatch)
    monkeypatch.setattr(hooks, "_gh_pr_head_branch", lambda *a, **k: "some-other-branch")
    _enable_standing(repo)
    marker = _write_standing_marker(repo, _utc_iso())
    _write_verdicts(repo, {"code-reviewer": "SHIP", "verification-gate": "SHIP"})

    # Explicit foreign PR ref => resolves to a branch != this worktree's branch.
    rc = hooks.run_admin_merge_gate(bash_event(repo, "gh pr merge 999 --admin"), repo)

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert "fresh explicit approval" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert marker.exists()  # mismatch => one-shot path, standing marker preserved


def test_approve_standing_without_toggle_prints_stanza(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.approve_admin_merge_standing(repo, repo)

    assert rc == 1
    err = capsys.readouterr().err
    assert "standing_grant" in err and "enabled" in err
    assert not (repo / ".claude" / ".admin-merge-standing").exists()


def test_approve_standing_with_toggle_writes_marker(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _enable_standing(repo)

    rc = hooks.approve_admin_merge_standing(repo, repo)

    assert rc == 0
    marker = repo / ".claude" / ".admin-merge-standing"
    assert marker.exists()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["worktree_root"] == str(repo)
    assert hooks._parse_iso_utc(data["created_at"]) is not None


def test_approve_admin_merge_requires_command_without_standing(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.main(["approve-admin-merge", "--repo-root", str(repo)])

    assert rc == 2
    assert "--command is required" in capsys.readouterr().err


def test_standing_grant_config_clamps_and_guards(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    # Absent config => fail-safe disabled.
    assert hooks._standing_grant_config(repo) == (False, 24.0)

    wardens = repo / ".claude" / "wardens"
    wardens.mkdir(parents=True)

    def cfg(val):
        (wardens / "config.json").write_text(
            json.dumps(
                {"admin-merge-gate": {"standing_grant": {"enabled": True, "expiry_hours": val}}}
            ),
            encoding="utf-8",
        )
        return hooks._standing_grant_config(repo)

    assert cfg(100000) == (True, 168.0)  # clamped to max
    assert cfg(-5) == (True, 0.0)  # clamped to 0 (always-expired)
    assert cfg(True) == (True, 24.0)  # bool rejected -> default
    assert cfg("nope") == (True, 24.0)  # non-numeric rejected -> default


def test_standing_marker_not_cleared_by_session_init(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    marker = _write_standing_marker(repo, _utc_iso())

    assert hooks.run_session_init(repo) == 0

    assert marker.exists()  # bounded by expiry, not session lifetime


def test_pr_matches_worktree_fails_safe_on_unborn_head(tmp_path):
    # git_repo has no commits -> `git rev-parse --abbrev-ref HEAD` errors ->
    # _git returns None -> the guard must NOT silently report a match.
    hooks = load_hooks()
    repo = git_repo(tmp_path)  # no _commit_repo: HEAD is unborn

    matched, reason = hooks._pr_matches_worktree("gh pr merge --admin", repo)

    assert matched is False
    assert "worktree branch" in reason


def test_pr_matches_worktree_no_ref_matches_current_branch(tmp_path):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _commit_repo(repo)  # born HEAD

    matched, _ = hooks._pr_matches_worktree("gh pr merge --admin", repo)

    assert matched is True  # no explicit ref => current branch => this worktree's PR


def test_gh_pr_head_branch_no_repo_argv_unchanged(monkeypatch):
    hooks = load_hooks()
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"headRefName": "some-branch"}), stderr=""
        )

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    head = hooks._gh_pr_head_branch("294")
    assert head == "some-branch"
    assert captured["cmd"] == ["gh", "pr", "view", "294", "--json", "headRefName"]


def test_gh_pr_head_branch_explicit_repo_scopes_gh_call(monkeypatch):
    hooks = load_hooks()
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = list(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"headRefName": "lia-410-branch"}), stderr=""
        )

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    head = hooks._gh_pr_head_branch("14", repo="owner/other-repo")
    assert head == "lia-410-branch"
    assert captured["cmd"] == [
        "gh", "pr", "view", "14", "--json", "headRefName",
        "--repo", "owner/other-repo",
    ]


def test_pr_matches_worktree_threads_explicit_repo_to_gh_pr_view(monkeypatch, tmp_path):
    # End-to-end: a `gh pr merge --repo o/r <ref>` command must scope the
    # PR-head-branch lookup to that same repo, not the worktree's own remote.
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    _commit_repo(repo)
    captured = {}

    def fake_run(cmd, *args, **kwargs):
        if (
            isinstance(cmd, (list, tuple))
            and len(cmd) >= 3
            and str(cmd[0]).endswith("gh")
            and cmd[1] == "pr"
            and cmd[2] == "view"
        ):
            captured["cmd"] = list(cmd)
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"headRefName": "other-branch"}), stderr=""
            )
        return _REAL_SUBPROCESS_RUN(cmd, *args, **kwargs)

    monkeypatch.setattr(hooks.subprocess, "run", fake_run)
    matched, _ = hooks._pr_matches_worktree(
        "gh pr merge --repo owner/other-repo --admin 999", repo
    )
    assert matched is False  # head branch differs from this worktree's branch
    assert "--repo" in captured["cmd"]
    assert captured["cmd"][captured["cmd"].index("--repo") + 1] == "owner/other-repo"


def test_block_message_diagnoses_bucket_mismatch(tmp_path):
    """A 'not run yet' model backend whose SHIP sits in a SIBLING bucket gets a
    pointer to that bucket instead of a silent retry."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    sibling = repo / ".claude" / "worktree-markers" / "deadbeef0001"
    sibling.mkdir(parents=True)
    (sibling / ".warden-verdicts.json").write_text(
        json.dumps(
            {"code-reviewer@gpt": {"verdict": "SHIP", "ts": "t", "reason": "r"}}
        )
    )

    msg = hooks._warden_backends_block_message("code-reviewer", [("gpt", None)], repo)

    assert "co-gate bucket mismatch:" in msg
    assert "code-reviewer@gpt" in msg
    assert str(sibling) in msg


def test_block_message_no_mismatch_when_verdict_absent_everywhere(tmp_path):
    """No SHIP anywhere -> plain 'not run yet', no false-positive diagnostic."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    msg = hooks._warden_backends_block_message("code-reviewer", [("gpt", None)], repo)

    assert "not run yet" in msg
    assert "co-gate bucket mismatch:" not in msg


def test_buckets_with_ship_excludes_current_bucket(tmp_path):
    """A SHIP in the gate's OWN bucket is never reported as a sibling mismatch."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    current = repo / ".claude"  # the excluded (gate's own) bucket
    (current / ".warden-verdicts.json").write_text(
        json.dumps({"code-reviewer@gpt": {"verdict": "SHIP", "ts": "t"}})
    )

    assert hooks._buckets_with_ship("code-reviewer", "gpt", repo, current) == []
