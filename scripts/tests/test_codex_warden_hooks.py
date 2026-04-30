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


def test_threat_model_gate_warns_for_security_paths(tmp_path, capsys):
    hooks = load_hooks()
    repo = git_repo(tmp_path)
    (repo / "src").mkdir()
    (repo / "src" / "auth.ts").write_text("old\n", encoding="utf-8")

    rc = hooks.run_threat_model_gate(apply_patch_event(repo, "src/auth.ts"), repo)

    assert rc == 0
    output = json.loads(capsys.readouterr().out)
    assert "threat-modeler" in output["systemMessage"]


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
    (repo / "STATE.md").write_text("old\n", encoding="utf-8")
    calls = []

    def fake_forward(event, script):
        calls.append((event, script))
        return 0

    monkeypatch.setattr(hooks, "_run_forwarded_hook", fake_forward)

    assert hooks.run_memory_tree_hook(apply_patch_event(repo, "STATE.md"), repo) == 0
    assert calls[0][1] == repo / "scripts" / "memory_tree_hook.py"
    assert calls[0][0]["tool_input"]["file_path"] == str(repo / "STATE.md")


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
    (vault / "STATE.md").write_text("pending:\n  - [ ] task\n", encoding="utf-8")
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
