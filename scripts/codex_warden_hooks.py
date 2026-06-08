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
from pathlib import Path, PurePosixPath, PureWindowsPath
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
    HookSpec("PreToolUse", "Bash", "ai-eng-gate", 5, "Checking AI engineering review"),
    HookSpec("PreToolUse", "Bash", "verification-gate", 5, "Checking Deus verification"),
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
        "memo-enricher",
        3,
        "Enriching Deus warden memo",
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
        "verification-invalidator",
        3,
        "Invalidating Deus verification",
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
    HookSpec(
        "PostToolUse",
        "Edit|Write|MultiEdit|apply_patch",
        "cold-memory-injector",
        5,
        "Injecting Deus cold-memory context",
    ),
    HookSpec(
        "PostToolUse",
        "Edit|Write|MultiEdit|apply_patch",
        "structural-check",
        3,
        "Running Deus structural checks",
    ),
    HookSpec(
        "PreToolUse",
        "Write|apply_patch",
        "placement-guard",
        3,
        "Checking Deus file placement",
    ),
    HookSpec(
        "PostToolUse",
        "Agent",
        "warden-verdict-tracker",
        5,
        "Tracking warden verdicts",
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
    HookSpec(
        "UserPromptSubmit",
        None,
        "migration-nudge",
        3,
        "Checking pending migrations",
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

    marker_names = {".plan-reviewed", ".code-reviewed", ".threat-modeled", ".verified", ".ai-eng-reviewed"}
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


# --- Per-worktree gate isolation -------------------------------------------
# Gate state (review markers + the verdict store) is keyed per git worktree so
# parallel gated commits across worktrees don't satisfy each other's gates.
# Main repo (worktree == repo_root) keeps the flat .claude/ paths (back-compat).

#: Markers namespaced per worktree. All six are accessed as files via _marker()
#: by session-init and the invalidators (and plan/ai-eng/threat gates also READ
#: their marker directly), so every one must be namespaced to isolate worktrees.
#: The verdict store is namespaced SEPARATELY in _verdicts_path() — that is the
#: extra read path code-review + verification gates decide on. Intentionally
#: global (NOT here): .admin-merge-approved (one-shot, consumed immediately, run
#: from a terminal whose cwd may not be the worktree) and .plan-scope.md
#: (the plan-reviewer/code-reviewer agents read/write it at the flat path).
_PER_WORKTREE_MARKERS = frozenset({
    ".plan-reviewed", ".code-reviewed", ".ai-eng-reviewed",
    ".threat-modeled", ".verified", ".commit-window",
})

#: Process-local cache of cwd -> worktree resolution (each hook/CLI run is a
#: fresh, single-threaded process, so a plain dict is safe).
_WORKTREE_CACHE: dict[tuple[str, str], Path] = {}

#: CLI override: the `mark`/`mark-batch` actions run from a terminal whose cwd
#: is not guaranteed to be the worktree, so main() sets this (try/finally) to
#: inject the target worktree. Hooks never set it -> they auto-derive from cwd.
_WORKTREE_OVERRIDE: Path | None = None


def _current_worktree(repo_root: Path) -> Path:
    """Resolve the worktree for the current process cwd (cached), or repo_root."""
    cwd = Path(os.getcwd()).resolve(strict=False)
    key = (str(cwd), str(repo_root))
    if key not in _WORKTREE_CACHE:
        _WORKTREE_CACHE[key] = _worktree_for_cwd(cwd, repo_root) or repo_root
    return _WORKTREE_CACHE[key]


def _claude_marker_dir(repo_root: Path) -> Path:
    """Return the .claude state dir for the active worktree.

    Main repo -> repo_root/.claude (flat, unchanged). A non-main worktree ->
    repo_root/.claude/worktree-markers/<sha1(worktree)[:12]>. 12 hex = 48 bits;
    with well under 100 worktrees the collision probability is effectively zero.
    """
    wt = _WORKTREE_OVERRIDE or _current_worktree(repo_root)
    base = repo_root / ".claude"
    if wt.resolve(strict=False) != repo_root.resolve(strict=False):
        wt_id = hashlib.sha1(str(wt.resolve(strict=False)).encode()).hexdigest()[:12]
        return base / "worktree-markers" / wt_id
    return base


def _marker(repo_root: Path, name: str) -> Path:
    if name in _PER_WORKTREE_MARKERS:
        return _claude_marker_dir(repo_root) / name
    return repo_root / ".claude" / name


def _marker_dir_for_worktree(repo_root: Path, worktree_root: Path) -> Path:
    """Like _claude_marker_dir but for an EXPLICIT worktree (no cwd derivation).

    Mirrors _claude_marker_dir's namespacing exactly so callers resolve the
    SAME per-worktree bucket the code-review/verification gates write to: the
    main repo -> flat .claude; any other worktree ->
    .claude/worktree-markers/<sha1(worktree)[:12]>. The admin-merge standing
    gate uses this to read the verdict store of the worktree being merged
    deterministically, instead of relying on _current_worktree()'s os.getcwd().
    """
    base = repo_root / ".claude"
    if worktree_root.resolve(strict=False) != repo_root.resolve(strict=False):
        wt_id = hashlib.sha1(
            str(worktree_root.resolve(strict=False)).encode()
        ).hexdigest()[:12]
        return base / "worktree-markers" / wt_id
    return base


# ---------------------------------------------------------------------------
# Codegraph-first gate (LIA-121 / RETRO-2026-05-29-01)
# ---------------------------------------------------------------------------

#: Filesystem code-search commands; blocked as a primary token before a
#: codegraph/code_search call. See ``_bash_is_code_search``.
_CODE_SEARCH_COMMANDS = frozenset(
    {"grep", "egrep", "fgrep", "rg", "ripgrep", "ag", "ack", "find"}
)

#: Minimum number of assistant turns in a transcript before we trust that a
#: missing codegraph call is deliberate vs. the gate being blind.  Below this
#: threshold the gate blocks normally (agent might not have had a chance to
#: call codegraph yet); at or above it with zero recognized tool_use blocks of
#: any kind, the gate logs a canary and fails open.
_BLIND_DETECTION_THRESHOLD = 5


def _line_is_codegraph_toolcall(obj: Any) -> bool:
    """True if *obj* (a parsed JSONL transcript line) is a codegraph/
    code_search tool_use -- or a ToolSearch that selects one.

    This is the SHARED predicate used by both the live transcript scan
    (``_scan_transcript_for_codegraph``) and the CI fixture test
    (``test_codegraph_transcript_fixture``).  Keep both callers in sync.

    Rules:
    * Outer ``type`` must be ``"assistant"`` (not ``"user"`` / ``"attachment"``).
    * ``message.content`` must be a list containing a block where
      ``type == "tool_use"`` AND either:
      - ``name`` starts with ``"mcp__codegraph__"`` or ``"mcp__code-search__"``
      - ``name == "ToolSearch"`` AND ``input.query`` contains
        ``"mcp__codegraph__"`` or ``"mcp__code-search__"``

    False-positive sources explicitly excluded (caller type ``"user"``
    or non-``"tool_use"`` block types) are safe because we check outer type
    and inner block type strictly.
    """
    if not isinstance(obj, dict):
        return False
    if obj.get("type") != "assistant":
        return False
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    for blk in content:
        if not isinstance(blk, dict) or blk.get("type") != "tool_use":
            continue
        name = str(blk.get("name") or "")
        if name.startswith("mcp__codegraph__") or name.startswith("mcp__code-search__"):
            return True
        if name == "ToolSearch":
            inp = blk.get("input")
            query = str(inp.get("query") or "") if isinstance(inp, dict) else ""
            q = query.lower()
            if "mcp__codegraph__" in q or "mcp__code-search__" in q:
                return True
    return False


def _scan_transcript_for_codegraph(
    transcript_path: str,
) -> tuple[bool, int, int, int] | None:
    """Read the transcript JSONL at *transcript_path* and return
    ``(found, assistant_turns, any_tool_uses, prior_search_attempts)``,
    or ``None`` on IO error.

    * ``found``: True if any line satisfies ``_line_is_codegraph_toolcall``.
    * ``assistant_turns``: count of lines with outer ``type == "assistant"``.
    * ``any_tool_uses``: count of tool_use blocks seen across all lines
      (used to detect parse blindness: if assistant_turns is high but
      any_tool_uses is zero, the format may have changed).
    * ``prior_search_attempts``: count of tool_use blocks whose name is
      ``"Grep"`` or ``"Glob"``, or ``"Bash"`` with a primary code-search
      command (per ``_bash_is_code_search``).  The current hook's search
      attempt is NOT yet in the transcript when the hook fires, so this
      counts only PAST attempts — exactly what the escalating-deny message
      needs.
    * Returns ``None`` when the file cannot be opened (missing path, permission
      error).  The caller should treat ``None`` as a fail-open signal.

    Parse errors on individual lines are silently skipped (partial writes are
    expected on a live transcript).
    """
    path = Path(transcript_path)
    found = False
    assistant_turns = 0
    any_tool_uses = 0
    prior_search_attempts = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("type") == "assistant":
                    assistant_turns += 1
                    msg = obj.get("message")
                    if isinstance(msg, dict):
                        content = msg.get("content")
                        if isinstance(content, list):
                            for blk in content:
                                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                                    any_tool_uses += 1
                                    blk_name = blk.get("name", "")
                                    if blk_name in ("Grep", "Glob"):
                                        prior_search_attempts += 1
                                    elif blk_name == "Bash":
                                        blk_input = blk.get("input")
                                        if isinstance(blk_input, dict):
                                            cmd = blk_input.get("command", "")
                                            if isinstance(cmd, str) and _bash_is_code_search(cmd):
                                                prior_search_attempts += 1
                if _line_is_codegraph_toolcall(obj):
                    found = True
                    # Answer known; the turn/tool_use counters are only consulted
                    # by the caller's blindness branch when found is False.
                    break
    except OSError:
        return None
    return found, assistant_turns, any_tool_uses, prior_search_attempts


def _resolve_agent_transcript(event: dict[str, Any]) -> str:
    """Return the transcript file the gate should scan for THIS agent.

    For a Task-spawned subagent the hook event's ``transcript_path`` points at the
    PARENT session file (``.../<session_id>.jsonl``), which contains only the main
    agent's activity (its ``Agent`` delegation) -- NOT the subagent's own tool
    calls.  Those are written to ``.../<session_id>/subagents/agent-<agent_id>.jsonl``.
    When ``agent_id`` is present we scan that per-subagent file, which both fixes
    the production path AND gives natural per-invocation isolation (each subagent
    invocation has its own file, so a codegraph call by one never unblocks a
    parallel sibling).  For the main thread / ``--agent`` runs there is no
    ``agent_id`` and ``transcript_path`` is already the agent's own file.

    We deliberately do NOT fall back to the parent file when the derived subagent
    file is absent: the parent is the wrong file and scanning it would false-block.
    Returning the (possibly not-yet-existing) subagent path lets
    ``_scan_transcript_for_codegraph`` return ``None`` -> the gate fails open.

    Empirically validated (LIA-121): the PreToolUse event for a Task-spawned
    subagent carries ``agent_id`` + ``agent_type``; the derived file exists at
    tool-call time and holds the subagent's ``tool_use`` entries.

    Workflow-spawned subagents write their transcript DEEPER, at
    ``.../<session_id>/subagents/workflows/wf_<run>/agent-<agent_id>.jsonl``, so the
    flat derivation misses and the gate would silently fail open (observed: 3
    ``codegraph-gate CANARY`` fail-opens). When the flat path is absent we resolve
    the file under this session's ``subagents/workflows/*/`` -- only on a flat-path
    miss, so the common Task path and the "not yet written -> fail open" behavior are
    unchanged.
    """
    tp = str(event.get("transcript_path") or "")
    if not tp:
        return ""
    agent_id = str(event.get("agent_id") or "")
    if agent_id:
        # Path().name strips directory components; the regex below then ENFORCES the
        # "bare identifier" assumption -- a crafted agent_id with glob metacharacters
        # (*, ?, []) could otherwise match a SIBLING agent's file via the glob and
        # falsely unblock a grep-first agent. On an unexpected shape we fail open via
        # the flat path rather than glob.
        safe_id = Path(agent_id).name
        subagents_dir = Path(tp).with_suffix("") / "subagents"
        direct = subagents_dir / f"agent-{safe_id}.jsonl"
        if direct.exists():
            return str(direct)
        # Flat path absent: resolve the deeper workflow location with a precise,
        # non-recursive glob (known layout: subagents/workflows/wf_*/). A future
        # layout change is caught by drift_check.check_codegraph_transcript_format,
        # not silently absorbed here.
        if re.fullmatch(r"[A-Za-z0-9_-]+", safe_id):
            try:
                match = next(
                    iter(subagents_dir.glob(f"workflows/*/agent-{safe_id}.jsonl")),
                    None,
                )
            except OSError:
                match = None
            if match is not None:
                return str(match)
        # Not-yet-written, or an unexpected id shape: return the flat path so the
        # scan fails open (unchanged prior behavior).
        return str(direct)
    return tp


def _log_gate_canary(repo_root: Path, message: str) -> None:
    """Append a LOUD canary entry to the warden audit log.

    Used when the transcript-scanning gate detects a possible parse-blindness
    condition (rich transcript, no recognized tool_use blocks).  This makes a
    silent no-op VISIBLE in the warden log rather than invisible.
    """
    try:
        log = _audit_log_path(repo_root)
        log.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with log.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} | codegraph-gate   | CANARY  | {message}\n")
    except Exception:
        pass


def _bash_is_code_search(command: str) -> bool:
    """True if *command*'s PRIMARY token is a filesystem code search.

    Strips leading ``VAR=val`` assignments and ``sudo``. A search that appears
    only after a pipe/``;``/``&&`` (an output filter, e.g. ``ls | grep x``) is
    not the primary token and is allowed. Unparseable commands are not blocked
    (fail-open on ambiguity).
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if (
            "=" in tok
            and not tok.startswith("-")
            and tok.split("=", 1)[0].isidentifier()
        ):
            i += 1  # skip leading VAR=val assignment
            continue
        break
    if i < len(tokens) and tokens[i] == "sudo":
        i += 1
    if i >= len(tokens):
        return False
    base = tokens[i].rsplit("/", 1)[-1]  # handle /usr/bin/grep
    if base in _CODE_SEARCH_COMMANDS:
        return True
    if base == "git" and i + 1 < len(tokens) and tokens[i + 1] == "grep":
        return True
    return False


# ---------------------------------------------------------------------------
# Commit-window helpers
# ---------------------------------------------------------------------------

#: How long (seconds) a commit window stays active before it expires.
COMMIT_WINDOW_TTL_SECONDS: int = 60


def _in_commit_window(repo_root: Path) -> bool:
    """Return True if a fresh commit window marker exists (< TTL seconds old).

    During mark-batch the caller sets this marker so that any Edit/Write that
    fires between the first and last marker touch cannot invalidate a freshly
    approved marker.  The window is intentionally short and is consumed by
    session-init on the next session start.

    Security note: this is a convenience shortcut, NOT a security bypass.
    All wardens must still have produced SHIP verdicts before mark-batch is
    called.  Code edited *inside* the window will leave markers intact even
    though the diff changed — callers must understand this tradeoff and keep
    the window as short as possible (ideally no edits happen during it).
    """
    path = _marker(repo_root, ".commit-window")
    if not path.exists():
        return False
    try:
        age = dt.datetime.now(dt.UTC).timestamp() - path.stat().st_mtime
    except OSError:
        return False
    return age < COMMIT_WINDOW_TTL_SECONDS


def _set_commit_window(repo_root: Path) -> None:
    """Touch the commit-window marker to open (or refresh) a commit window."""
    path = _marker(repo_root, ".commit-window")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


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


def _extract_pr_ref(command: str) -> str | None:
    """Return the PR number, URL, or branch from a ``gh pr merge`` command.

    Scans past flags so ``gh pr merge --squash 294`` is handled correctly.
    """
    _FLAGS_WITH_VALUE = frozenset({
        "-R", "--repo", "-t", "--subject-body",
        "--match-head-commit", "--author",
        "-b", "--body", "-F", "--body-file", "-A", "--author-email",
    })
    tokens = _shell_tokens(command)
    for index, token in enumerate(tokens):
        if not _is_gh_executable(token):
            continue
        command_index = _gh_command_index_after_global_flags(tokens, index)
        if tokens[command_index : command_index + 2] != ["pr", "merge"]:
            continue
        i = command_index + 2
        while i < len(tokens):
            tok = tokens[i]
            if not tok.startswith("-"):
                return tok
            if "=" in tok:
                i += 1
                continue
            if tok in _FLAGS_WITH_VALUE:
                i += 2
                continue
            i += 1
        return None
    return None


_CI_STATUS_GREEN = "green"
_CI_STATUS_RED = "red"
_CI_STATUS_PENDING = "pending"
_CI_STATUS_NO_CHECKS = "no-checks"
# Checks exist on the PR but none are branch-protection-required — an ambiguous
# state we fail closed on rather than silently allow an unverified admin-merge.
_CI_STATUS_NO_REQUIRED = "no-required"
_CI_STATUS_ERROR = "error"

# Bucket values returned by ``gh pr checks --json bucket``
_BUCKET_PASS = frozenset({"pass", "skipping"})
_BUCKET_PENDING = frozenset({"pending"})
_BUCKET_FAIL = frozenset({"fail", "cancel"})


def _query_gh_checks(
    pr_ref: str, *, required_only: bool, timeout: int = 3
) -> tuple[str, str, int]:
    """Run ``gh pr checks`` once and classify the result.

    Returns ``(status, message, num_checks)`` where status is one of the
    ``_CI_STATUS_*`` constants and num_checks is how many checks were returned.
    When *required_only* is set, the query is scoped with ``--required`` so the
    gate sees only branch-protection-required checks. Failure to query defaults
    to ``_CI_STATUS_ERROR`` so the caller blocks rather than falls open.
    """
    argv = ["gh", "pr", "checks", pr_ref, "--json", "bucket,name"]
    if required_only:
        argv.append("--required")
    try:
        result = subprocess.run(
            argv,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return _CI_STATUS_ERROR, "gh CLI not found; cannot verify CI status", 0
    except subprocess.TimeoutExpired:
        return _CI_STATUS_ERROR, f"gh pr checks timed out after {timeout}s", 0
    except OSError as exc:
        return _CI_STATUS_ERROR, f"gh pr checks failed: {exc}", 0

    if result.returncode not in (0, 1, 8):
        # Exit code 1 = some checks failed (still parseable).
        # Exit code 8 = checks pending (still parseable).
        # Other codes indicate auth / network errors.
        stderr_snippet = result.stderr.strip()[:200]
        return (
            _CI_STATUS_ERROR,
            f"gh pr checks exited {result.returncode}: {stderr_snippet}",
            0,
        )

    raw = result.stdout.strip()
    if not raw:
        return _CI_STATUS_NO_CHECKS, "no checks found for this PR", 0

    try:
        checks = json.loads(raw)
    except json.JSONDecodeError:
        return _CI_STATUS_ERROR, "gh pr checks returned unparseable output", 0

    if not isinstance(checks, list):
        return _CI_STATUS_ERROR, "gh pr checks returned unexpected JSON shape", 0

    if not checks:
        return _CI_STATUS_NO_CHECKS, "no checks found for this PR", 0

    n = len(checks)
    buckets = {str(c.get("bucket", "")) for c in checks if isinstance(c, dict)}
    failed = [
        str(c.get("name", "?"))
        for c in checks
        if isinstance(c, dict) and str(c.get("bucket", "")) in _BUCKET_FAIL
    ]
    pending = [
        str(c.get("name", "?"))
        for c in checks
        if isinstance(c, dict) and str(c.get("bucket", "")) in _BUCKET_PENDING
    ]

    if failed:
        return _CI_STATUS_RED, f"failing checks: {', '.join(failed[:5])}", n
    if pending:
        return _CI_STATUS_PENDING, f"pending checks: {', '.join(pending[:5])}", n
    if buckets <= _BUCKET_PASS:
        return _CI_STATUS_GREEN, "all checks passed", n

    unknown = buckets - _BUCKET_PASS - _BUCKET_PENDING - _BUCKET_FAIL
    return _CI_STATUS_ERROR, f"unknown check buckets: {', '.join(sorted(unknown))}", n


def _check_ci_status(pr_ref: str, timeout: int = 3) -> tuple[str, str]:
    """Classify CI for *pr_ref*, scoped to branch-protection-required checks.

    The admin-merge gate must mirror branch protection — only checks the repo
    actually marks required (e.g. ``ci``) may block a merge, never
    advisory bots (TrueCourse, the platform test matrix, CodeQL) the repo
    deliberately left non-required. Applies to every caller of this function
    (the one-shot approve CLI, the PreToolUse hook, and merge_train).

    Falls closed: an unverifiable status — or a PR that has checks but none
    required — blocks rather than allowing an unreviewed admin-merge.
    """
    status, message, _ = _query_gh_checks(pr_ref, required_only=True, timeout=timeout)
    if status != _CI_STATUS_NO_CHECKS:
        return status, message

    # No REQUIRED checks reported. Disambiguate against the unfiltered set:
    # genuinely zero checks → allowed through (unchanged behaviour); checks
    # present but none required → ambiguous, fail closed.
    all_status, all_message, all_n = _query_gh_checks(
        pr_ref, required_only=False, timeout=timeout
    )
    if all_status == _CI_STATUS_ERROR:
        return all_status, all_message
    if all_n == 0:
        return _CI_STATUS_NO_CHECKS, "no checks found for this PR"
    # Thread the unfiltered status through so the operator sees WHAT is
    # outstanding (e.g. a failing advisory check), not just the ambiguity.
    return (
        _CI_STATUS_NO_REQUIRED,
        f"{all_n} check(s) present but none are branch-protection-required "
        f"(unfiltered: {all_status} — {all_message})",
    )


def _ci_block_reason(pr_ref: str, status: str, detail: str) -> str | None:
    """Return a block reason string if CI is not green, else ``None``."""
    if status == _CI_STATUS_GREEN:
        return None
    if status == _CI_STATUS_NO_CHECKS:
        return None
    if status == _CI_STATUS_RED:
        return (
            f"[admin-merge-gate] CI is red — autonomy grant is conditional on green. "
            f"Run `gh pr checks {pr_ref}` first.\n\n"
            f"Detail: {detail}"
        )
    if status == _CI_STATUS_PENDING:
        return (
            f"[admin-merge-gate] CI is pending — autonomy grant is conditional on green. "
            f"Run `gh pr checks {pr_ref}` first.\n\n"
            f"Detail: {detail}"
        )
    if status == _CI_STATUS_NO_REQUIRED:
        return (
            f"[admin-merge-gate] Branch protection reports no required checks for "
            f"{pr_ref}, yet the PR has checks — refusing admin-merge (fail-closed). "
            f"Inspect with `gh api repos/<owner>/<repo>/branches/main/protection` and "
            f"confirm the required-check names before merging.\n\n"
            f"Detail: {detail}"
        )
    # _CI_STATUS_ERROR — fail closed
    return (
        f"[admin-merge-gate] CI status could not be verified — blocking as a precaution. "
        f"Run `gh pr checks {pr_ref}` manually to confirm green, then retry.\n\n"
        f"Detail: {detail}"
    )


def _admin_merge_marker(repo_root: Path) -> Path:
    return _marker(repo_root, ".admin-merge-approved")


def _admin_merge_standing_marker(repo_root: Path) -> Path:
    """Path to the global/flat standing-grant marker.

    NOT per-worktree: a standing autonomy grant is host-wide and time-boxed
    (records the activating worktree for audit only). Intentionally absent from
    _PER_WORKTREE_MARKERS and the session-init clear list -- bounded by expiry.
    """
    return repo_root / ".claude" / ".admin-merge-standing"


def _active_script_path(repo_root: Path) -> Path:
    configured = os.environ.get("DEUS_CODEX_HOOK_SCRIPT_PATH")
    if configured:
        return Path(configured).expanduser().resolve(strict=False)
    return repo_root / "scripts" / "codex_warden_hooks.py"


def approve_admin_merge(command: str, repo_root: Path) -> int:
    pr_ref = _extract_pr_ref(command)
    # Current-branch merges (no ref) pass ``gh pr checks`` the branch name
    check_ref = pr_ref or "HEAD"
    status, detail = _check_ci_status(check_ref)
    block = _ci_block_reason(check_ref, status, detail)
    if block:
        print(block, file=sys.stderr)
        return 1

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


#: Standing-grant expiry defaults. The grant is a HARD time box; expiry_hours is
#: clamped to [0, MAX] so a config typo (e.g. 100000) cannot grant effectively
#: permanent autonomy, and <= 0 makes every grant immediately expired.
_STANDING_GRANT_DEFAULT_EXPIRY_HOURS = 24.0
_STANDING_GRANT_MAX_EXPIRY_HOURS = 168.0


def _standing_grant_config(repo_root: Path) -> tuple[bool, float]:
    """Read .claude/wardens/config.json admin-merge-gate.standing_grant.

    Returns (enabled, expiry_hours).  Fail-safe: an absent/non-dict/malformed
    config yields (False, default) so the gate falls back to strict one-shot.
    ``enabled`` is honoured only when it is exactly ``True``.
    """
    config = _wardens_config(repo_root)
    gate = config.get("admin-merge-gate")
    sg = gate.get("standing_grant") if isinstance(gate, dict) else None
    if not isinstance(sg, dict):
        return (False, _STANDING_GRANT_DEFAULT_EXPIRY_HOURS)
    enabled = sg.get("enabled") is True
    raw = sg.get("expiry_hours", _STANDING_GRANT_DEFAULT_EXPIRY_HOURS)
    # bool is a subclass of int -- reject it so `expiry_hours: true` is not 1h.
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raw = _STANDING_GRANT_DEFAULT_EXPIRY_HOURS
    expiry = max(0.0, min(float(raw), _STANDING_GRANT_MAX_EXPIRY_HOURS))
    return (enabled, expiry)


def _standing_grant_config_stanza(repo_root: Path) -> str:
    cfg = repo_root / ".claude" / "wardens" / "config.json"
    return (
        "[admin-merge-gate] Standing autonomy is OFF. To enable it, set this in\n"
        f"{cfg} (gitignored, host-local) and retry:\n\n"
        "  {\n"
        '    "admin-merge-gate": {\n'
        '      "standing_grant": { "enabled": true, "expiry_hours": 24 }\n'
        "    }\n"
        "  }\n\n"
        "While enabled, `gh pr merge --admin` runs without per-command approval "
        "for a PR whose branch matches the current worktree and whose "
        "code-review + verification verdicts are SHIP (CI must be green). The "
        f"grant expires after expiry_hours (max {int(_STANDING_GRANT_MAX_EXPIRY_HOURS)})."
    )


def approve_admin_merge_standing(repo_root: Path, worktree_root: Path) -> int:
    """Activate a time-boxed standing admin-merge autonomy grant.

    Requires the admin-merge-gate.standing_grant toggle to already be enabled in
    wardens/config.json (the durable opt-in); this records the activation time
    (the expiry anchor) and the activating worktree (audit only). No CI check
    here -- a standing grant spans multiple PRs, so CI is enforced per-merge at
    the gate, against the actual PR being merged.
    """
    enabled, expiry_hours = _standing_grant_config(repo_root)
    if not enabled:
        print(_standing_grant_config_stanza(repo_root), file=sys.stderr)
        return 1

    marker = _admin_merge_standing_marker(repo_root)
    reactivated = marker.exists()
    marker.parent.mkdir(parents=True, exist_ok=True)
    # Plain write (mirrors the one-shot .admin-merge-approved sibling) rather
    # than _write_atomic: the marker is tiny ephemeral state, a torn write is
    # fail-closed by the gate's guarded parse, and _write_atomic would leave
    # .bak-* files containing the prior absolute worktree_root.
    marker.write_text(
        json.dumps(
            {
                "worktree_root": str(worktree_root),
                "created_at": dt.datetime.now(dt.UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    if reactivated:
        print(
            "[admin-merge-gate] NOTE: a standing grant was already active; "
            "its expiry clock has been reset to now.",
            file=sys.stderr,
        )
    print(
        f"Standing admin-merge grant active for ~{expiry_hours:g}h "
        f"(activated from {worktree_root}). Each merge still requires green CI, "
        "a branch match to its worktree, and SHIP code-review + verification "
        "verdicts."
    )
    return 0


def _sync_atom_kinds_on_init(repo_root: Path) -> None:
    """Best-effort sync of DB atom_kind from on-disk frontmatter.

    Runs ``memory_tree.py sync-atom-kinds`` at SessionStart so that any
    kind-field mutations made outside the current session (e.g. via
    ``migrate_atom_tiers.py --apply`` or direct frontmatter edits) are
    propagated to the DB before the first retrieval.  Failures are logged
    to stderr and never block startup — the sync is opportunistic.

    Skips silently when:
    - ``DEUS_AUTO_MEMORY_DIR`` is unset (memory layer not configured)
    - the ``memory_tree.py`` script is absent (optional dependency)
    - the DB file does not yet exist (first-run / cold environment)
    """
    ext_dir = os.environ.get("DEUS_AUTO_MEMORY_DIR")
    if not ext_dir:
        return

    tree = repo_root / "scripts" / "memory_tree.py"
    if not tree.exists():
        return

    db_path = Path(
        os.environ.get("DEUS_MEMORY_TREE_DB", "~/.deus/memory_tree.db")
    ).expanduser()
    if not db_path.exists():
        return

    try:
        result = subprocess.run(
            [sys.executable, str(tree), "sync-atom-kinds", "--json"],
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[session-init] sync-atom-kinds failed: {exc}", file=sys.stderr)
        return

    if result.returncode != 0:
        print(
            f"[session-init] sync-atom-kinds exited {result.returncode}: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return

    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return

    fixed = data.get("fixed", [])
    if fixed:
        print(
            f"[session-init] sync-atom-kinds: reconciled {len(fixed)} stale atom_kind "
            f"value(s) — {', '.join(name for name, *_ in fixed)}",
            file=sys.stderr,
        )


def regenerate_codebase_map(repo_root: Path) -> int:
    """Regenerate .claude/codebase_map.md via scripts/codebase_map.py.

    Called from the pre-push hook to ensure the map is always fresh before
    a push lands on the remote. Uses SHA-based invalidation so it's a no-op
    on clean repos where the map is already current.

    Returns 0 on success, 1 on error.
    """
    script = repo_root / "scripts" / "codebase_map.py"
    if not script.exists():
        print(
            f"[codebase-map] scripts/codebase_map.py not found at {script} — skipping",
            file=sys.stderr,
        )
        return 0  # non-blocking: missing script is not a push blocker

    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=repo_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"[codebase-map] regeneration failed: {exc}", file=sys.stderr)
        return 0  # non-blocking: map regen failures must not block pushes

    if result.stdout.strip():
        print(f"[codebase-map] {result.stdout.strip()}")
    if result.returncode != 0:
        print(
            f"[codebase-map] codebase_map.py exited {result.returncode}: "
            f"{result.stderr.strip()}",
            file=sys.stderr,
        )
        return 0  # non-blocking
    return 0


def run_session_init(repo_root: Path) -> int:
    global _PATTERN_ROUTES_CACHE
    # .admin-merge-standing is intentionally absent -- it is bounded by expiry, not session lifetime.
    for name in (
        ".plan-reviewed",
        ".code-reviewed",
        ".threat-modeled",
        ".verified",
        ".ai-eng-reviewed",
        ".admin-merge-approved",
        ".migration-nudged",
        ".warden-memo.md",
        ".plan-scope.md",
        ".commit-window",
    ):
        _marker(repo_root, name).unlink(missing_ok=True)
    _PATTERN_ROUTES_CACHE = None
    _INJECTED_DOCS.clear()
    _sync_atom_kinds_on_init(repo_root)
    return 0


def _codegraph_deny_message(prior_searches: int) -> str:
    """Return an escalating deny message for the codegraph-first gate.

    The message tier is based on how many search-tool attempts (Grep, Glob, or
    Bash code-search) the agent has already made in its transcript, giving a
    repeatedly-blocked agent increasingly explicit instructions instead of
    cycling on the same polite hint.

    * prior_searches == 0  → polite hint (tier 0, preserved wording).
    * 1 <= prior_searches <= 2 → imperative + exact two-step sequence (tier 1).
    * prior_searches >= 3  → tier-1 text PLUS Read fallback for when the
      codegraph MCP server is unavailable (tier 2).
    """
    tier0 = (
        "[codegraph-first-gate] Call a codegraph or code_search tool first "
        '(ToolSearch "select:mcp__codegraph__codegraph_context"), then retry. '
        "core-behavioral-rules.md § Code Exploration."
    )
    tier1 = (
        "[codegraph-first-gate] Blocked again — stop retrying search. "
        "Run these two calls, in order, THEN retry: "
        '(1) ToolSearch(query="select:mcp__codegraph__codegraph_context"); '
        "(2) codegraph_context with your question. "
        "core-behavioral-rules.md § Code Exploration."
    )
    tier2 = (
        tier1
        + " If ToolSearch returns no codegraph tool (the MCP server is down), "
        "use Read on the specific files you need instead — do not keep retrying grep/find."
    )
    if prior_searches == 0:
        return tier0
    if prior_searches <= 2:
        return tier1
    return tier2


def run_codegraph_first_gate(event: dict[str, Any], repo_root: Path) -> int:
    """Single PreToolUse gate enforcing codegraph-first exploration for gated
    agents (``codegraph_gated: true``). LIA-121 / RETRO-2026-05-29-01.

    IMPLEMENTATION: transcript-scanning (replaces broken marker scheme).

    Empirically proven (LIA-121 Validate phase): mcp__codegraph__ hooks do NOT
    fire in subagent sessions, so the marker can never be set from a codegraph
    call.  The Grep/Glob/Bash-search PreToolUse hook DOES fire reliably, so:

    At every Grep/Glob/Bash-search event the gate reads the agent's transcript
    JSONL (``event["transcript_path"]``) and scans for a prior
    ``_line_is_codegraph_toolcall`` match.  If found → allow.  If not → block.

    Flush-timing guarantee (empirically confirmed): prior tool calls ARE written
    to the transcript before the next hook fires; no race condition.

    Fail-open: any internal error (IO, parse, missing path) returns 0 -- a gate
    bug must never hard-block an agent.

    Canary: when the transcript is "rich" (>= _BLIND_DETECTION_THRESHOLD
    assistant turns) but contains zero tool_use blocks of any kind, the gate
    logs a CANARY entry to .warden-log and fails open -- this discriminates
    between "agent hasn't called any tools yet" (short transcript, normal deny)
    and "gate can no longer parse the transcript format" (silent no-op, must be
    visible).
    """
    try:
        tool_name = str(event.get("tool_name") or "")
        # Determine whether this tool call is a search command to gate on.
        if tool_name in ("Grep", "Glob"):
            should_block = True
        elif tool_name == "Bash":
            tool_input = event.get("tool_input")
            command = ""
            if isinstance(tool_input, dict):
                command = str(tool_input.get("command") or "")
            should_block = _bash_is_code_search(command)
        else:
            # Non-search tool: always allow.
            return 0

        if not should_block:
            return 0

        # Scan the agent's OWN transcript for a prior codegraph call. For a
        # Task-spawned subagent this derives the per-subagent file from agent_id
        # (the raw transcript_path is the parent session file, which lacks the
        # subagent's tool calls). See _resolve_agent_transcript.
        transcript_path = _resolve_agent_transcript(event)
        if not transcript_path:
            # No transcript path → fail open (can't scan).
            _log_gate_canary(
                repo_root,
                f"transcript_path missing in hook event (tool={tool_name}); failing open",
            )
            return 0

        try:
            scan_result = _scan_transcript_for_codegraph(transcript_path)
        except Exception as exc:
            _log_gate_canary(
                repo_root,
                f"transcript scan raised {type(exc).__name__} for {transcript_path}; failing open",
            )
            return 0

        if scan_result is None:
            # IO error opening transcript (missing file, permission denied).
            # Fail open: the gate must not deadlock an agent due to an IO issue.
            _log_gate_canary(
                repo_root,
                f"transcript not readable: {transcript_path}; failing open",
            )
            return 0

        found, assistant_turns, any_tool_uses, prior_search_attempts = scan_result

        if found:
            return 0

        # Blindness detection: rich transcript with zero recognized tool_uses.
        if (
            assistant_turns >= _BLIND_DETECTION_THRESHOLD
            and any_tool_uses == 0
        ):
            _log_gate_canary(
                repo_root,
                f"transcript has {assistant_turns} assistant turns but 0 tool_use blocks "
                f"of any kind ({transcript_path}); CC format may have changed -- "
                "run `python3 scripts/drift_check.py --codegraph-format` to validate. "
                "Failing open to avoid deadlock.",
            )
            return 0

        _block_pre_tool(_codegraph_deny_message(prior_search_attempts))
        return 0
    except Exception:
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
        _marker(repo_root, ".warden-memo.md").unlink(missing_ok=True)
    return 0


def run_plan_review_gate(event: dict[str, Any], repo_root: Path) -> int:
    config = _wardens_config(repo_root)
    if not _warden_enabled(config, "plan-reviewer"):
        return 0
    tool_name = str(event.get("tool_name") or "")
    if tool_name and not _warden_has_tool(
        config, "plan-reviewer", tool_name,
        ["Edit", "Write", "MultiEdit", "apply_patch", "ExitPlanMode"],
    ):
        return 0

    # Marker check first — cheapest, satisfies the gate before any
    # subprocess work in `_managed_paths`.
    if _marker(repo_root, ".plan-reviewed").exists():
        return 0

    # ExitPlanMode has no file paths — skip _managed_paths (which would
    # escape via the empty-paths short-circuit) and block on marker alone.
    if tool_name == "ExitPlanMode":
        mark_cmd = (
            f"  python3 {shlex.quote(str(_active_script_path(repo_root)))} "
            f"mark plan-reviewed SHIP \"reason\" --repo-root {shlex.quote(str(repo_root))}"
        )
        if _last_verdict_is_blocking(repo_root, "plan-reviewer"):
            last = _last_verdict(repo_root, "plan-reviewer")
            reason = (
                f"[plan-review-gate] BLOCKED: last plan-reviewer verdict was {last}.\n\n"
                "Re-run the plan-reviewer after fixing the issues. Trivial bypass is "
                f"not permitted after {last} — no exceptions.\n\n"
                f"After SHIP:\n{mark_cmd}"
            )
        else:
            reason = (
                "[plan-review-gate] BLOCKED: no plan-reviewer approval marker.\n\n"
                "Run the plan-reviewer Warden for this project and wait for VERDICT: SHIP before "
                "exiting plan mode. Then run:\n\n"
                f"{mark_cmd}"
            )
        _block_pre_tool(reason)
        return 0

    # `_managed_paths` returns `(None, [])` outside every worktree;
    # otherwise `(worktree, paths_after_filtering)`. Empty `paths` after
    # filtering must NOT bypass the gate (the pre-fix `not paths` short-
    # circuit was the ExitPlanMode enforcement gap, PR #430).
    #
    # Scope note (LIA-77): this Python gate is intentionally scoped to deus
    # worktrees. Edits in non-git directories (vault, scratch, config files)
    # are covered by the user-level bash hook at ~/.claude/hooks/plan-review-gate.sh,
    # which falls back to the deus marker when not in a wardens-enabled repo.
    worktree, paths = _managed_paths(event, repo_root)
    if worktree is None:
        return 0

    # Disambiguate empty-paths: (a) all targets outside worktree → return 0;
    # (b) in-worktree targets filtered by `_is_excluded`/`_git_ignored` → BLOCK.
    if not paths:
        cwd = Path(str(event.get("cwd") or os.getcwd())).resolve(strict=False)
        any_in_worktree = any(
            _is_relative_to(p, worktree)
            for p in _event_paths(event, cwd)
        )
        if not any_in_worktree:
            return 0

    # BLOCK: in-worktree edit without marker. `paths` may still be empty
    # here when all targets were filtered (PR #430 invariant preserved).
    if paths:
        target_list = "\n".join(f"  - {path}" for path in paths[:5])
    else:
        target_list = "  - (filtered target — gate still applies)"
    mark_cmd = (
        f"  python3 {shlex.quote(str(_active_script_path(repo_root)))} "
        f"mark plan-reviewed SHIP \"reason\" --repo-root {shlex.quote(str(repo_root))}"
    )

    if _last_verdict_is_blocking(repo_root, "plan-reviewer"):
        last = _last_verdict(repo_root, "plan-reviewer")
        reason = (
            f"[plan-review-gate] BLOCKED: last plan-reviewer verdict was {last}.\n\n"
            "Re-run the plan-reviewer after fixing the issues. Trivial bypass is "
            f"not permitted after {last} — no exceptions.\n\n"
            f"After SHIP:\n{mark_cmd}\n\nTargets:\n{target_list}"
        )
    else:
        reason = (
            "[plan-review-gate] BLOCKED: no plan-reviewer approval marker.\n\n"
            "Before editing this project, run the plan-reviewer Warden and wait for "
            "VERDICT: SHIP. Then run:\n\n"
            f"{mark_cmd}\n\n"
            "Trivial-change bypass (typos, comments, single-line renames):\n"
            f"  python3 {shlex.quote(str(_active_script_path(repo_root)))} "
            f"mark plan-reviewed TRIVIAL \"reason\" --repo-root {shlex.quote(str(repo_root))}\n\n"
            f"Targets:\n{target_list}"
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
    if _read_verdict("code-reviewed", repo_root) == "SHIP":
        return 0

    mark_cmd = (
        f"  python3 {shlex.quote(str(_active_script_path(repo_root)))} "
        f"mark code-reviewed SHIP \"reason\" --repo-root {shlex.quote(str(repo_root))}"
    )

    if _last_verdict_is_blocking(repo_root, "code-reviewer"):
        last = _last_verdict(repo_root, "code-reviewer")
        reason = (
            f"[code-review-gate] BLOCKED: last code-reviewer verdict was {last}.\n\n"
            "Re-run the code-reviewer after fixing the issues. Trivial bypass is "
            f"not permitted after {last} — no exceptions.\n\n"
            f"After SHIP:\n{mark_cmd}"
        )
    else:
        reason = (
            "[code-review-gate] BLOCKED: no code-reviewer approval marker.\n\n"
            "Before committing changes, run the code-reviewer Warden and wait "
            "for VERDICT: SHIP. Then run:\n\n"
            f"{mark_cmd}\n\n"
            "Trivial-commit bypass (typos, deps, config-only):\n"
            f"  python3 {shlex.quote(str(_active_script_path(repo_root)))} "
            f"mark code-reviewed TRIVIAL \"reason\" --repo-root {shlex.quote(str(repo_root))}"
        )
    _block_pre_tool(reason)
    return 0


# Files that assemble prompts or call LLM APIs directly
_AI_ENG_BASENAMES = {
    "linear-dispatcher.ts", "linear-webhook.ts", "linear-notifications.ts",
    "linear-gate-specs.ts", "memory_indexer.py", "memory_tree.py",
}
# Directory prefixes whose children involve LLM logic (judge, agent specs)
_AI_ENG_DIR_PREFIXES = ("evolution/", ".claude/agents/")


def _diff_touches_llm_files(repo_root: Path) -> bool:
    """Check if staged/unstaged changes touch LLM-related files. Fail-closed."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, cwd=repo_root, timeout=10,
        )
        if result.returncode != 0:
            return True
        files = result.stdout.strip().split("\n") if result.stdout.strip() else []
        result2 = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True, text=True, cwd=repo_root, timeout=10,
        )
        if result2.returncode != 0:
            return True
        files += result2.stdout.strip().split("\n") if result2.stdout.strip() else []
    except Exception:
        return True
    for f in files:
        basename = f.split("/")[-1]
        if basename in _AI_ENG_BASENAMES:
            return True
        if f.startswith(_AI_ENG_DIR_PREFIXES):
            return True
    return False


def run_ai_eng_gate(event: dict[str, Any], repo_root: Path) -> int:
    config = _wardens_config(repo_root)
    if not _warden_enabled(config, "ai-eng-warden"):
        return 0

    cwd = Path(str(event.get("cwd") or os.getcwd())).resolve(strict=False)
    if _worktree_for_cwd(cwd, repo_root) is None:
        return 0

    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not GIT_COMMIT_RE.search(command):
        return 0
    if _marker(repo_root, ".ai-eng-reviewed").exists():
        return 0

    if not _diff_touches_llm_files(repo_root):
        return 0

    mark_cmd = (
        f"  python3 {shlex.quote(str(_active_script_path(repo_root)))} "
        f"mark ai-eng-reviewed SHIP \"reason\""
    )

    if _last_verdict_is_blocking(repo_root, "ai-eng-warden"):
        last = _last_verdict(repo_root, "ai-eng-warden")
        reason = (
            f"[ai-eng-gate] BLOCKED: last ai-eng-warden verdict was {last}.\n\n"
            "Re-run the ai-eng-warden after fixing the issues. Trivial bypass is "
            f"not permitted after {last} — no exceptions.\n\n"
            f"After SHIP:\n{mark_cmd}"
        )
    else:
        reason = (
            "[ai-eng-gate] BLOCKED: no AI engineering review marker.\n\n"
            "This commit touches LLM-related code. Run the ai-eng-warden and wait "
            "for VERDICT: SHIP. Then run:\n\n"
            f"{mark_cmd}\n\n"
            "Trivial-commit bypass (non-LLM changes only):\n"
            f"  python3 {shlex.quote(str(_active_script_path(repo_root)))} "
            f"mark ai-eng-reviewed TRIVIAL \"reason\""
        )
    _block_pre_tool(reason)
    return 0


def run_verification_gate(event: dict[str, Any], repo_root: Path) -> int:
    config = _wardens_config(repo_root)
    if not _warden_enabled(config, "verification-gate"):
        return 0

    cwd = Path(str(event.get("cwd") or os.getcwd())).resolve(strict=False)
    if _worktree_for_cwd(cwd, repo_root) is None:
        return 0

    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not GIT_COMMIT_RE.search(command):
        return 0
    if _read_verdict("verified", repo_root) == "SHIP":
        return 0

    mark_cmd = (
        f"  python3 {shlex.quote(str(_active_script_path(repo_root)))} "
        f"mark verified SHIP \"reason\""
    )

    if _last_verdict_is_blocking(repo_root, "verification-gate"):
        last = _last_verdict(repo_root, "verification-gate")
        reason = (
            f"[verification-gate] BLOCKED: last verification-gate verdict was {last}.\n\n"
            "Re-run the verification-gate after fixing the issues. Trivial bypass is "
            f"not permitted after {last} — no exceptions.\n\n"
            f"After SHIP:\n{mark_cmd}"
        )
    else:
        reason = (
            "[verification-gate] BLOCKED: no verification-gate approval marker.\n\n"
            "Before committing Deus changes, run the verification-gate Warden "
            "(subagent_type=\"verification-gate\") and wait for VERDICT: SHIP. "
            "The verification-gate confirms all task requirements were actually "
            "implemented with evidence. Pass the plan from .claude/.plan-reviewed "
            "(if present) or the commit message as requirements context.\n\n"
            f"After SHIP:\n{mark_cmd}\n\n"
            "Trivial-commit bypass (typos, deps, config-only):\n"
            f"  python3 {shlex.quote(str(_active_script_path(repo_root)))} "
            f"mark verified TRIVIAL \"reason\""
        )
    _block_pre_tool(reason)
    return 0


def run_verification_invalidator(event: dict[str, Any], repo_root: Path) -> int:
    # Fail-open on empty paths: filtered targets (gitignored,
    # `.claude/worktrees/<sub>/`, etc.) don't change the main-thread diff,
    # so the marker survives. The plan-review GATE fails closed on the
    # same condition — that asymmetry is intentional.
    #
    # git add is a staging-only operation, not a code edit — skip it so
    # that pattern-only commits don't lose their SHIP verdict.
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if isinstance(command, str) and command.startswith("git add"):
        return 0
    worktree, paths = _managed_paths(event, repo_root)
    if worktree is None:
        return 0
    if not paths:
        return 0
    if _in_commit_window(repo_root):
        print(
            "[verification-invalidator] skipping invalidation — inside commit window",
            file=sys.stderr,
        )
        return 0
    _marker(repo_root, ".verified").unlink(missing_ok=True)
    _clear_verdict("verified", repo_root)
    return 0


#: Standing-grant action outcomes returned by _evaluate_standing_grant.
_GRANT_ALLOW = "allow"
_GRANT_BLOCK = "block"
_GRANT_FALL_THROUGH = "fall_through"

#: Mandatory wardens (must be present AND SHIP) vs conditional (if present must
#: be SHIP; absence is fine -- a non-LLM / non-plan change legitimately never
#: ran ai-eng / threat-model / plan-review). Marker names map to warden keys via
#: MARKER_NAMES.
_STANDING_MANDATORY_MARKERS = ("code-reviewed", "verified")
_STANDING_CONDITIONAL_MARKERS = ("plan-reviewed", "ai-eng-reviewed", "threat-modeled")


def _parse_iso_utc(raw: Any) -> dt.datetime | None:
    """Parse an ISO-8601 timestamp into a tz-aware UTC datetime, or None.

    A naive timestamp is assumed UTC. Guards against the classic naive-vs-aware
    comparison TypeError -- the caller compares against ``dt.datetime.now(dt.UTC)``.
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def _verdict_in(verdicts: dict[str, Any], marker_name: str) -> str | None:
    """Return the verdict string for *marker_name* from a verdicts dict."""
    warden = MARKER_NAMES.get(marker_name)
    entry = verdicts.get(warden) if warden else None
    if isinstance(entry, dict):
        v = entry.get("verdict")
        return v if isinstance(v, str) else None
    return None


def _gh_pr_head_branch(ref: str, timeout: int = 3) -> str | None:
    """Resolve a PR ref (number or URL) to its head branch via ``gh pr view``.

    Returns None on any failure so the caller fails safe (treats it as an
    unverifiable match and falls through to the one-shot approval path).
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "view", ref, "--json", "headRefName"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    head = data.get("headRefName") if isinstance(data, dict) else None
    return head if isinstance(head, str) and head else None


def _pr_matches_worktree(command: str, wt: Path) -> tuple[bool, str]:
    """True iff the PR referenced by *command* has head branch == *wt*'s branch.

    A no-ref ``gh pr merge --admin`` targets the current branch (inherently
    *wt*'s PR). An explicit branch name is compared directly; a PR number/URL is
    resolved via ``gh pr view``. Anything unverifiable returns (False, reason).
    This binds the verdicts we read (this worktree's) to the PR being merged.
    """
    wt_branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD")
    if not wt_branch:
        return (False, "[admin-merge-gate] could not resolve the worktree branch")
    wt_branch = wt_branch.strip()
    ref = _extract_pr_ref(command)
    if ref is None or ref == wt_branch:
        return (True, "")
    head = _gh_pr_head_branch(ref)
    if head is None:
        return (
            False,
            f"[admin-merge-gate] could not verify PR '{ref}' belongs to this worktree",
        )
    if head == wt_branch:
        return (True, "")
    return (
        False,
        f"[admin-merge-gate] PR head branch '{head}' != worktree branch '{wt_branch}'",
    )


def _evaluate_standing_grant(
    repo_root: Path, wt: Path, command: str, expiry_hours: float
) -> tuple[str, str]:
    """Decide a standing admin-merge grant; return (action, reason).

    action is one of _GRANT_ALLOW / _GRANT_BLOCK / _GRANT_FALL_THROUGH. Pure
    except for the deliberate unlink of an expired/corrupt marker. Fail-closed:
    an unparseable marker, malformed timestamp, expiry, or any missing/non-SHIP
    mandatory verdict blocks -- never silently allows.
    """
    marker = _admin_merge_standing_marker(repo_root)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        marker.unlink(missing_ok=True)
        return (
            _GRANT_BLOCK,
            "[admin-merge-gate] standing grant marker is unreadable/corrupt and "
            "was cleared. Re-activate with `approve-admin-merge --standing`.",
        )
    if not isinstance(data, dict):
        marker.unlink(missing_ok=True)
        return (
            _GRANT_BLOCK,
            "[admin-merge-gate] standing grant marker is malformed and was "
            "cleared. Re-activate with `approve-admin-merge --standing`.",
        )

    created = _parse_iso_utc(data.get("created_at"))
    if created is None:
        marker.unlink(missing_ok=True)
        return (
            _GRANT_BLOCK,
            "[admin-merge-gate] standing grant has no valid created_at and was "
            "cleared. Re-activate with `approve-admin-merge --standing`.",
        )
    age_hours = (dt.datetime.now(dt.UTC) - created).total_seconds() / 3600.0
    if age_hours >= expiry_hours:
        marker.unlink(missing_ok=True)
        return (
            _GRANT_BLOCK,
            f"[admin-merge-gate] standing grant expired (age {age_hours:.1f}h >= "
            f"{expiry_hours:g}h limit) and was cleared. Re-activate with "
            "`approve-admin-merge --standing`.",
        )

    matched, why = _pr_matches_worktree(command, wt)
    if not matched:
        return (_GRANT_FALL_THROUGH, why)

    verdicts = _read_verdicts_at(_verdicts_path_for_worktree(repo_root, wt))
    for name in _STANDING_MANDATORY_MARKERS:
        v = _verdict_in(verdicts, name)
        if v != "SHIP":
            warden = MARKER_NAMES.get(name, name)
            return (
                _GRANT_BLOCK,
                f"[admin-merge-gate] standing grant requires a SHIP {warden} "
                f"verdict for this worktree; found {v or 'none'}. Run the "
                f"{warden} warden to SHIP, then retry.",
            )
    for name in _STANDING_CONDITIONAL_MARKERS:
        v = _verdict_in(verdicts, name)
        if v is not None and v != "SHIP":
            warden = MARKER_NAMES.get(name, name)
            return (
                _GRANT_BLOCK,
                f"[admin-merge-gate] standing grant blocked: {warden} verdict is "
                f"{v} (must be SHIP or absent). Re-run {warden}, then retry.",
            )
    return (_GRANT_ALLOW, "")


def run_admin_merge_gate(event: dict[str, Any], repo_root: Path) -> int:
    cwd = Path(str(event.get("cwd") or os.getcwd())).resolve(strict=False)
    wt = _worktree_for_cwd(cwd, repo_root)
    if wt is None:
        return 0

    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not _is_admin_merge_command(command):
        return 0

    pr_ref = _extract_pr_ref(command)
    check_ref = pr_ref or "HEAD"
    ci_status, ci_detail = _check_ci_status(check_ref)
    ci_block = _ci_block_reason(check_ref, ci_status, ci_detail)
    if ci_block:
        _block_pre_tool(ci_block)
        return 0

    # Standing autonomy grant (opt-in via wardens/config.json). CI-green is
    # already enforced above. When the toggle is on and an unexpired standing
    # marker exists, allow the merge WITHOUT per-command approval iff the PR's
    # branch matches this worktree and its mandatory verdicts (code-review +
    # verification) are SHIP. Verdicts are read from the worktree being merged,
    # so the grant can never authorise an unreviewed PR. A branch mismatch falls
    # through to the one-shot path; an unmet/expired condition blocks.
    enabled, expiry_hours = _standing_grant_config(repo_root)
    if enabled and _admin_merge_standing_marker(repo_root).exists():
        action, reason = _evaluate_standing_grant(repo_root, wt, command, expiry_hours)
        if action == _GRANT_ALLOW:
            return 0
        if action == _GRANT_BLOCK:
            _block_pre_tool(reason)
            return 0
        # _GRANT_FALL_THROUGH -> require the one-shot approval below.

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
    # Same fail-open-on-empty-paths invariant as run_verification_invalidator.
    #
    # git add is a staging-only operation, not a code edit — skip it so
    # that pattern-only commits don't lose their SHIP verdict.
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else ""
    if isinstance(command, str) and command.startswith("git add"):
        return 0
    worktree, paths = _managed_paths(event, repo_root)
    if worktree is None:
        return 0
    if not paths:
        return 0
    if _in_commit_window(repo_root):
        print(
            "[code-review-invalidator] skipping invalidation — inside commit window",
            file=sys.stderr,
        )
        return 0
    _marker(repo_root, ".code-reviewed").unlink(missing_ok=True)
    _clear_verdict("code-reviewed", repo_root)
    # LLM code is a subset of all code — source edits invalidate both markers
    _marker(repo_root, ".ai-eng-reviewed").unlink(missing_ok=True)
    return 0


def run_threat_model_gate(event: dict[str, Any], repo_root: Path) -> int:
    config = _wardens_config(repo_root)
    if not _warden_enabled(config, "threat-modeler"):
        return 0

    # Marker first — cheapest exit before any path resolution.
    if _marker(repo_root, ".threat-modeled").exists():
        return 0

    cwd = Path(str(event.get("cwd") or os.getcwd())).resolve(strict=False)
    worktree = _worktree_for_cwd(cwd, repo_root)
    if worktree is None:
        return 0  # cwd outside any Deus worktree — gate doesn't apply.

    # Run SECURITY_PATH_RE against raw event paths within the worktree,
    # bypassing `_managed_paths` — its `_is_excluded`/`.gitignore` filters
    # strip the very subagent-worktree and gitignored security paths we
    # want to warn about.
    matched = [
        path for path in _event_paths(event, cwd)
        if _is_relative_to(path, worktree)
        and SECURITY_PATH_RE.search(path.as_posix())
    ]
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


# --- Cold-memory injection helpers ---

_GOVERNS_ITEM_RE = re.compile(r"^\s+-\s+(.+?)(?:\s*#.*)?$", re.MULTILINE)
# 3800 leaves headroom within CONTEXT_LIMIT (6000) for header/footer + other systemMessages in same turn
_COLD_MEMORY_CHAR_CAP = 3800
_PATTERN_ROUTES_CACHE: list[tuple[str, Path]] | None = None
_INJECTED_DOCS: set[Path] = set()


def _load_pattern_routes(repo_root: Path) -> list[tuple[str, Path]]:
    global _PATTERN_ROUTES_CACHE
    if _PATTERN_ROUTES_CACHE is not None:
        return _PATTERN_ROUTES_CACHE

    patterns_dir = repo_root / "patterns"
    if not patterns_dir.is_dir():
        return []
    routes: list[tuple[str, Path]] = []
    for md_path in sorted(patterns_dir.glob("*.md")):
        if md_path.name == "INDEX.md":
            continue
        try:
            text = md_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        parts = text.split("---", 2)
        if len(parts) < 3:
            continue
        frontmatter = parts[1]
        items = _GOVERNS_ITEM_RE.findall(frontmatter)
        for item in items:
            item = item.strip().strip("\"'")
            if item:
                routes.append((item, md_path))
    routes.sort(key=lambda r: len(r[0]), reverse=True)
    _PATTERN_ROUTES_CACHE = routes
    return routes


def _match_pattern_docs(
    paths: list[Path], routes: list[tuple[str, Path]], worktree: Path
) -> list[Path]:
    matched: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            rel = path.relative_to(worktree).as_posix()
        except ValueError:
            continue
        for prefix, doc_path in routes:
            if doc_path in seen:
                continue
            if rel == prefix or rel.startswith(prefix.rstrip("/") + "/"):
                matched.append(doc_path)
                seen.add(doc_path)
    return matched


def run_cold_memory_injector(event: dict[str, Any], repo_root: Path) -> int:
    config = _wardens_config(repo_root)
    if not _warden_enabled(config, "cold-memory-injector"):
        return 0

    worktree, paths = _managed_paths(event, repo_root)
    if worktree is None or not paths:
        return 0

    routes = _load_pattern_routes(repo_root)
    if not routes:
        return 0

    matched_docs = _match_pattern_docs(paths, routes, worktree)
    new_docs = [d for d in matched_docs if d not in _INJECTED_DOCS]
    if not new_docs:
        return 0

    header = "=== Cold-memory injection (path-triggered conventions) ===\n"
    footer = "\n=== End cold-memory injection ==="
    budget = _COLD_MEMORY_CHAR_CAP
    parts: list[str] = []
    used = 0
    omitted = 0

    for doc_path in new_docs:
        try:
            content = doc_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        section = f"\n--- {doc_path.stem} ---\n{content}"
        if used + len(section) > budget:
            omitted += 1
            continue
        parts.append(section)
        used += len(section)
        _INJECTED_DOCS.add(doc_path)

    if not parts:
        return 0

    text = header + "".join(parts)
    if omitted:
        text += f"\n[{omitted} more pattern(s) matched but omitted - cap: {_COLD_MEMORY_CHAR_CAP} chars]"
    text += footer

    _debug(f"[cold-memory-injector] injected {used} chars from {len(parts)} doc(s)")
    _warn_post_tool(text)
    return 0


def _glob_match(rel_posix: str, pattern: str) -> bool:
    p = PurePosixPath(rel_posix)
    if hasattr(p, "full_match"):
        return p.full_match(pattern)
    return p.match(pattern)


def run_structural_check(event: dict[str, Any], repo_root: Path) -> int:
    config = _wardens_config(repo_root)
    if not _warden_enabled(config, "structural-check"):
        return 0

    worktree, paths = _managed_paths(event, repo_root)
    if worktree is None or not paths:
        return 0

    config_path = repo_root / ".claude" / "cold-memory" / "structural-checks.json"
    if not config_path.exists():
        return 0
    try:
        checks = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _debug(f"[structural-check] config parse error: {exc}")
        return 0

    check_list = checks.get("checks") if isinstance(checks, dict) else None
    if not isinstance(check_list, list):
        return 0

    findings: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(worktree).as_posix()
        except ValueError:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for check in check_list:
            if not isinstance(check, dict):
                continue
            glob_pat = check.get("glob", "")
            exclude_glob = check.get("exclude_glob")
            if not _glob_match(rel, glob_pat):
                continue
            if exclude_glob and _glob_match(rel, exclude_glob):
                continue
            pattern = check.get("pattern", "")
            try:
                if re.search(pattern, text):
                    msg = check.get("message", "pattern violation")
                    findings.append(f"  [{check.get('id', '?')}] {rel}: {msg}")
            except re.error as exc:
                _debug(f"[structural-check] bad regex in {check.get('id', '?')}: {exc}")

    if findings:
        _warn_post_tool(
            "[structural-check] WARNING: pattern violations found:\n\n"
            + "\n".join(findings[:10])
            + ("\n  [...more findings omitted]" if len(findings) > 10 else "")
        )
    return 0


def run_placement_guard(event: dict[str, Any], repo_root: Path) -> int:
    config = _wardens_config(repo_root)
    if not _warden_enabled(config, "placement-guard"):
        return 0

    cwd = Path(str(event.get("cwd") or os.getcwd())).resolve(strict=False)
    worktree = _worktree_for_cwd(cwd, repo_root)
    if worktree is None:
        return 0

    raw_paths = _event_paths(event, cwd)
    new_paths = [p for p in raw_paths if _is_relative_to(p, worktree) and not p.exists()]
    if not new_paths:
        return 0

    config_path = repo_root / ".claude" / "cold-memory" / "placement-rules.json"
    if not config_path.exists():
        return 0
    try:
        rules_data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _debug(f"[placement-guard] config parse error: {exc}")
        return 0

    rule_list = rules_data.get("rules") if isinstance(rules_data, dict) else None
    if not isinstance(rule_list, list):
        return 0

    warnings: list[str] = []
    for path in new_paths:
        try:
            rel = path.relative_to(worktree).as_posix()
        except ValueError:
            continue
        for rule in rule_list:
            if not isinstance(rule, dict):
                continue
            pattern = rule.get("path_pattern", "")
            try:
                if re.search(pattern, rel):
                    warnings.append(
                        f"  [{rule.get('id', '?')}] {rel}: {rule.get('message', 'placement issue')}"
                    )
            except re.error as exc:
                _debug(f"[placement-guard] bad regex in {rule.get('id', '?')}: {exc}")

    if warnings:
        _warn_post_tool(
            "[placement-guard] NOTICE: new file may be in the wrong location:\n\n"
            + "\n".join(warnings[:5])
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
        return f"[warn] CLAUDE.md not found: {state_file}"
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

    lines.extend(["", "--- CLAUDE.md pending (live from disk) ---"])
    if vault is None:
        lines.append("[warn] vault path unknown; cannot read CLAUDE.md")
    else:
        lines.append(_pending_block(vault / "CLAUDE.md"))
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


def _verdicts_path(repo_root: Path) -> Path:
    # Per-worktree: the code-review + verification gates decide on this store
    # (not the marker files), so it must be isolated alongside the markers.
    # Main repo resolves to the flat .claude/.warden-verdicts.json (back-compat).
    return _claude_marker_dir(repo_root) / ".warden-verdicts.json"


def _verdicts_path_for_worktree(repo_root: Path, worktree_root: Path) -> Path:
    # Deterministic verdict store for an EXPLICIT worktree (the admin-merge
    # standing gate resolves the cwd worktree itself rather than relying on
    # _current_worktree()'s os.getcwd() derivation). Mirrors _verdicts_path.
    return _marker_dir_for_worktree(repo_root, worktree_root) / ".warden-verdicts.json"


def _audit_log_path(repo_root: Path) -> Path:
    # Deliberately GLOBAL (flat), not per-worktree: this is an append-only audit
    # trail that aggregates verdicts across every worktree. Do not namespace it.
    return repo_root / ".claude" / ".warden-log"


def _bypass_log_path() -> Path:
    override = os.environ.get("DEUS_WARDEN_BYPASS_LOG")
    if override:
        return Path(override)
    return Path.home() / ".claude" / ".warden-bypass-log"


def _write_bypass_log(
    warden: str,
    verdict: str,
    session_type: str,
    reason: str,
    cwd: Path,
) -> None:
    try:
        diff_stats = _git(cwd, "diff", "--stat", "HEAD")
        entry = {
            "timestamp": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "warden": warden,
            "verdict": verdict,
            "session_type": session_type,
            "reason": reason,
            "cwd": str(cwd),
            "diff_stats": diff_stats,
        }
        path = _bypass_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
    except OSError:
        _debug("bypass log write failed")


def _is_bg_session() -> bool:
    return bool(os.environ.get("CLAUDE_JOB_DIR"))


def _read_verdicts_at(path: Path) -> dict[str, Any]:
    """Read a .warden-verdicts.json at an EXPLICIT path (no cwd derivation)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_verdicts(repo_root: Path) -> dict[str, Any]:
    return _read_verdicts_at(_verdicts_path(repo_root))


def _read_verdict(marker_name: str, repo_root: Path) -> str | None:
    """Return the verdict string for *marker_name* from .warden-verdicts.json.

    Maps the marker name (e.g. ``"code-reviewed"``) to the warden key used in
    the JSON (e.g. ``"code-reviewer"``) via ``MARKER_NAMES``.  Returns ``None``
    if the file is absent, malformed, or the entry is missing.
    """
    warden = MARKER_NAMES.get(marker_name)
    if not warden:
        return None
    data = _read_verdicts(repo_root)
    entry = data.get(warden)
    if not isinstance(entry, dict):
        return None
    v = entry.get("verdict")
    return v if isinstance(v, str) else None


def _clear_verdict(marker_name: str, repo_root: Path) -> None:
    """Remove the *marker_name* entry from .warden-verdicts.json.

    Maps the marker name to the warden key via ``MARKER_NAMES``.  Silently
    skips if the file is absent or the key is not present.
    """
    warden = MARKER_NAMES.get(marker_name)
    if not warden:
        return
    path = _verdicts_path(repo_root)
    data = _read_verdicts(repo_root)
    if warden not in data:
        return
    del data[warden]
    try:
        _write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")
    except OSError:
        _debug(f"_clear_verdict: failed to write {path}")


def _write_verdict(repo_root: Path, warden: str, verdict: str, reason: str, source: str = "manual") -> None:
    path = _verdicts_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_verdicts(repo_root)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    data[warden] = {"verdict": verdict, "ts": stamp, "reason": reason, "source": source}
    _write_atomic(path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    log = _audit_log_path(repo_root)
    safe_reason = reason.replace("|", "/").replace("\n", " ").strip()
    with log.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} | {warden:<15} | {verdict:<7} | {safe_reason}\n")


def _last_verdict(repo_root: Path, warden: str) -> str | None:
    data = _read_verdicts(repo_root)
    entry = data.get(warden)
    if isinstance(entry, dict):
        v = entry.get("verdict")
        return v if isinstance(v, str) else None
    return None


def _last_verdict_is_blocking(repo_root: Path, warden: str) -> bool:
    v = _last_verdict(repo_root, warden)
    return v in ("REVISE", "BLOCK")


VERDICT_RE = re.compile(
    r"^##\s*Verdict\s*:\s*(SHIP|REVISE|BLOCK)\b",
    re.MULTILINE,
)

WARDEN_SUBAGENT_TYPES = frozenset({"plan-reviewer", "code-reviewer", "threat-modeler", "verification-gate", "ai-eng-warden"})


def run_verdict_tracker(event: dict[str, Any], repo_root: Path) -> int:
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    subagent = str(tool_input.get("subagent_type") or tool_input.get("agent_type") or "")
    if subagent not in WARDEN_SUBAGENT_TYPES:
        return 0

    response = event.get("tool_response")
    if isinstance(response, dict):
        text = str(response.get("content") or response.get("response") or response.get("text") or "")
    elif isinstance(response, str):
        text = response
    elif isinstance(response, list):
        text = "\n".join(str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in response)
    else:
        return 0

    match = VERDICT_RE.search(text)
    if not match:
        return 0

    verdict = match.group(1).upper()
    _write_verdict(repo_root, subagent, verdict, f"{subagent} returned {verdict}", source="agent")
    _debug(f"verdict-tracker: {subagent} → {verdict}")
    return 0


def _find_importers(file_path: Path, repo_root: Path) -> list[str]:
    """Return list of files that import *file_path*, relative to *repo_root*.

    Searches ``src/`` for .ts files and ``evolution/`` + ``scripts/`` for .py
    files.  Returns paths relative to repo_root, or absolute if they fall
    outside repo_root.  Errors are swallowed so the hook stays fail-open.
    """
    suffix = file_path.suffix.lower()
    importers: list[str] = []

    if suffix == ".ts":
        search_dirs = [repo_root / "src"]
        # Match import/from/require lines that reference this module stem.
        stem = file_path.stem
        pattern = rf"(import|from|require).*['\"].*{re.escape(stem)}['\"]"
    elif suffix == ".py":
        search_dirs = [repo_root / "evolution", repo_root / "scripts"]
        stem = file_path.stem
        pattern = rf"(import|from).*\b{re.escape(stem)}\b"
    else:
        return importers

    for search_dir in search_dirs:
        if not search_dir.is_dir():
            continue
        try:
            result = subprocess.run(
                ["grep", "-rlE", pattern, str(search_dir)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=10,
            )
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                found = Path(line)
                # Exclude the file itself
                if found.resolve(strict=False) == file_path.resolve(strict=False):
                    continue
                try:
                    importers.append(str(found.relative_to(repo_root)))
                except ValueError:
                    importers.append(line)
        except (OSError, subprocess.TimeoutExpired) as exc:
            _debug(f"[memo-enricher] grep failed for {file_path}: {exc}")

    return importers


def _parse_memo_sections(text: str) -> tuple[list[str], list[str]]:
    """Extract existing bullet lines from each section of a warden memo.

    Returns (edited_file_lines, import_graph_lines) where each element is a
    list of raw ``- ...`` lines belonging to that section.  Lines that don't
    start with ``- `` are ignored (headings, blank lines, etc.).
    """
    edited: list[str] = []
    imports: list[str] = []
    in_edited = False
    in_imports = False
    for line in text.splitlines():
        if line.startswith("### Edited Files"):
            in_edited = True
            in_imports = False
        elif line.startswith("### Import Graph"):
            in_edited = False
            in_imports = True
        elif line.startswith("## ") or line.startswith("### "):
            in_edited = False
            in_imports = False
        elif line.startswith("- "):
            if in_edited:
                edited.append(line)
            elif in_imports:
                imports.append(line)
    return edited, imports


def run_memo_enricher(event: dict[str, Any], repo_root: Path) -> int:
    """Rebuild .warden-memo.md with edited-file info and import graph. Fails open."""
    worktree, paths = _managed_paths(event, repo_root)
    if worktree is None or not paths:
        return 0

    memo_path = _marker(repo_root, ".warden-memo.md")
    memo_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing memo to recover previously accumulated entries.
    existing_text = ""
    if memo_path.exists():
        try:
            existing_text = memo_path.read_text(encoding="utf-8")
        except OSError as exc:
            _debug(f"[memo-enricher] read failed: {exc}")

    # Recover previously accumulated entries from the existing memo.
    existing_file_lines, existing_import_lines = _parse_memo_sections(existing_text)

    # Build sets of already-recorded paths for deduplication.  The file path
    # backtick pattern appears in both section types, so we track at the
    # path-string level rather than the full line level.
    recorded_paths: set[str] = set()
    for line in existing_file_lines:
        # Extract `path` from "- `path`"
        if "`" in line:
            parts = line.split("`")
            if len(parts) >= 2:
                recorded_paths.add(parts[1])

    new_file_lines: list[str] = []
    new_import_lines: list[str] = []
    for file_path in paths:
        try:
            rel = str(file_path.relative_to(worktree))
        except ValueError:
            rel = str(file_path)

        if rel in recorded_paths:
            continue

        importers = _find_importers(file_path, repo_root)

        new_file_lines.append(f"- `{rel}`")
        if importers:
            callers = ", ".join(f"`{imp}`" for imp in importers[:10])
            new_import_lines.append(f"- `{rel}` ← {callers}")

    if not new_file_lines:
        return 0

    # Merge new entries with existing ones and rebuild the whole memo so that
    # ### Edited Files always precedes ### Import Graph, regardless of the
    # order in which multiple Edit events fired during this session.
    all_file_lines = existing_file_lines + new_file_lines
    all_import_lines = existing_import_lines + new_import_lines

    parts: list[str] = [
        "",
        "## Warden Memo (auto-generated)",
        "",
        "### Edited Files",
    ]
    parts.extend(all_file_lines)
    if all_import_lines:
        parts.append("")
        parts.append("### Import Graph")
        parts.extend(all_import_lines)

    try:
        memo_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    except OSError as exc:
        _debug(f"[memo-enricher] write failed: {exc}")

    return 0


def run_migration_nudge(event: dict[str, Any], repo_root: Path) -> int:
    """Once per session, check for pending migrations and emit a nudge."""
    marker = _marker(repo_root, ".migration-nudged")
    if marker.exists():
        return 0
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()

    migrations_dir = repo_root / "migrations"
    if not migrations_dir.exists():
        return 0
    state_file = repo_root / ".deus" / "migration-state.json"
    try:
        state = json.loads(state_file.read_text()) if state_file.exists() else {}
    except (json.JSONDecodeError, OSError):
        state = {}
    applied = set(state.get("applied", []))
    files = [f for f in os.listdir(migrations_dir) if re.match(r"^\d{4}-.+\.mjs$", f)]
    pending = [f.split("-")[0] for f in files if f.split("-")[0] not in applied]
    if not pending:
        return 0
    _additional_context(
        f"[deus] {len(pending)} pending migration(s). Run: npm run migrate"
    )
    return 0


RUNNERS = {
    "session-init": lambda event, repo: run_session_init(repo),
    "plan-review-gate": run_plan_review_gate,
    "plan-mode-invalidator": run_plan_mode_invalidator,
    "code-review-gate": run_code_review_gate,
    "ai-eng-gate": run_ai_eng_gate,
    "verification-gate": run_verification_gate,
    "admin-merge-gate": run_admin_merge_gate,
    "stop-checkpoint": run_stop_checkpoint,
    "memory-tree-hook": run_memory_tree_hook,
    "code-review-invalidator": run_code_review_invalidator,
    "verification-invalidator": run_verification_invalidator,
    "threat-model-gate": run_threat_model_gate,
    "path-leak-detector": run_path_leak_detector,
    "cold-memory-injector": run_cold_memory_injector,
    "structural-check": run_structural_check,
    "placement-guard": run_placement_guard,
    "catchup-freshness": run_catchup_freshness,
    "memory-retrieval": run_memory_retrieval,
    "memo-enricher": run_memo_enricher,
    "migration-nudge": run_migration_nudge,
    "orchestrator-preflight": run_orchestrator_preflight,
    "warden-verdict-tracker": run_verdict_tracker,
    "codegraph-first-gate": run_codegraph_first_gate,
}


MARKER_NAMES = {
    "plan-reviewed": "plan-reviewer",
    "code-reviewed": "code-reviewer",
    "ai-eng-reviewed": "ai-eng-warden",
    "threat-modeled": "threat-modeler",
    "verified": "verification-gate",
}


def mark_warden(marker_name: str, verdict: str, reason: str, repo_root: Path) -> int:
    warden = MARKER_NAMES.get(marker_name)
    if not warden:
        print(f"Unknown marker: {marker_name}. Valid: {', '.join(sorted(MARKER_NAMES))}", file=sys.stderr)
        return 1
    verdict = verdict.upper()
    if verdict not in ("SHIP", "TRIVIAL"):
        print(f"Invalid verdict: {verdict}. Must be SHIP or TRIVIAL.", file=sys.stderr)
        return 1

    bg = _is_bg_session()
    session_type = "bg" if bg else "interactive"

    if verdict == "TRIVIAL" and bg:
        _write_bypass_log(warden, "REFUSED", "bg", reason, repo_root)
        print(
            "[warden-mark] BLOCKED: TRIVIAL bypass is not permitted in background sessions.\n"
            "Background sessions must run the full warden and get SHIP.",
            file=sys.stderr,
        )
        return 2

    if verdict == "TRIVIAL" and _last_verdict_is_blocking(repo_root, warden):
        last = _last_verdict(repo_root, warden)
        if last:
            _write_bypass_log(warden, "REFUSED", session_type, reason, repo_root)
            print(
                f"[warden-mark] BLOCKED: last {warden} verdict was {last}.\n"
                "Re-run the warden and get SHIP — trivial bypass is not permitted after REVISE or BLOCK.",
                file=sys.stderr,
            )
            return 2

    _write_verdict(repo_root, warden, verdict, reason, source="mark")
    if verdict == "TRIVIAL":
        _write_bypass_log(warden, "TRIVIAL", session_type, reason, repo_root)
    _marker(repo_root, f".{marker_name}").parent.mkdir(parents=True, exist_ok=True)
    _marker(repo_root, f".{marker_name}").touch()
    print(f"[warden-mark] {marker_name} marked as {verdict}: {reason}")
    return 0


def mark_batch_wardens(specs: list[str], repo_root: Path) -> int:
    """Mark multiple wardens atomically inside a commit window.

    Each element of *specs* must be a colon-delimited triplet:
    ``"<marker_name>:<verdict>:<reason>"``.  The reason field may itself
    contain colons — only the first two colons are treated as delimiters.

    The function validates ALL entries before touching any file.  If any
    entry fails validation the function returns non-zero without writing
    anything.  Once all entries pass, it opens a commit window (so that
    any Edit/Write hook fired by the subsequent touches cannot invalidate
    a freshly-set marker), writes all marker files, then prints a summary.

    Backwards compatibility: individual ``mark`` calls continue to work
    unchanged.
    """
    # --- Parse and validate all specs first (fail-fast, atomic) ---
    parsed: list[tuple[str, str, str]] = []  # (marker_name, verdict, reason)
    for i, spec in enumerate(specs):
        parts = spec.split(":", 2)
        if len(parts) != 3:
            print(
                f"[warden-mark-batch] invalid spec at position {i}: {spec!r}\n"
                "Expected format: <marker_name>:<verdict>:<reason>",
                file=sys.stderr,
            )
            return 1
        marker_name, verdict, reason = parts
        verdict = verdict.upper()
        if marker_name not in MARKER_NAMES:
            print(
                f"[warden-mark-batch] unknown marker: {marker_name!r}. "
                f"Valid: {', '.join(sorted(MARKER_NAMES))}",
                file=sys.stderr,
            )
            return 1
        if verdict not in ("SHIP", "TRIVIAL"):
            print(
                f"[warden-mark-batch] invalid verdict {verdict!r} for {marker_name}. "
                "Must be SHIP or TRIVIAL.",
                file=sys.stderr,
            )
            return 1
        bg = _is_bg_session()
        if verdict == "TRIVIAL" and bg:
            warden = MARKER_NAMES[marker_name]
            _write_bypass_log(warden, "REFUSED", "bg", reason, repo_root)
            print(
                f"[warden-mark-batch] BLOCKED: TRIVIAL bypass not permitted in "
                f"background sessions (marker: {marker_name}).\n"
                "Background sessions must run the full warden and get SHIP.",
                file=sys.stderr,
            )
            return 2
        if verdict == "TRIVIAL" and _last_verdict_is_blocking(repo_root, MARKER_NAMES[marker_name]):
            last = _last_verdict(repo_root, MARKER_NAMES[marker_name])
            if last:
                warden = MARKER_NAMES[marker_name]
                session_type = "bg" if bg else "interactive"
                _write_bypass_log(warden, "REFUSED", session_type, reason, repo_root)
                print(
                    f"[warden-mark-batch] BLOCKED: last {warden} verdict was {last} "
                    f"(marker: {marker_name}).\n"
                    "Re-run the warden and get SHIP — trivial bypass is not permitted "
                    "after REVISE or BLOCK.",
                    file=sys.stderr,
                )
                return 2
        parsed.append((marker_name, verdict, reason))

    if not parsed:
        print("[warden-mark-batch] no specs provided.", file=sys.stderr)
        return 1

    # --- All entries valid: open commit window then write atomically ---
    _set_commit_window(repo_root)

    bg = _is_bg_session()
    session_type = "bg" if bg else "interactive"
    for marker_name, verdict, reason in parsed:
        warden = MARKER_NAMES[marker_name]
        _write_verdict(repo_root, warden, verdict, reason, source="mark-batch")
        if verdict == "TRIVIAL":
            _write_bypass_log(warden, "TRIVIAL", session_type, reason, repo_root)
        _marker(repo_root, f".{marker_name}").parent.mkdir(parents=True, exist_ok=True)
        _marker(repo_root, f".{marker_name}").touch()
        print(f"[warden-mark-batch] {marker_name} marked as {verdict}: {reason}")

    print(
        f"[warden-mark-batch] {len(parsed)} marker(s) set; commit window open for "
        f"{COMMIT_WINDOW_TTL_SECONDS}s."
    )
    return 0


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
    approve_parser.add_argument(
        "--command", dest="admin_command", required=False, default=None,
        help="The exact `gh pr merge --admin ...` command to approve (one-shot). "
             "Required unless --standing is given.",
    )
    approve_parser.add_argument(
        "--standing", action="store_true",
        help="Activate a time-boxed standing autonomy grant instead of approving "
             "a single command. Requires the admin-merge-gate.standing_grant "
             "toggle in .claude/wardens/config.json. While active, admin merges "
             "run without per-command approval for a PR whose branch matches its "
             "worktree and whose code-review + verification verdicts are SHIP.",
    )
    approve_parser.add_argument(
        "--worktree-root", default=None,
        help="Worktree recorded on the standing grant (audit only; defaults to "
             "the worktree of the current cwd). Only used with --standing.",
    )

    mark_parser = subparsers.add_parser("mark")
    mark_parser.add_argument("marker_name", choices=sorted(MARKER_NAMES))
    mark_parser.add_argument("mark_verdict", choices=["SHIP", "TRIVIAL"])
    mark_parser.add_argument("mark_reason")
    mark_parser.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])
    mark_parser.add_argument(
        "--worktree-root", default=None,
        help="Target worktree for the marker/verdict bucket (defaults to the "
             "worktree of the current cwd).",
    )

    mark_batch_parser = subparsers.add_parser(
        "mark-batch",
        help=(
            "Mark multiple wardens atomically inside a commit window.  "
            "Each SPEC is '<marker_name>:<verdict>:<reason>'.  All specs are "
            "validated before any file is written; a commit window is opened "
            "so intermediate Edit/Write hooks cannot invalidate the markers."
        ),
    )
    mark_batch_parser.add_argument(
        "specs",
        nargs="+",
        metavar="SPEC",
        help="One or more '<marker_name>:<verdict>:<reason>' triplets.",
    )
    mark_batch_parser.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])
    mark_batch_parser.add_argument(
        "--worktree-root", default=None,
        help="Target worktree for the marker/verdict bucket (defaults to the "
             "worktree of the current cwd).",
    )

    for name in ("install", "check", "uninstall"):
        sub = subparsers.add_parser(name)
        _add_common_install_args(sub)
        if name == "uninstall":
            sub.add_argument("--disable-feature", action="store_true")

    regen_parser = subparsers.add_parser(
        "regenerate-map",
        help="Regenerate .claude/codebase_map.md (SHA-invalidated, no-op if fresh)",
    )
    regen_parser.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])

    return parser


def _with_cli_worktree(repo_root: Path, worktree_root_arg: str | None, fn):
    """Run a CLI mark action with ``_WORKTREE_OVERRIDE`` set so marker + verdict
    writes land in the correct per-worktree bucket regardless of the process cwd.
    Restores the previous value on exit so direct test calls don't leak state.
    """
    global _WORKTREE_OVERRIDE
    if worktree_root_arg:
        wt = Path(worktree_root_arg).resolve(strict=False)
    else:
        wt = _worktree_for_cwd(Path.cwd(), repo_root)
        if wt is None:
            print(
                "[warden-mark] WARNING: cwd is not inside a worktree of "
                f"{repo_root}; markers/verdicts will use the main-repo (flat) "
                "bucket. Pass --worktree-root to target a specific worktree.",
                file=sys.stderr,
            )
            wt = repo_root
    prev = _WORKTREE_OVERRIDE  # nested calls are safe: prev is restored on exit
    _WORKTREE_OVERRIDE = wt
    try:
        return fn()
    finally:
        _WORKTREE_OVERRIDE = prev


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.action in {"install", "check", "uninstall"}:
        _finalize_paths(args)

    if args.action == "run":
        return run(args)
    if args.action == "mark":
        repo_root = Path(args.repo_root).resolve(strict=False)
        return _with_cli_worktree(
            repo_root,
            args.worktree_root,
            lambda: mark_warden(
                args.marker_name, args.mark_verdict, args.mark_reason, repo_root,
            ),
        )
    if args.action == "mark-batch":
        repo_root = Path(args.repo_root).resolve(strict=False)
        return _with_cli_worktree(
            repo_root,
            args.worktree_root,
            lambda: mark_batch_wardens(args.specs, repo_root),
        )
    if args.action == "approve-admin-merge":
        repo_root = Path(args.repo_root).resolve(strict=False)
        if args.standing:
            if args.worktree_root:
                wt = Path(args.worktree_root).resolve(strict=False)
            else:
                wt = _worktree_for_cwd(Path.cwd(), repo_root)
                if wt is None:
                    print(
                        "[admin-merge-gate] WARNING: cwd is not inside a worktree "
                        f"of {repo_root}; recording the main repo as the activating "
                        "worktree. Pass --worktree-root to be explicit.",
                        file=sys.stderr,
                    )
                    wt = repo_root
            return approve_admin_merge_standing(repo_root, wt)
        if not args.admin_command:
            print(
                "[admin-merge-gate] --command is required unless --standing is given.",
                file=sys.stderr,
            )
            return 2
        return approve_admin_merge(args.admin_command, repo_root)
    if args.action == "install":
        return install(args)
    if args.action == "check":
        return check(args)
    if args.action == "uninstall":
        return uninstall(args)
    if args.action == "regenerate-map":
        return regenerate_codebase_map(Path(args.repo_root).resolve(strict=False))
    raise AssertionError(args.action)


if __name__ == "__main__":
    sys.exit(main())
