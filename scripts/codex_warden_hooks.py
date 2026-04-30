#!/usr/bin/env python3
"""Install and run Codex hooks that mirror Deus Warden gates."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from typing import Any


@dataclasses.dataclass(frozen=True)
class HookSpec:
    event: str
    matcher: str | None
    behavior: str
    timeout: int
    status: str


HOOK_SPECS: tuple[HookSpec, ...] = (
    HookSpec(
        "SessionStart",
        "startup|resume|clear",
        "session-init",
        3,
        "Resetting Deus review markers",
    ),
    HookSpec(
        "PreToolUse",
        "Edit|Write|apply_patch",
        "plan-review-gate",
        5,
        "Checking Deus plan review",
    ),
    HookSpec("PreToolUse", "Bash", "code-review-gate", 5, "Checking Deus code review"),
    HookSpec(
        "PreToolUse",
        "Bash",
        "admin-merge-gate",
        5,
        "Checking admin merge approval",
    ),
    HookSpec(
        "PostToolUse",
        "Edit|Write|apply_patch",
        "code-review-invalidator",
        3,
        "Invalidating Deus code review",
    ),
    HookSpec(
        "PostToolUse",
        "Edit|Write|apply_patch",
        "threat-model-gate",
        3,
        "Checking Deus threat model",
    ),
    HookSpec(
        "PostToolUse",
        "Edit|Write|apply_patch",
        "path-leak-detector",
        5,
        "Checking Deus path leaks",
    ),
)

PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
GIT_COMMIT_RE = re.compile(r"(^|[;&|]\s*)git(?:\s+-C\s+\S+)?\s+commit(\s|$)")
SECURITY_PATH_RE = re.compile(
    r"(auth|session|credential|token|oauth|secret|proxy|security|trust|encrypt|decrypt|permission)",
    re.IGNORECASE,
)


def _json(data: dict[str, Any]) -> None:
    print(json.dumps(data, separators=(",", ":")))


def _read_stdin_json() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _git(cwd: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip()


def _resolve_common_dir(top: Path, common: str | None) -> Path | None:
    if not common:
        return None
    path = Path(common)
    if not path.is_absolute():
        path = top / path
    return path.resolve(strict=False)


def _worktree_for_cwd(cwd: Path, repo_root: Path) -> Path | None:
    top_raw = _git(cwd, "rev-parse", "--show-toplevel")
    if top_raw is None:
        return None

    top = Path(top_raw).resolve(strict=False)
    common = _resolve_common_dir(top, _git(cwd, "rev-parse", "--git-common-dir"))
    repo_git = (repo_root / ".git").resolve(strict=False)

    if top == repo_root or common == repo_git:
        return top
    return None


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _event_paths(event: dict[str, Any], cwd: Path) -> list[Path]:
    tool_input = event.get("tool_input")
    raw_paths: list[str] = []

    if isinstance(tool_input, dict):
        file_path = tool_input.get("file_path")
        if isinstance(file_path, str):
            raw_paths.append(file_path)
        command = tool_input.get("command")
        if isinstance(command, str):
            raw_paths.extend(PATCH_FILE_RE.findall(command))
    elif isinstance(tool_input, str):
        raw_paths.extend(PATCH_FILE_RE.findall(tool_input))

    paths: list[Path] = []
    for raw in raw_paths:
        raw = raw.strip()
        if not raw:
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = cwd / path
        paths.append(path.resolve(strict=False))
    return paths


def _is_excluded(path: Path, marker_dir: Path) -> bool:
    if _is_relative_to(path, marker_dir / "worktrees"):
        return True

    parts = set(path.parts)
    if parts & {".git", "node_modules", "dist", ".truecourse", "coverage", "build"}:
        return True

    path_text = path.as_posix()
    if "/.coverage" in path_text:
        return True
    if any(segment in path_text for segment in ("/Checkpoints/", "/Session-Logs/", "/Atoms/")):
        return True
    if "/.claude/projects/" in path_text and "/memory/" in path_text:
        return True

    marker_names = {".plan-reviewed", ".code-reviewed", ".threat-modeled"}
    return _is_relative_to(path, marker_dir) and path.name in marker_names


def _git_ignored(path: Path, worktree: Path) -> bool:
    try:
        subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=worktree,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    return True


def _managed_paths(event: dict[str, Any], repo_root: Path) -> tuple[Path | None, list[Path]]:
    cwd = Path(str(event.get("cwd") or os.getcwd())).resolve(strict=False)
    worktree = _worktree_for_cwd(cwd, repo_root)
    if worktree is None:
        return None, []

    paths = [
        path
        for path in _event_paths(event, cwd)
        if _is_relative_to(path, worktree)
        and not _is_excluded(path, repo_root / ".claude")
        and not _git_ignored(path, worktree)
    ]
    return worktree, paths


def _block_pre_tool(reason: str) -> None:
    _json(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def _warn_post_tool(message: str) -> None:
    _json({"systemMessage": message})


def _marker(repo_root: Path, name: str) -> Path:
    return repo_root / ".claude" / name


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return command.split()


def _gh_command_index_after_global_flags(tokens: list[str], gh_index: int) -> int:
    index = gh_index + 1
    flags_with_values = {
        "--config-dir",
        "--hostname",
        "--repo",
        "-R",
    }

    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if not token.startswith("-"):
            return index
        if token in flags_with_values and index + 1 < len(tokens):
            index += 2
        else:
            index += 1
    return index


def _is_gh_executable(token: str) -> bool:
    token = token.strip("\"'")
    names = {Path(token).name.lower(), PureWindowsPath(token).name.lower()}
    return bool(names & {"gh", "gh.exe"})


def _is_admin_merge_command(command: str) -> bool:
    tokens = _shell_tokens(command)
    if not any(token == "--admin" or token.startswith("--admin=") for token in tokens):
        return False

    for index, token in enumerate(tokens):
        if not _is_gh_executable(token):
            continue
        command_index = _gh_command_index_after_global_flags(tokens, index)
        if tokens[command_index : command_index + 2] == ["pr", "merge"]:
            return True
    return False


def _admin_merge_marker(repo_root: Path) -> Path:
    return _marker(repo_root, ".admin-merge-approved")


def approve_admin_merge(command: str, repo_root: Path) -> int:
    marker = _admin_merge_marker(repo_root)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                "command_hash": _command_hash(command),
                "command": command,
                "created_at": dt.datetime.now(dt.UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Approved one admin merge command for {repo_root}")
    return 0


def run_session_init(repo_root: Path) -> int:
    for name in (
        ".plan-reviewed",
        ".code-reviewed",
        ".threat-modeled",
        ".admin-merge-approved",
    ):
        _marker(repo_root, name).unlink(missing_ok=True)
    return 0


def run_plan_review_gate(event: dict[str, Any], repo_root: Path) -> int:
    _, paths = _managed_paths(event, repo_root)
    if not paths or _marker(repo_root, ".plan-reviewed").exists():
        return 0

    target_list = "\n".join(f"  - {path}" for path in paths[:5])
    reason = (
        "[plan-review-gate] BLOCKED: no plan-reviewer approval marker.\n\n"
        "Before editing Deus source, run the plan-reviewer Warden and wait for "
        "VERDICT: SHIP. Then run:\n\n"
        f"  touch {shlex.quote(str(_marker(repo_root, '.plan-reviewed')))}\n\n"
        "Targets:\n"
        f"{target_list}"
    )
    _block_pre_tool(reason)
    return 0


def run_code_review_gate(event: dict[str, Any], repo_root: Path) -> int:
    cwd = Path(str(event.get("cwd") or os.getcwd())).resolve(strict=False)
    if _worktree_for_cwd(cwd, repo_root) is None:
        return 0

    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not GIT_COMMIT_RE.search(command):
        return 0
    if _marker(repo_root, ".code-reviewed").exists():
        return 0

    reason = (
        "[code-review-gate] BLOCKED: no code-reviewer approval marker.\n\n"
        "Before committing Deus changes, run the code-reviewer Warden and wait "
        "for VERDICT: SHIP. Then run:\n\n"
        f"  touch {shlex.quote(str(_marker(repo_root, '.code-reviewed')))}"
    )
    _block_pre_tool(reason)
    return 0


def run_admin_merge_gate(event: dict[str, Any], repo_root: Path) -> int:
    cwd = Path(str(event.get("cwd") or os.getcwd())).resolve(strict=False)
    if _worktree_for_cwd(cwd, repo_root) is None:
        return 0

    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not _is_admin_merge_command(command):
        return 0

    marker = _admin_merge_marker(repo_root)
    command_hash = _command_hash(command)
    if marker.exists():
        try:
            approved = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            approved = {}
        marker.unlink(missing_ok=True)
        if approved.get("command_hash") == command_hash:
            return 0

    approval = (
        f"{_default_python_command()} "
        f"{_quote_args([str(repo_root / 'scripts' / 'codex_warden_hooks.py'), 'approve-admin-merge', '--repo-root', str(repo_root), '--command', command])}"
    )
    reason = (
        "[admin-merge-gate] BLOCKED: `gh pr merge --admin` bypasses branch "
        "policy and needs fresh explicit approval.\n\n"
        "Prior approval to merge after green CI is not approval to bypass branch "
        "protection. Ask the user for explicit approval to use `--admin` on this "
        "exact command, then run:\n\n"
        f"  {approval}\n\n"
        "Retry the same admin merge command after approval. The approval marker "
        "is command-scoped and consumed on use.\n\n"
        f"Command hash: {command_hash}"
    )
    _block_pre_tool(reason)
    return 0


def run_code_review_invalidator(event: dict[str, Any], repo_root: Path) -> int:
    _, paths = _managed_paths(event, repo_root)
    if paths:
        _marker(repo_root, ".code-reviewed").unlink(missing_ok=True)
    return 0


def run_threat_model_gate(event: dict[str, Any], repo_root: Path) -> int:
    _, paths = _managed_paths(event, repo_root)
    if not paths or _marker(repo_root, ".threat-modeled").exists():
        return 0

    matched = [path for path in paths if SECURITY_PATH_RE.search(path.as_posix())]
    if not matched:
        return 0

    target_list = "\n".join(f"  - {path}" for path in matched[:5])
    _warn_post_tool(
        "[threat-model-gate] WARNING: edited a security-sensitive Deus path "
        "without a threat-modeler marker.\n\n"
        "Consider running the threat-modeler Warden, then suppress further "
        "warnings with:\n\n"
        f"  touch {shlex.quote(str(_marker(repo_root, '.threat-modeled')))}\n\n"
        f"Targets:\n{target_list}"
    )
    return 0


def run_path_leak_detector(event: dict[str, Any], repo_root: Path) -> int:
    worktree, paths = _managed_paths(event, repo_root)
    if worktree is None or not paths:
        return 0

    home = Path.home().resolve(strict=False).as_posix()
    leaks: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        matches = []
        if home and home in text:
            matches.append("absolute home path")
        if "/Users/" in text and "/Users/" + os.environ.get("USER", "") + "/" in text:
            matches.append("absolute macOS user path")
        if matches:
            rel = path.relative_to(worktree)
            leaks.append(f"  - {rel}: {', '.join(sorted(set(matches)))}")

    if leaks:
        _warn_post_tool(
            "[path-leak-detector] WARNING: tracked Deus file contains a personal "
            "absolute path. Replace it with config, $HOME, or a repo-relative path.\n\n"
            + "\n".join(leaks[:5])
        )
    return 0


RUNNERS = {
    "session-init": lambda event, repo: run_session_init(repo),
    "plan-review-gate": run_plan_review_gate,
    "code-review-gate": run_code_review_gate,
    "admin-merge-gate": run_admin_merge_gate,
    "code-review-invalidator": run_code_review_invalidator,
    "threat-model-gate": run_threat_model_gate,
    "path-leak-detector": run_path_leak_detector,
}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{path}: hooks must be an object")
    return data


def _default_python_command() -> str:
    configured = os.environ.get("DEUS_CODEX_HOOK_PYTHON")
    if configured:
        return configured
    return "py -3" if os.name == "nt" else "python3"


def _quote_args(args: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(args)
    return " ".join(shlex.quote(arg) for arg in args)


def _command(repo_root: Path, behavior: str, python_command: str | None = None) -> str:
    script = repo_root / "scripts" / "codex_warden_hooks.py"
    python_command = python_command or _default_python_command()
    return (
        f"{python_command} "
        f"{_quote_args([str(script), 'run', behavior, '--repo-root', str(repo_root)])}"
    )


def _handler(
    repo_root: Path, spec: HookSpec, python_command: str | None = None
) -> dict[str, Any]:
    return {
        "type": "command",
        "command": _command(repo_root, spec.behavior, python_command),
        "timeout": spec.timeout,
        "statusMessage": spec.status,
    }


def _is_managed_command(command: str, repo_root: Path) -> bool:
    return "codex_warden_hooks.py" in command and str(repo_root) in command


def _merge_hooks(
    hooks_doc: dict[str, Any], repo_root: Path, python_command: str | None = None
) -> bool:
    changed = False
    hooks = hooks_doc.setdefault("hooks", {})
    for spec in HOOK_SPECS:
        event_groups = hooks.setdefault(spec.event, [])
        if not isinstance(event_groups, list):
            raise ValueError(f"hooks.{spec.event} must be a list")

        group = next(
            (
                item
                for item in event_groups
                if isinstance(item, dict) and item.get("matcher") == spec.matcher
            ),
            None,
        )
        if group is None:
            group = {"hooks": []}
            if spec.matcher is not None:
                group["matcher"] = spec.matcher
            event_groups.append(group)
            changed = True

        handlers = group.setdefault("hooks", [])
        if not isinstance(handlers, list):
            raise ValueError(f"hooks.{spec.event}.hooks must be a list")
        desired = _handler(repo_root, spec, python_command)
        if not any(
            isinstance(handler, dict) and handler.get("command") == desired["command"]
            for handler in handlers
        ):
            handlers.append(desired)
            changed = True
    return changed


def _remove_hooks(
    hooks_doc: dict[str, Any],
    repo_root: Path,
    python_command: str | None = None,
    *,
    any_python: bool = False,
) -> bool:
    changed = False
    desired_commands = {
        _command(repo_root, spec.behavior, python_command) for spec in HOOK_SPECS
    }
    hooks = hooks_doc.get("hooks", {})
    if not isinstance(hooks, dict):
        return False

    for event in list(hooks):
        groups = hooks[event]
        if not isinstance(groups, list):
            continue
        new_groups = []
        for group in groups:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            handlers = group.get("hooks", [])
            if not isinstance(handlers, list):
                new_groups.append(group)
                continue
            kept = [
                handler
                for handler in handlers
                if not (
                    isinstance(handler, dict)
                    and isinstance(handler.get("command"), str)
                    and (
                        handler.get("command") in desired_commands
                        or (
                            any_python
                            and _is_managed_command(handler["command"], repo_root)
                        )
                    )
                )
            ]
            if len(kept) != len(handlers):
                changed = True
            if kept:
                group = dict(group)
                group["hooks"] = kept
                new_groups.append(group)
        if new_groups:
            hooks[event] = new_groups
        else:
            del hooks[event]
            changed = True
    return changed


def _feature_enabled(config_text: str) -> bool:
    in_features = False
    for line in config_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_features = stripped == "[features]"
            continue
        if in_features and stripped.startswith("codex_hooks"):
            return stripped.split("=", 1)[1].strip().lower() == "true"
    return False


def _set_feature(config_text: str, enabled: bool) -> tuple[str, bool]:
    value = "true" if enabled else "false"
    lines = config_text.splitlines()
    out: list[str] = []
    in_features = False
    saw_features = False
    wrote = False
    changed = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_features and not wrote:
                out.append(f"codex_hooks = {value}")
                wrote = True
                changed = True
            in_features = stripped == "[features]"
            saw_features = saw_features or in_features
            out.append(line)
            continue

        if in_features and stripped.startswith("codex_hooks"):
            new_line = f"codex_hooks = {value}"
            out.append(new_line)
            wrote = True
            changed = changed or line != new_line
            continue

        out.append(line)

    if saw_features and in_features and not wrote:
        out.append(f"codex_hooks = {value}")
        changed = True
    elif not saw_features:
        if out and out[-1] != "":
            out.append("")
        out.extend(["[features]", f"codex_hooks = {value}"])
        changed = True

    return "\n".join(out).rstrip() + "\n", changed


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_bytes(path.read_bytes())
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as tmp:
        tmp.write(text)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def install(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve(strict=False)
    hooks_path = Path(args.hooks_json)
    config_path = Path(args.config)
    python_command = args.python

    hooks_doc = _load_json(hooks_path)
    upgrade_changed = _remove_hooks(
        hooks_doc, repo_root, python_command, any_python=True
    )
    hooks_changed = _merge_hooks(hooks_doc, repo_root, python_command) or upgrade_changed
    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    new_config, config_changed = _set_feature(config_text, True)

    if args.dry_run:
        print(f"DRY RUN: hooks {'would change' if hooks_changed else 'already installed'}")
        print(f"DRY RUN: config {'would change' if config_changed else 'already enabled'}")
        return 0

    if hooks_changed:
        _write_atomic(hooks_path, json.dumps(hooks_doc, indent=2, sort_keys=True) + "\n")
    if config_changed:
        _write_atomic(config_path, new_config)
    print(f"Installed Codex Warden hooks for {repo_root}")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve(strict=False)
    hooks_path = Path(args.hooks_json)
    config_path = Path(args.config)
    python_command = args.python
    hooks_doc = _load_json(hooks_path)
    hooks_changed = _remove_hooks(
        hooks_doc, repo_root, python_command, any_python=True
    )

    config_changed = False
    new_config = ""
    if args.disable_feature:
        config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        new_config, config_changed = _set_feature(config_text, False)

    if args.dry_run:
        print(f"DRY RUN: hooks {'would change' if hooks_changed else 'not installed'}")
        if args.disable_feature:
            print(
                f"DRY RUN: config {'would change' if config_changed else 'already disabled'}"
            )
        return 0

    if hooks_changed:
        _write_atomic(hooks_path, json.dumps(hooks_doc, indent=2, sort_keys=True) + "\n")
    if args.disable_feature and config_changed:
        _write_atomic(config_path, new_config)
    print(f"Uninstalled Codex Warden hooks for {repo_root}")
    return 0


def check(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve(strict=False)
    hooks_path = Path(args.hooks_json)
    config_path = Path(args.config)
    python_command = args.python

    hooks_doc = _load_json(hooks_path)
    hooks_ok = True
    for spec in HOOK_SPECS:
        command = _command(repo_root, spec.behavior, python_command)
        found = False
        for group in hooks_doc.get("hooks", {}).get(spec.event, []):
            if not isinstance(group, dict) or group.get("matcher") != spec.matcher:
                continue
            handlers = group.get("hooks", [])
            found = any(
                isinstance(handler, dict) and handler.get("command") == command
                for handler in handlers
            )
            if found:
                break
        if not found:
            print(f"MISSING: {spec.event} {spec.matcher} {spec.behavior}")
            hooks_ok = False

    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    feature_ok = _feature_enabled(config_text)
    if not feature_ok:
        print("MISSING: [features].codex_hooks = true")

    if hooks_ok and feature_ok:
        print("Codex Warden hooks installed.")
        return 0
    return 1


def _default_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))


def _add_common_install_args(parser: argparse.ArgumentParser) -> None:
    codex_home = _default_codex_home()
    parser.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--codex-home", default=codex_home)
    parser.add_argument("--config", default=None)
    parser.add_argument("--hooks-json", default=None)
    parser.add_argument(
        "--python",
        default=_default_python_command(),
        help="Python command used in installed hook handlers.",
    )
    parser.add_argument("--dry-run", action="store_true")


def _finalize_paths(args: argparse.Namespace) -> None:
    codex_home = Path(args.codex_home).expanduser()
    if args.config is None:
        args.config = codex_home / "config.toml"
    if args.hooks_json is None:
        args.hooks_json = codex_home / "hooks.json"


def run(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve(strict=False)
    event = _read_stdin_json()
    return RUNNERS[args.behavior](event, repo_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("behavior", choices=sorted(RUNNERS))
    run_parser.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])

    approve_parser = subparsers.add_parser("approve-admin-merge")
    approve_parser.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])
    approve_parser.add_argument("--command", dest="admin_command", required=True)

    for name in ("install", "check", "uninstall"):
        sub = subparsers.add_parser(name)
        _add_common_install_args(sub)
        if name == "uninstall":
            sub.add_argument("--disable-feature", action="store_true")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action in {"install", "check", "uninstall"}:
        _finalize_paths(args)

    if args.action == "run":
        return run(args)
    if args.action == "approve-admin-merge":
        return approve_admin_merge(
            args.admin_command, Path(args.repo_root).resolve(strict=False)
        )
    if args.action == "install":
        return install(args)
    if args.action == "check":
        return check(args)
    if args.action == "uninstall":
        return uninstall(args)
    raise AssertionError(args.action)


if __name__ == "__main__":
    sys.exit(main())
