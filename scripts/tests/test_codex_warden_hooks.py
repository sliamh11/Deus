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
