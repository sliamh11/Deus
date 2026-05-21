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
    """Event from cwd outside any git worktree → gate passes silently.

    Pins the non-worktree early-exit. Without this, the empty-paths fix
    could regress in the other direction (firing the gate everywhere).
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
