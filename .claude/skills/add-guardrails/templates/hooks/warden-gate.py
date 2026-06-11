#!/usr/bin/env python3
"""Self-contained quality-gate for Claude Code projects.

Enforces a three-stage review discipline through Claude Code hooks:

    plan-review   before any Edit / Write / MultiEdit
    code-review   before `git commit`
    verification  before `git commit`

State is a small set of marker files under ``<repo>/.claude/``. A marker is
created by the ``mark`` action after a reviewing agent returns a SHIP verdict,
and cleared at SessionStart and whenever a new plan is started. The companion
agent specs live in ``.claude/agents/`` and the rules they read live in
``.claude/wardens/`` — everything travels with the repo, so any developer who
clones it gets the same gates with no external setup.

No third-party dependencies. Reads the hook event as JSON on stdin and, when a
gate fails, prints a PreToolUse deny decision to stdout (exit 0).
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

#: marker key -> filename under <repo>/.claude/
MARKERS = {
    "plan-reviewed": ".plan-reviewed",
    "code-reviewed": ".code-reviewed",
    "verified": ".verified",
}

#: marker key -> the agent that produces it
WARDEN_FOR = {
    "plan-reviewed": "plan-reviewer",
    "code-reviewed": "code-reviewer",
    "verified": "verification-gate",
}

VERDICTS_FILE = ".warden-verdicts.json"
VALID_VERDICTS = {"SHIP", "TRIVIAL", "REVISE", "BLOCK"}
PASS_VERDICTS = {"SHIP", "TRIVIAL"}

#: Matches a `git commit` invocation at the start of the command or after a
#: shell separator, including `git -C <dir> commit`. Mirrors the host gate so a
#: commit can only proceed once the review markers exist.
GIT_COMMIT_RE = re.compile(r"(^|[;&|]\s*)git(?:\s+-C\s+\S+)?\s+commit(\s|$)")

SELF = "python3 .claude/hooks/warden-gate.py"


# --- repo / state helpers --------------------------------------------------


def repo_root() -> Path:
    """Resolve the repo (or worktree) root, falling back to the cwd."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path.cwd()


def _claude_dir(repo: Path) -> Path:
    d = repo / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _marker_path(repo: Path, key: str) -> Path:
    return _claude_dir(repo) / MARKERS[key]


def _has_marker(repo: Path, key: str) -> bool:
    return _marker_path(repo, key).exists()


def _verdicts(repo: Path) -> dict:
    path = _claude_dir(repo) / VERDICTS_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _last_verdict(repo: Path, warden: str) -> str | None:
    entry = _verdicts(repo).get(warden)
    return entry.get("verdict") if isinstance(entry, dict) else None


# --- hook I/O --------------------------------------------------------------


def read_event() -> dict:
    try:
        raw = sys.stdin.read()
    except (OSError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def deny(reason: str) -> None:
    """Emit a PreToolUse deny decision (the tool call is blocked)."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            separators=(",", ":"),
        )
    )


def _tool_input(event: dict) -> dict:
    value = event.get("tool_input")
    return value if isinstance(value, dict) else {}


def _is_git_commit(event: dict) -> bool:
    command = _tool_input(event).get("command")
    return isinstance(command, str) and bool(GIT_COMMIT_RE.search(command))


# --- actions ---------------------------------------------------------------


def session_init(repo: Path) -> int:
    for key in MARKERS:
        _marker_path(repo, key).unlink(missing_ok=True)
    return 0


def plan_mode_invalidator(repo: Path, event: dict) -> int:
    """Clear the plan marker when a new plan is started or a Plan subagent runs."""
    clear = False
    if event.get("hook_event_name") == "UserPromptSubmit":
        clear = str(event.get("prompt") or "").lstrip().startswith("/plan")
    else:
        tool = str(event.get("tool_name") or "")
        data = _tool_input(event)
        subagent = str(
            data.get("subagent_type") or data.get("agent_type") or data.get("name") or ""
        )
        clear = tool == "ExitPlanMode" or (
            tool in {"Task", "Agent"} and subagent.lower() == "plan"
        )
    if clear:
        _marker_path(repo, "plan-reviewed").unlink(missing_ok=True)
    return 0


def plan_review_gate(repo: Path, event: dict) -> int:
    if _has_marker(repo, "plan-reviewed"):
        return 0
    mark_cmd = f'  {SELF} mark plan-reviewed SHIP "reason"'
    last = _last_verdict(repo, "plan-reviewer")
    if last in {"REVISE", "BLOCK"}:
        reason = (
            f"[plan-review-gate] BLOCKED: last plan-reviewer verdict was {last}.\n\n"
            "Re-run the plan-reviewer agent after fixing the issues. Trivial bypass is "
            f"not permitted after {last}.\n\nAfter SHIP:\n{mark_cmd}"
        )
    else:
        reason = (
            "[plan-review-gate] BLOCKED: no plan-reviewer approval for this change.\n\n"
            "Run the plan-reviewer agent, wait for VERDICT: SHIP, then:\n\n"
            f"{mark_cmd}\n\n"
            "Trivial change (typo, comment, single-line rename):\n"
            f'  {SELF} mark plan-reviewed TRIVIAL "reason"'
        )
    deny(reason)
    return 0


def code_review_gate(repo: Path, event: dict) -> int:
    if not _is_git_commit(event) or _has_marker(repo, "code-reviewed"):
        return 0
    mark_cmd = f'  {SELF} mark code-reviewed SHIP "reason"'
    last = _last_verdict(repo, "code-reviewer")
    if last in {"REVISE", "BLOCK"}:
        reason = (
            f"[code-review-gate] BLOCKED: last code-reviewer verdict was {last}.\n\n"
            f"Re-run the code-reviewer agent after fixing, then:\n{mark_cmd}"
        )
    else:
        reason = (
            "[code-review-gate] BLOCKED: no code-reviewer approval for this commit.\n\n"
            f"Run the code-reviewer agent on the staged diff, wait for SHIP, then:\n{mark_cmd}"
        )
    deny(reason)
    return 0


def verification_gate(repo: Path, event: dict) -> int:
    if not _is_git_commit(event) or _has_marker(repo, "verified"):
        return 0
    deny(
        "[verification-gate] BLOCKED: no verification evidence for this commit.\n\n"
        "Run the verification-gate agent (build / test / lint), wait for SHIP, then:\n"
        f'  {SELF} mark verified SHIP "reason"'
    )
    return 0


def mark(repo: Path, key: str, verdict: str, reason: str) -> int:
    if key not in MARKERS:
        print(
            f"[warden-gate] unknown marker '{key}' (expected one of {sorted(MARKERS)})",
            file=sys.stderr,
        )
        return 2
    verdict = verdict.upper()
    if verdict not in VALID_VERDICTS:
        print(
            f"[warden-gate] verdict must be one of {sorted(VALID_VERDICTS)}",
            file=sys.stderr,
        )
        return 2

    warden = WARDEN_FOR[key]
    store = _verdicts(repo)
    previous = store.get(warden)
    prev_verdict = previous.get("verdict") if isinstance(previous, dict) else None
    if verdict == "TRIVIAL" and prev_verdict in {"REVISE", "BLOCK"}:
        print(
            f"[warden-gate] trivial bypass not permitted after {prev_verdict}; "
            "re-run the warden and mark SHIP.",
            file=sys.stderr,
        )
        return 2

    store[warden] = {"verdict": verdict, "reason": reason}
    (_claude_dir(repo) / VERDICTS_FILE).write_text(
        json.dumps(store, indent=2) + "\n", encoding="utf-8"
    )

    marker = _marker_path(repo, key)
    if verdict in PASS_VERDICTS:
        marker.write_text(f"{verdict}: {reason}\n", encoding="utf-8")
        print(f"[warden-gate] {key} marked {verdict}: {reason}")
    else:
        marker.unlink(missing_ok=True)
        print(f"[warden-gate] {key} recorded {verdict} (marker cleared): {reason}")
    return 0


# --- dispatch --------------------------------------------------------------

_GATES = {
    "plan-review-gate": plan_review_gate,
    "plan-mode-invalidator": plan_mode_invalidator,
    "code-review-gate": code_review_gate,
    "verification-gate": verification_gate,
}


def main(argv: list[str]) -> int:
    if not argv:
        print(f"usage: {SELF} <action> [args...]", file=sys.stderr)
        return 2

    action = argv[0]
    repo = repo_root()

    if action == "mark":
        if len(argv) < 4:
            print(f'usage: {SELF} mark <marker> <verdict> "<reason>"', file=sys.stderr)
            return 2
        return mark(repo, argv[1], argv[2], " ".join(argv[3:]))

    if action == "session-init":
        return session_init(repo)

    gate = _GATES.get(action)
    if gate is None:
        print(f"[warden-gate] unknown action '{action}'", file=sys.stderr)
        return 2
    return gate(repo, read_event())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
