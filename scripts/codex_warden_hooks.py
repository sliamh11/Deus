#!/usr/bin/env python3
"""Install and run Codex hooks that mirror Deus Warden gates."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import platform
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
        "Edit|Write|MultiEdit|apply_patch",
        "plan-review-gate",
        5,
        "Checking Deus plan review",
    ),
    HookSpec(
        "PreToolUse",
        "ExitPlanMode|Task|Agent|spawn_agent",
        "plan-mode-invalidator",
        3,
        "Invalidating Deus plan review",
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
        "Edit|Write|MultiEdit|apply_patch",
        "memory-tree-hook",
        5,
        "Updating Deus memory tree",
    ),
    HookSpec(
        "PostToolUse",
        "Edit|Write|MultiEdit|apply_patch",
        "code-review-invalidator",
        3,
        "Invalidating Deus code review",
    ),
    HookSpec(
        "PostToolUse",
        "Edit|Write|MultiEdit|apply_patch",
        "threat-model-gate",
        3,
        "Checking Deus threat model",
    ),
    HookSpec(
        "PostToolUse",
        "Edit|Write|MultiEdit|apply_patch",
        "path-leak-detector",
        5,
        "Checking Deus path leaks",
    ),
    HookSpec("Stop", None, "stop-checkpoint", 5, "Writing Deus checkpoint"),
    HookSpec(
        "UserPromptSubmit",
        None,
        "plan-mode-invalidator",
        3,
        "Invalidating Deus plan review",
    ),
    HookSpec(
        "UserPromptSubmit",
        None,
        "catchup-freshness",
        10,
        "Checking Deus session freshness",
    ),
    HookSpec(
        "UserPromptSubmit",
        None,
        "orchestrator-preflight",
        5,
        "Checking Deus orchestrator",
    ),
    HookSpec(
        "UserPromptSubmit",
        None,
        "memory-retrieval",
        5,
        "Retrieving Deus memory",
    ),
)

PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
GIT_COMMIT_RE = re.compile(r"(^|[;&|]\s*)git(?:\s+-C\s+\S+)?\s+commit(\s|$)")
SECURITY_PATH_RE = re.compile(
    r"(auth|session|credential|token|oauth|secret|proxy|security|trust|encrypt|decrypt|permission)",
    re.IGNORECASE,
)
CATCHUP_RE = re.compile(
    r"catch.{0,5}up|what.{0,10}(were|we).{0,10}(doing|working)|"
    r"what do you remember|continue (from|where).{0,15}(left|stopped)|"
    r"pick up where|/resume\b|last session",
    re.IGNORECASE,
)
CONTEXT_LIMIT = 6_000


def _json(data: dict[str, Any]) -> None:
    print(json.dumps(data, separators=(",", ":")))


def _debug(message: str) -> None:
    if os.environ.get("DEUS_CODEX_HOOK_DEBUG") != "1":
        return
    try:
        log_dir = Path(os.environ.get("DEUS_STATE_DIR", Path.home() / ".deus"))
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.UTC).isoformat()
        with (log_dir / "codex_warden_hooks.log").open("a", encoding="utf-8") as f:
            f.write(f"{stamp} {message}\n")
    except OSError:
        pass


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


def _prompt(event: dict[str, Any]) -> str:
    prompt = event.get("prompt")
    return prompt if isinstance(prompt, str) else ""


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


def _active_script_path(repo_root: Path) -> Path:
    configured = os.environ.get("DEUS_CODEX_HOOK_SCRIPT_PATH")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return repo_root / "scripts" / "codex_warden_hooks.py"


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


def run_plan_mode_invalidator(event: dict[str, Any], repo_root: Path) -> int:
    should_clear = False
    if event.get("hook_event_name") == "UserPromptSubmit":
        should_clear = _prompt(event).lstrip().startswith("/plan")
    else:
        tool_name = str(event.get("tool_name") or "")
        tool_input = event.get("tool_input")
        tool_data = tool_input if isinstance(tool_input, dict) else {}
        subagent = str(
            tool_data.get("subagent_type")
            or tool_data.get("agent_type")
            or tool_data.get("name")
            or ""
        )
        should_clear = tool_name == "ExitPlanMode" or (
            tool_name in {"Task", "Agent", "spawn_agent"} and subagent.lower() == "plan"
        )

    if should_clear:
        _marker(repo_root, ".plan-reviewed").unlink(missing_ok=True)
    return 0


def run_plan_review_gate(event: dict[str, Any], repo_root: Path) -> int:
    config = _wardens_config(repo_root)
    if not _warden_enabled(config, "plan-reviewer"):
        return 0
    tool_name = str(event.get("tool_name") or "")
    if tool_name and not _warden_has_tool(
        config, "plan-reviewer", tool_name,
        ["Edit", "Write", "MultiEdit", "apply_patch"],
    ):
        return 0

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
    config = _wardens_config(repo_root)
    if not _warden_enabled(config, "code-reviewer"):
        return 0

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
        f"{_quote_args([str(_active_script_path(repo_root)), 'approve-admin-merge', '--repo-root', str(repo_root), '--command', command])}"
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


def _run_forwarded_hook(event: dict[str, Any], script: Path) -> int:
    if not script.exists():
        _debug(f"forwarded hook missing: {script}")
        return 0
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            input=json.dumps(event),
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            check=False,
        )
        if result.returncode != 0:
            _debug(f"forwarded hook returned {result.returncode}: {script}")
    except (OSError, subprocess.SubprocessError) as exc:
        _debug(f"forwarded hook failed: {script}: {exc}")
    return 0


def run_stop_checkpoint(event: dict[str, Any], repo_root: Path) -> int:
    return _run_forwarded_hook(event, repo_root / "scripts" / "stop_hook.py")


def run_memory_tree_hook(event: dict[str, Any], repo_root: Path) -> int:
    script = repo_root / "scripts" / "memory_tree_hook.py"
    _, paths = _managed_paths(event, repo_root)
    if not paths:
        return _run_forwarded_hook(event, script)

    for path in paths:
        forwarded = dict(event)
        tool_input = dict(event.get("tool_input") or {})
        tool_input["file_path"] = str(path)
        forwarded["tool_input"] = tool_input
        _run_forwarded_hook(forwarded, script)
    return 0


def run_code_review_invalidator(event: dict[str, Any], repo_root: Path) -> int:
    _, paths = _managed_paths(event, repo_root)
    if paths:
        _marker(repo_root, ".code-reviewed").unlink(missing_ok=True)
    return 0


def run_threat_model_gate(event: dict[str, Any], repo_root: Path) -> int:
    config = _wardens_config(repo_root)
    if not _warden_enabled(config, "threat-modeler"):
        return 0

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


def _additional_context(context: str) -> None:
    _json(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context[:CONTEXT_LIMIT],
            }
        }
    )


def _deus_config() -> dict[str, Any]:
    path = Path(os.environ.get("DEUS_CONFIG_PATH", "~/.config/deus/config.json")).expanduser()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _wardens_config(repo_root: Path) -> dict[str, Any]:
    path = repo_root / ".claude" / "wardens" / "config.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _warden_enabled(config: dict[str, Any], name: str) -> bool:
    warden = config.get(name)
    if not isinstance(warden, dict):
        return True
    return warden.get("enabled", True) is not False


def _warden_has_tool(
    config: dict[str, Any], name: str, tool: str, default_tools: list[str],
) -> bool:
    warden = config.get(name)
    if not isinstance(warden, dict):
        return tool in default_tools
    tools = warden.get("tools", default_tools)
    if not isinstance(tools, list):
        return tool in default_tools
    return tool in tools


def _vault_root() -> Path | None:
    env_path = os.environ.get("DEUS_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser()
    cfg_path = _deus_config().get("vault_path")
    if isinstance(cfg_path, str) and cfg_path:
        return Path(cfg_path).expanduser()
    return None


def _list_recent_names(path: Path, limit: int) -> list[str]:
    try:
        entries = sorted(path.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    return [entry.name for entry in entries[:limit]]


def _run_text(command: list[str], cwd: Path, timeout: int = 5) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"[warn] {exc}"
    return result.stdout.strip()


def _pending_block(state_file: Path) -> str:
    try:
        lines = state_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return f"[warn] STATE.md not found: {state_file}"
    out: list[str] = []
    in_pending = False
    for line in lines:
        if line.startswith("pending:"):
            in_pending = True
        elif in_pending and line and not line.startswith(" "):
            break
        if in_pending:
            out.append(line)
    return "\n".join(out) if out else "[warn] pending block not found"


def run_catchup_freshness(event: dict[str, Any], repo_root: Path) -> int:
    prompt = _prompt(event)
    if not prompt or not CATCHUP_RE.search(prompt):
        return 0

    today = dt.datetime.now().strftime("%Y-%m-%d")
    vault = _vault_root()
    lines = [
        "=== FRESHNESS CHECK (Codex hook-injected) ===",
        "(triggered by catch-up-shaped prompt; verifying live disk state)",
    ]

    lines.extend(["", f"--- Session-Logs/{today}/ ---"])
    if vault is None:
        lines.append("[warn] vault path unknown; set DEUS_VAULT_PATH or ~/.config/deus/config.json")
    else:
        names = _list_recent_names(vault / "Session-Logs" / today, 10)
        lines.extend(names or [f"[no entries for {today}]"])

    lines.extend(["", "--- Checkpoints (top 3) ---"])
    checkpoints = (vault / "Checkpoints") if vault is not None else Path("~/.deus/checkpoints").expanduser()
    names = _list_recent_names(checkpoints, 3)
    lines.extend(names or [f"[warn] checkpoints dir empty or missing: {checkpoints}"])

    lines.extend(["", "--- memory_indexer.py --recent 3 ---"])
    indexer = repo_root / "scripts" / "memory_indexer.py"
    if indexer.exists():
        recent = _run_text([sys.executable, str(indexer), "--recent", "3"], repo_root)
        lines.append("\n".join(recent.splitlines()[:80]) if recent else "[no recent output]")
    else:
        lines.append(f"[warn] indexer missing: {indexer}")

    lines.extend(["", "--- STATE.md pending (live from disk) ---"])
    if vault is None:
        lines.append("[warn] vault path unknown; cannot read STATE.md")
    else:
        lines.append(_pending_block(vault / "STATE.md"))
        lines.append("IMPORTANT: Prefer this live pending block over stale startup snapshots.")
    lines.append("=== END FRESHNESS CHECK ===")

    _additional_context("\n".join(lines))
    return 0


def _memory_log(result: dict[str, Any], prompt: str) -> None:
    try:
        log_file = Path(os.environ.get("DEUS_STATE_DIR", Path.home() / ".deus"))
        log_file.mkdir(parents=True, exist_ok=True)
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        paths = [
            item.get("path")
            for item in result.get("results", [])
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ]
        row = {
            "ts": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "prompt_hash": prompt_hash,
            "confidence": result.get("confidence", 0),
            "fell_back": bool(result.get("fell_back")),
            "paths": paths,
        }
        with (log_file / "memory_retrieval_log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError as exc:
        _debug(f"memory retrieval log failed: {exc}")


def _read_memory_result(path: str, vault: Path | None) -> str:
    if path.startswith("auto-memory/"):
        auto_root = os.environ.get("DEUS_AUTO_MEMORY_DIR")
        if not auto_root:
            return ""
        root = Path(auto_root).expanduser().resolve(strict=False)
        full = (root / path.removeprefix("auto-memory/")).resolve(strict=False)
    elif vault is not None:
        root = vault.expanduser().resolve(strict=False)
        full = (root / path).resolve(strict=False)
    else:
        return ""
    if not _is_relative_to(full, root):
        _debug(f"blocked memory path outside root: {path}")
        return ""
    try:
        return full.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def run_memory_retrieval(event: dict[str, Any], repo_root: Path) -> int:
    prompt = _prompt(event)
    if not prompt:
        return 0

    tree = repo_root / "scripts" / "memory_tree.py"
    if not tree.exists():
        return 0

    abstain = os.environ.get("DEUS_TREE_ABSTAIN", "0.45")
    try:
        result = subprocess.run(
            [sys.executable, str(tree), "query", prompt, "--json", "-k", "3", "--abstain", abstain],
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _debug(f"memory retrieval query failed: {exc}")
        return 0
    if not result.stdout.strip():
        return 0
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        _debug("memory retrieval returned non-json output")
        return 0
    if not isinstance(data, dict):
        return 0

    _memory_log(data, prompt)
    if data.get("fell_back"):
        return 0

    vault = _vault_root()
    sections = ["=== Auto-retrieved memory (may not be relevant to your task) ==="]
    for item in data.get("results", []):
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            continue
        text = _read_memory_result(item["path"], vault)
        if text:
            sections.append(f"--- {item['path']} (score: {item.get('score', 'n/a')}) ---")
            sections.append(text)
    if len(sections) == 1:
        return 0
    sections.append("=== End auto-retrieved memory ===")
    _additional_context("\n".join(sections))
    return 0


def run_orchestrator_preflight(event: dict[str, Any], repo_root: Path) -> int:
    del repo_root
    if os.environ.get("DEUS_CODEX_ORCHESTRATOR_PREFLIGHT") != "1":
        return 0
    if not _prompt(event).lstrip().startswith("/resume"):
        return 0
    if platform.system() != "Darwin":
        return 0

    label = os.environ.get("DEUS_HEALTHCHECK_LABEL")
    if not label:
        _additional_context(
            "=== ORCHESTRATOR PREFLIGHT (Codex hook-injected) ===\n"
            "[WARN] DEUS_HEALTHCHECK_LABEL is not set; preflight cannot check launchd."
        )
        return 0

    uid = str(os.getuid())
    target = f"gui/{uid}/{label}"
    if subprocess.run(["launchctl", "print", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        return 0

    plist = os.environ.get("DEUS_HEALTHCHECK_PLIST")
    if plist:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(Path(plist).expanduser())],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if subprocess.run(["launchctl", "print", target], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            _additional_context(
                "=== ORCHESTRATOR PREFLIGHT (Codex hook-injected) ===\n"
                f"Re-loaded {label} (was unloaded)."
            )
            return 0

    _additional_context(
        "=== ORCHESTRATOR PREFLIGHT (Codex hook-injected) ===\n"
        f"[WARN] {label} is not loaded; investigate before relying on fleet supervision."
    )
    return 0


RUNNERS = {
    "session-init": lambda event, repo: run_session_init(repo),
    "plan-review-gate": run_plan_review_gate,
    "plan-mode-invalidator": run_plan_mode_invalidator,
    "code-review-gate": run_code_review_gate,
    "admin-merge-gate": run_admin_merge_gate,
    "stop-checkpoint": run_stop_checkpoint,
    "memory-tree-hook": run_memory_tree_hook,
    "code-review-invalidator": run_code_review_invalidator,
    "threat-model-gate": run_threat_model_gate,
    "path-leak-detector": run_path_leak_detector,
    "catchup-freshness": run_catchup_freshness,
    "memory-retrieval": run_memory_retrieval,
    "orchestrator-preflight": run_orchestrator_preflight,
}


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"hooks": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
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


def _command(
    repo_root: Path,
    behavior: str,
    python_command: str | None = None,
    script_path: Path | None = None,
) -> str:
    script = script_path or Path(__file__).resolve()
    python_command = python_command or _default_python_command()
    return (
        f"{python_command} "
        f"{_quote_args([str(script), 'run', behavior, '--repo-root', str(repo_root), '--script-path', str(script)])}"
    )


def _handler(
    repo_root: Path,
    spec: HookSpec,
    python_command: str | None = None,
    script_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "type": "command",
        "command": _command(repo_root, spec.behavior, python_command, script_path),
        "timeout": spec.timeout,
        "statusMessage": spec.status,
    }


def _is_managed_command(command: str, repo_root: Path) -> bool:
    return "codex_warden_hooks.py" in command and str(repo_root) in command


def _merge_hooks(
    hooks_doc: dict[str, Any],
    repo_root: Path,
    python_command: str | None = None,
    script_path: Path | None = None,
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
        desired = _handler(repo_root, spec, python_command, script_path)
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
    script_path: Path | None = None,
    *,
    any_python: bool = False,
) -> bool:
    changed = False
    desired_commands = {
        _command(repo_root, spec.behavior, python_command, script_path)
        for spec in HOOK_SPECS
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


def _validated_script_path(raw: str | Path) -> Path:
    script = Path(raw).expanduser().resolve(strict=False)
    if not script.is_file():
        raise FileNotFoundError(f"Codex hook script path is missing: {script}")
    if not os.access(script, os.R_OK):
        raise PermissionError(f"Codex hook script path is not readable: {script}")
    return script


def install(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve(strict=False)
    hooks_path = Path(args.hooks_json)
    config_path = Path(args.config)
    python_command = args.python
    script_path = _validated_script_path(
        getattr(args, "script_path", Path(__file__).resolve())
    )

    hooks_doc = _load_json(hooks_path)
    upgrade_changed = _remove_hooks(
        hooks_doc, repo_root, python_command, script_path, any_python=True
    )
    hooks_changed = (
        _merge_hooks(hooks_doc, repo_root, python_command, script_path)
        or upgrade_changed
    )
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
    script_path = Path(
        getattr(args, "script_path", Path(__file__).resolve())
    ).expanduser().resolve(strict=False)
    hooks_doc = _load_json(hooks_path)
    hooks_changed = _remove_hooks(
        hooks_doc, repo_root, python_command, script_path, any_python=True
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
    try:
        script_path = _validated_script_path(
            getattr(args, "script_path", Path(__file__).resolve())
        )
    except (FileNotFoundError, PermissionError) as exc:
        print(f"MISSING: script-path {exc}")
        script_path = Path(
            getattr(args, "script_path", Path(__file__).resolve())
        ).expanduser().resolve(strict=False)
        script_ok = False
    else:
        script_ok = True

    hooks_doc = _load_json(hooks_path)
    hooks_ok = script_ok
    print(f"script-path: {script_path}")
    for spec in HOOK_SPECS:
        command = _command(repo_root, spec.behavior, python_command, script_path)
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
        else:
            print(f"OK: {spec.event} {spec.matcher} {spec.behavior}")

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
    parser.add_argument("--script-path", default=Path(__file__).resolve())
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
    os.environ["DEUS_CODEX_HOOK_SCRIPT_PATH"] = str(
        Path(args.script_path).expanduser().resolve(strict=False)
    )
    event = _read_stdin_json()
    return RUNNERS[args.behavior](event, repo_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("behavior", choices=sorted(RUNNERS))
    run_parser.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])
    run_parser.add_argument("--script-path", default=Path(__file__).resolve())

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
