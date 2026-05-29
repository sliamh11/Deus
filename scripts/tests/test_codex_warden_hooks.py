from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path


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


def test_admin_merge_gate_blocks_without_exact_approval(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh pr merge 294 --squash --admin"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "fresh explicit approval" in reason
    assert "approve-admin-merge" in reason


def test_admin_merge_gate_blocks_with_gh_global_repo_flag(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh --repo owner/repo pr merge 294 --squash --admin"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "fresh explicit approval" in reason


def test_admin_merge_gate_blocks_with_gh_short_repo_flag(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh -R owner/repo pr merge 294 --squash --admin"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_admin_merge_gate_blocks_equals_form_admin_flag(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "gh pr merge 294 --squash --admin=true"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_admin_merge_gate_blocks_absolute_gh_path(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

    rc = hooks.run_admin_merge_gate(
        bash_event(repo, "/opt/homebrew/bin/gh pr merge 294 --squash --admin=true"),
        repo,
    )

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_admin_merge_gate_blocks_windows_gh_exe_path(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

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
    tmp_path, capsys
):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    command = "gh pr merge 294 --squash --admin"

    assert hooks.approve_admin_merge(command, repo) == 0
    assert (repo / ".claude" / ".admin-merge-approved").exists()
    rc = hooks.run_admin_merge_gate(bash_event(repo, command), repo)

    assert rc == 0
    assert (repo / ".claude" / ".admin-merge-approved").exists() is False
    output = capsys.readouterr().out
    assert "Approved one admin merge command" in output
    assert "permissionDecision" not in output


def test_admin_merge_gate_rejects_stale_marker_for_different_command(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)

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
# Codegraph-first gate (LIA-121 / RETRO-2026-05-29-01)
# ---------------------------------------------------------------------------
#
# Implementation: transcript-scanning (replaces broken marker scheme).
# The gate reads the agent's transcript JSONL at Grep/Glob/Bash-search hook
# time and checks for a prior codegraph tool_use.
# The shared predicate is _line_is_codegraph_toolcall in codex_warden_hooks.py.

_MCP_CODEGRAPH_LINE = json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "id": "t", "name": "mcp__codegraph__codegraph_context", "input": {"task": "x"}}]},
})
_TOOLSEARCH_CODEGRAPH_LINE = json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "id": "t", "name": "ToolSearch", "input": {"query": "select:mcp__codegraph__codegraph_context"}}]},
})
_BASH_LINE = json.dumps({
    "type": "assistant",
    "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "ls"}}]},
})
_USER_RESULT_LINE = json.dumps({
    "type": "user",
    "message": {"content": [{"type": "tool_result", "content": "codegraph gate denied"}]},
})


def _write_transcript(tmp_path: Path, lines: list[str]) -> Path:
    """Write a fake transcript JSONL file and return its path."""
    p = tmp_path / "transcript.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def _gate_event(repo, tool_name, tool_input=None, transcript=None):
    if transcript is None:
        transcript = "/nonexistent/no-transcript.jsonl"
    event = tool_event(repo, tool_name, tool_input)
    event["transcript_path"] = str(transcript)
    return event


def _deny(capsys):
    return json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"]


def test_codegraph_gate_blocks_grep_without_prior_call(tmp_path, capsys):
    """Grep is denied when transcript has no prior codegraph tool_use."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    # Transcript exists but contains only non-codegraph lines.
    tr = _write_transcript(tmp_path, [_BASH_LINE])
    rc = hooks.run_codegraph_first_gate(_gate_event(repo, "Grep", transcript=tr), repo)
    assert rc == 0
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "codegraph" in out["permissionDecisionReason"].lower()


def test_codegraph_gate_blocks_glob_without_prior_call(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    tr = _write_transcript(tmp_path, [_BASH_LINE])
    assert hooks.run_codegraph_first_gate(_gate_event(repo, "Glob", transcript=tr), repo) == 0
    assert _deny(capsys) == "deny"


def test_codegraph_gate_blocks_bash_grep(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    tr = _write_transcript(tmp_path, [_BASH_LINE])
    rc = hooks.run_codegraph_first_gate(
        _gate_event(repo, "Bash", {"command": "grep -r foo src/"}, transcript=tr), repo
    )
    assert rc == 0
    assert _deny(capsys) == "deny"


def test_codegraph_gate_blocks_git_grep(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    tr = _write_transcript(tmp_path, [_BASH_LINE])
    rc = hooks.run_codegraph_first_gate(
        _gate_event(repo, "Bash", {"command": "git grep foo"}, transcript=tr), repo
    )
    assert rc == 0
    assert _deny(capsys) == "deny"


def test_codegraph_gate_blocks_env_prefixed_grep(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    tr = _write_transcript(tmp_path, [_BASH_LINE])
    rc = hooks.run_codegraph_first_gate(
        _gate_event(repo, "Bash", {"command": "FOO=bar grep -r x ."}, transcript=tr), repo
    )
    assert rc == 0
    assert _deny(capsys) == "deny"


def test_codegraph_gate_allows_piped_grep(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    tr = _write_transcript(tmp_path, [_BASH_LINE])
    rc = hooks.run_codegraph_first_gate(
        _gate_event(repo, "Bash", {"command": "ls | grep foo"}, transcript=tr), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_codegraph_gate_allows_non_search_bash(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    tr = _write_transcript(tmp_path, [_BASH_LINE])
    rc = hooks.run_codegraph_first_gate(
        _gate_event(repo, "Bash", {"command": "FOO=bar npm test"}, transcript=tr), repo
    )
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_codegraph_gate_ignores_non_search_tools(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    tr = _write_transcript(tmp_path, [_BASH_LINE])
    assert hooks.run_codegraph_first_gate(_gate_event(repo, "Read", transcript=tr), repo) == 0
    assert capsys.readouterr().out == ""


def test_codegraph_transcript_mcp_call_unblocks_grep(tmp_path, capsys):
    """Prior mcp__codegraph__ tool_use in transcript unblocks Grep."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    tr = _write_transcript(tmp_path, [_MCP_CODEGRAPH_LINE])
    assert hooks.run_codegraph_first_gate(_gate_event(repo, "Grep", transcript=tr), repo) == 0
    assert capsys.readouterr().out == ""


def test_codegraph_transcript_toolsearch_unblocks_grep(tmp_path, capsys):
    """Prior ToolSearch(select:mcp__codegraph__...) in transcript unblocks Grep."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    tr = _write_transcript(tmp_path, [_TOOLSEARCH_CODEGRAPH_LINE])
    assert hooks.run_codegraph_first_gate(_gate_event(repo, "Grep", transcript=tr), repo) == 0
    assert capsys.readouterr().out == ""


def test_codegraph_user_tool_result_does_not_unblock(tmp_path, capsys):
    """A user-type tool_result that mentions codegraph in text must NOT unblock."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    # user tool_result echoing 'codegraph' — outer type is 'user', not 'assistant'
    tr = _write_transcript(tmp_path, [_USER_RESULT_LINE])
    assert hooks.run_codegraph_first_gate(_gate_event(repo, "Grep", transcript=tr), repo) == 0
    assert _deny(capsys) == "deny"


def test_codegraph_non_codegraph_toolsearch_does_not_unblock(tmp_path, capsys):
    """ToolSearch for a non-codegraph tool must NOT unblock."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    non_cg = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "t", "name": "ToolSearch",
         "input": {"query": "select:WebFetch"}}
    ]}})
    tr = _write_transcript(tmp_path, [non_cg])
    assert hooks.run_codegraph_first_gate(_gate_event(repo, "Grep", transcript=tr), repo) == 0
    assert _deny(capsys) == "deny"


def test_codegraph_gate_is_per_invocation(tmp_path, capsys):
    """Agent A's codegraph in its transcript must not unblock Agent B's grep."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    # Agent A transcript has a codegraph call → A's grep passes.
    tr_a = tmp_path / "a.jsonl"
    tr_a.write_text(_MCP_CODEGRAPH_LINE + "\n", encoding="utf-8")
    # Agent B transcript has only a bash call → B's grep is blocked.
    tr_b = tmp_path / "b.jsonl"
    tr_b.write_text(_BASH_LINE + "\n", encoding="utf-8")

    assert hooks.run_codegraph_first_gate(_gate_event(repo, "Grep", transcript=tr_a), repo) == 0
    assert capsys.readouterr().out == ""  # A is unblocked

    assert hooks.run_codegraph_first_gate(_gate_event(repo, "Grep", transcript=tr_b), repo) == 0
    assert _deny(capsys) == "deny"  # B is still blocked


def test_codegraph_gate_fail_open_on_exception(tmp_path, capsys):
    """Non-dict event -> .get() raises -> caught -> must not block."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    assert hooks.run_codegraph_first_gate([], repo) == 0
    assert capsys.readouterr().out == ""


def test_codegraph_gate_fail_open_missing_transcript(tmp_path, capsys):
    """Missing transcript path → fail open (no deny)."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    event = tool_event(repo, "Grep")
    event["transcript_path"] = ""  # empty → no scan possible
    assert hooks.run_codegraph_first_gate(event, repo) == 0
    assert capsys.readouterr().out == ""  # fail open, no deny


def test_codegraph_gate_fail_open_unreadable_transcript(tmp_path, capsys):
    """Unreadable transcript path → fail open (no deny)."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    # /nonexistent path → OSError in scan → canary logged + fail open.
    event = _gate_event(repo, "Grep", transcript="/nonexistent/no-such-file.jsonl")
    assert hooks.run_codegraph_first_gate(event, repo) == 0
    # Gate fails open — no deny output.
    assert capsys.readouterr().out == ""


def test_codegraph_gate_canary_on_blindness(tmp_path, capsys):
    """Rich transcript with zero tool_use blocks triggers canary + fail open."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    # Build a transcript with many assistant turns but NO tool_use blocks.
    blind_line = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking..."}]}})
    threshold = hooks._BLIND_DETECTION_THRESHOLD
    tr = _write_transcript(tmp_path, [blind_line] * (threshold + 1))

    assert hooks.run_codegraph_first_gate(_gate_event(repo, "Grep", transcript=tr), repo) == 0
    assert capsys.readouterr().out == ""  # fail open, not deny

    # Canary must be logged to .warden-log.
    log = repo / ".claude" / ".warden-log"
    assert log.exists(), "canary must write to .warden-log"
    assert "CANARY" in log.read_text()


def test_codegraph_gate_canary_boundary(tmp_path, capsys):
    """Below the blindness threshold a tool-less transcript still BLOCKS (the
    agent may simply not have called codegraph yet); AT the threshold with zero
    tool_use blocks it flips to canary + fail-open."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    blind = json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "..."}]}}
    )
    threshold = hooks._BLIND_DETECTION_THRESHOLD
    # threshold-1 assistant turns, 0 tool_uses → normal DENY (not yet blind).
    tr_below = _write_transcript(tmp_path, [blind] * (threshold - 1))
    assert (
        hooks.run_codegraph_first_gate(_gate_event(repo, "Grep", transcript=tr_below), repo)
        == 0
    )
    assert _deny(capsys) == "deny"
    # exactly threshold assistant turns, 0 tool_uses → canary + fail-open (allow).
    tr_at = _write_transcript(tmp_path, [blind] * threshold)
    assert (
        hooks.run_codegraph_first_gate(_gate_event(repo, "Grep", transcript=tr_at), repo)
        == 0
    )
    assert capsys.readouterr().out == ""


def test_codegraph_gate_allows_indirect_grep_by_design(tmp_path, capsys):
    """Primary-token classification does NOT block greps wrapped in another command."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    tr = _write_transcript(tmp_path, [_BASH_LINE])
    for cmd in ("xargs grep foo", "bash -c 'grep foo .'"):
        assert hooks.run_codegraph_first_gate(
            _gate_event(repo, "Bash", {"command": cmd}, transcript=tr), repo
        ) == 0
        assert capsys.readouterr().out == "", f"unexpectedly blocked: {cmd}"


def test_resolve_agent_transcript_derivation(tmp_path):
    """For a Task-spawned subagent (agent_id present) the gate scans the derived
    per-subagent file, not the parent transcript_path. Without agent_id the path
    is used as-is."""
    hooks = load_hooks()
    ev_sub = {"transcript_path": "/p/sess.jsonl", "agent_id": "xyz"}
    assert (
        hooks._resolve_agent_transcript(ev_sub)
        == "/p/sess/subagents/agent-xyz.jsonl"
    )
    assert (
        hooks._resolve_agent_transcript({"transcript_path": "/p/sess.jsonl"})
        == "/p/sess.jsonl"
    )
    assert hooks._resolve_agent_transcript({}) == ""


def test_codegraph_gate_scans_subagent_file_not_parent(tmp_path, capsys):
    """A codegraph call in the PARENT transcript must NOT unblock a Task-spawned
    subagent's grep (the parent lacks the subagent's calls); the same call in the
    derived SUBAGENT file MUST unblock it. This is the production-path fix."""
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    session_dir = tmp_path / "proj"
    session_dir.mkdir()
    parent = session_dir / "sess.jsonl"
    # Codegraph call lives ONLY in the parent (the wrong file for a subagent).
    parent.write_text(_MCP_CODEGRAPH_LINE + "\n", encoding="utf-8")
    sub_dir = (session_dir / "sess") / "subagents"
    sub_dir.mkdir(parents=True)
    sub_file = sub_dir / "agent-abc123.jsonl"
    sub_file.write_text(_BASH_LINE + "\n", encoding="utf-8")  # subagent: no codegraph yet

    event = tool_event(repo, "Grep")
    event["transcript_path"] = str(parent)
    event["agent_id"] = "abc123"
    # Gate scans the SUBAGENT file (no codegraph there) → blocked, despite the
    # parent containing a codegraph call.
    assert hooks.run_codegraph_first_gate(event, repo) == 0
    assert _deny(capsys) == "deny"

    # Subagent itself calls codegraph → its file now has the call → unblocked.
    sub_file.write_text(_MCP_CODEGRAPH_LINE + "\n", encoding="utf-8")
    assert hooks.run_codegraph_first_gate(event, repo) == 0
    assert capsys.readouterr().out == ""


def test_codegraph_gated_agents_have_hooks_block():
    """Coverage: an agent has ``codegraph_gated: true`` iff it has the gate
    hooks block. Prevents drift -- opting into the gate without the enforcement
    block (or vice versa) fails here."""
    agents_dir = ROOT / ".claude" / "agents"
    for md in agents_dir.rglob("*.md"):
        text = md.read_text(encoding="utf-8")
        if not text.startswith("---"):
            continue
        front = text[3 : text.index("---", 3)]
        gated = "codegraph_gated: true" in front
        has_block = "run codegraph-first-gate" in front
        assert gated == has_block, (
            f"{md.name}: `codegraph_gated` flag and the codegraph gate hook block "
            "are out of sync. Both must be present together or both absent."
        )


# ---------------------------------------------------------------------------
# Codegraph transcript format staleness gate (LIA-121)
# ---------------------------------------------------------------------------
# These tests exercise the SHARED PREDICATE (_line_is_codegraph_toolcall)
# against the committed fixture. If CC changes its transcript format, the
# fixture becomes stale and these tests detect the regression in the scan code.


_FIXTURE_PATH = ROOT / "scripts" / "tests" / "fixtures" / "codegraph_transcript_sample.jsonl"


def test_codegraph_transcript_fixture_positives():
    """Fixture positive samples are correctly detected by the shared predicate."""
    hooks = load_hooks()
    assert _FIXTURE_PATH.exists(), (
        f"Fixture not found at {_FIXTURE_PATH}. "
        "Re-capture from a live CC transcript and commit."
    )
    found_any_positive = False
    for raw in _FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        obj = json.loads(raw)
        role = obj.pop("_fixture_role", None)
        if role and role.startswith("pos_"):
            found_any_positive = True
            assert hooks._line_is_codegraph_toolcall(obj), (
                f"Positive fixture sample '{role}' NOT detected by _line_is_codegraph_toolcall. "
                "CC may have changed its transcript format, or the predicate was broken. "
                "Re-capture the fixture from a live transcript and update the predicate in "
                "codex_warden_hooks.py."
            )
    assert found_any_positive, "No positive samples found in fixture — fixture may be empty or malformed"


def test_codegraph_transcript_fixture_negatives():
    """Fixture negative samples are correctly rejected by the shared predicate."""
    hooks = load_hooks()
    assert _FIXTURE_PATH.exists(), f"Fixture not found at {_FIXTURE_PATH}"
    for raw in _FIXTURE_PATH.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        obj = json.loads(raw)
        role = obj.pop("_fixture_role", None)
        if role and role.startswith("neg_"):
            assert not hooks._line_is_codegraph_toolcall(obj), (
                f"Negative fixture sample '{role}' wrongly detected as codegraph call. "
                "The predicate is producing false positives — review _line_is_codegraph_toolcall."
            )


def test_line_is_codegraph_toolcall_direct_mcp():
    """Direct mcp__codegraph__ call detected."""
    hooks = load_hooks()
    obj = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__codegraph__codegraph_context", "input": {}}
    ]}}
    assert hooks._line_is_codegraph_toolcall(obj)


def test_line_is_codegraph_toolcall_toolsearch_select():
    """ToolSearch with select:mcp__codegraph__ query detected."""
    hooks = load_hooks()
    obj = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "ToolSearch",
         "input": {"query": "select:mcp__codegraph__codegraph_context"}}
    ]}}
    assert hooks._line_is_codegraph_toolcall(obj)


def test_line_is_codegraph_toolcall_code_search():
    """mcp__code-search__ prefix also detected."""
    hooks = load_hooks()
    obj = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "mcp__code-search__search_code", "input": {}}
    ]}}
    assert hooks._line_is_codegraph_toolcall(obj)


def test_line_is_codegraph_toolcall_rejects_user_type():
    """user-type lines are NOT matched (false-positive safety)."""
    hooks = load_hooks()
    obj = {"type": "user", "message": {"content": [
        {"type": "tool_result", "content": "mcp__codegraph__ gate denied"}
    ]}}
    assert not hooks._line_is_codegraph_toolcall(obj)


def test_line_is_codegraph_toolcall_rejects_text_block():
    """text blocks mentioning mcp__codegraph__ are NOT matched."""
    hooks = load_hooks()
    obj = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "I will call mcp__codegraph__codegraph_context"}
    ]}}
    assert not hooks._line_is_codegraph_toolcall(obj)


def test_line_is_codegraph_toolcall_rejects_non_codegraph_toolsearch():
    """ToolSearch with a non-codegraph query is NOT matched."""
    hooks = load_hooks()
    obj = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "ToolSearch", "input": {"query": "select:WebFetch"}}
    ]}}
    assert not hooks._line_is_codegraph_toolcall(obj)


def test_line_is_codegraph_toolcall_rejects_non_dict():
    """Non-dict input is safely rejected (no exception)."""
    hooks = load_hooks()
    assert not hooks._line_is_codegraph_toolcall(None)
    assert not hooks._line_is_codegraph_toolcall("string")
    assert not hooks._line_is_codegraph_toolcall([])


def test_scan_transcript_detects_mcp_call(tmp_path):
    """_scan_transcript_for_codegraph finds a direct MCP call."""
    hooks = load_hooks()
    tr = _write_transcript(tmp_path, [_MCP_CODEGRAPH_LINE])
    found, turns, tool_uses = hooks._scan_transcript_for_codegraph(str(tr))
    assert found
    assert turns >= 1
    assert tool_uses >= 1


def test_scan_transcript_detects_toolsearch(tmp_path):
    """_scan_transcript_for_codegraph finds a ToolSearch select call."""
    hooks = load_hooks()
    tr = _write_transcript(tmp_path, [_TOOLSEARCH_CODEGRAPH_LINE])
    found, turns, tool_uses = hooks._scan_transcript_for_codegraph(str(tr))
    assert found
    assert tool_uses >= 1


def test_scan_transcript_returns_false_for_no_call(tmp_path):
    """_scan_transcript_for_codegraph returns False when no codegraph call present."""
    hooks = load_hooks()
    tr = _write_transcript(tmp_path, [_BASH_LINE, _USER_RESULT_LINE])
    found, turns, tool_uses = hooks._scan_transcript_for_codegraph(str(tr))
    assert not found
    assert tool_uses >= 1  # Bash tool_use counts


def test_scan_transcript_missing_file():
    """_scan_transcript_for_codegraph returns None for missing/unreadable file."""
    hooks = load_hooks()
    result = hooks._scan_transcript_for_codegraph("/nonexistent/x.jsonl")
    assert result is None, "missing file must return None (fail-open sentinel)"
