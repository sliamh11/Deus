#!/usr/bin/env python3
"""Concurrent-session collision detector.

Run this BEFORE a session starts writing to a repo. It reports whether another
live Claude/Codex session is already working the same checkout, the intended
branch is checked out in another worktree, an open PR already exists for the
branch, or files were just modified by something else. The point is to stop a
second session before the first file is written -- the failure that produced an
incoherent tree on 2026-06-18 (two sessions on one branch).

It is mostly read-only: it shells out to `git` and (optionally) `gh`, and reads
`~/.claude/sessions/*.json`. It never writes to the repo.

Exit codes (see _exit_codes.py):
  0 SUCCESS       -- clear, or only WARNING/SKIPPED findings (advisory use never blocks)
  6 CONFLICT      -- a CRITICAL finding (live session on this checkout, or branch
                     checked out in another worktree); a caller/hook should stop
  2 USAGE_ERROR   -- not a git repo / bad args
  5 INTERNAL_ERROR-- unexpected failure

Agent-native protocol: --json / --compact / --select, and DEUS_AGENT_NATIVE=1
auto-enables JSON (see docs/decisions/printing-press-adoption.md).

Wiring: invoke manually before claiming work, or from an optional local Claude
Code SessionStart hook (user-scope settings.json -- not shipped here, it is
interface-specific). A `deus preflight` CLI subcommand is a planned follow-on.
This change intentionally ships the detector only, unwired, so the portable core
lands first.

Cross-platform: liveness is primarily `updatedAt` freshness (portable). On POSIX
a secondary `os.kill(pid, 0)` confirms the pid is alive; on Windows that path is
skipped (liveness is updatedAt-only -- slightly weaker, no functional loss). The
self-session ancestor-pid walk is POSIX-only; on other platforms pass --self /
--self-pid or set CLAUDE_SESSION_ID to exclude the calling session.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from _agent_io import agent_output, is_agent_context
from _exit_codes import CONFLICT, INTERNAL_ERROR, SUCCESS, USAGE_ERROR

# Severity levels (worst-wins for the exit code).
CRITICAL = "critical"
WARNING = "warning"
SKIPPED = "skipped"

DEFAULT_WINDOW_MIN = 10


@dataclass
class Finding:
    check: str
    severity: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class Ctx:
    repo_root: Path
    branch: str
    window_min: int
    own_session_ids: set[str]
    own_pids: set[int]


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 10) -> tuple[int, str, str]:
    """Run a subprocess; return (rc, stdout, stderr). Never raises for the normal
    failure modes (missing binary, timeout, nonzero) -- callers turn those into
    SKIPPED findings rather than crashing the whole preflight."""
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        # rstrip only line-endings, never leading whitespace: `git status --porcelain`
        # encodes status in the first two columns, so a line like " M path" must keep
        # its leading space for the column-offset parse in probe_recent_writes.
        return proc.returncode, proc.stdout.rstrip("\r\n"), proc.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{cmd[0]}: timed out after {timeout}s"
    except OSError as exc:
        return 1, "", f"{cmd[0]}: {exc}"


def _git_toplevel(cwd: Path) -> Path | None:
    """Resolve the git working-tree root for `cwd`, or None if it is not a repo.
    Two paths share a checkout iff their toplevels are equal -- this correctly
    treats a subdirectory as the same checkout and a (nested) worktree as a
    different one."""
    rc, out, _ = _run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"])
    if rc != 0 or not out:
        return None
    try:
        return Path(out).resolve()
    except OSError:
        return None


def _ancestor_pids(max_depth: int = 8) -> set[int]:
    """The calling process and its ancestors (POSIX only). The invoking Claude/
    Codex session's pid is an ancestor of this script, so this lets probe_sessions
    exclude the caller without relying on an env var. Empty on non-POSIX."""
    pids: set[int] = set()
    if os.name != "posix":
        # Windows/other: no ps/getppid walk -- caller excludes itself via --self /
        # --self-pid / CLAUDE_SESSION_ID instead (see module docstring).
        return pids
    pid = os.getpid()
    for _ in range(max_depth):
        pids.add(pid)
        rc, out, _ = _run(["ps", "-o", "ppid=", "-p", str(pid)])
        if rc != 0 or not out.strip():
            break
        try:
            ppid = int(out.strip())
        except ValueError:
            break
        if ppid <= 1 or ppid == pid:
            break
        pid = ppid
    return pids


def _session_is_live(session: dict, window_ms: float, now_ms: float) -> bool:
    """Live = updated within the window. On POSIX, a dead pid overrides a fresh
    timestamp (stale tracker file); a fresh timestamp with no pid check passes."""
    updated = session.get("updatedAt")
    if not isinstance(updated, (int, float)) or (now_ms - updated) > window_ms:
        return False
    pid = session.get("pid")
    if os.name == "posix" and isinstance(pid, int):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
    return True


def probe_sessions(ctx: Ctx, now_ms: float | None = None) -> list[Finding]:
    """Other live sessions whose cwd is the SAME git checkout as ours. Same
    checkout => same branch => direct collision (the 06-18 failure)."""
    sessions_dir = Path.home() / ".claude" / "sessions"
    if not sessions_dir.is_dir():
        return [Finding("sessions", SKIPPED, "no ~/.claude/sessions directory")]
    if now_ms is None:
        now_ms = time.time() * 1000
    window_ms = ctx.window_min * 60 * 1000
    findings: list[Finding] = []
    for path in sorted(sessions_dir.glob("*.json")):
        try:
            session = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        sid = session.get("sessionId")
        pid = session.get("pid")
        if sid in ctx.own_session_ids or (isinstance(pid, int) and pid in ctx.own_pids):
            continue
        if not _session_is_live(session, window_ms, now_ms):
            continue
        cwd = session.get("cwd")
        if not cwd:
            continue
        try:
            cwd_real = Path(cwd).resolve()
        except OSError:
            continue
        if not cwd_real.exists() or _git_toplevel(cwd_real) != ctx.repo_root:
            continue
        findings.append(
            Finding(
                "sessions",
                CRITICAL,
                f"live session {sid or pid} ({session.get('status', '?')}) is on this "
                f"checkout (branch '{ctx.branch}')",
                {"pid": pid, "sessionId": sid, "status": session.get("status"), "cwd": str(cwd_real)},
            )
        )
    return findings


def probe_worktree(ctx: Ctx) -> list[Finding]:
    """The intended branch is checked out in a DIFFERENT worktree (can't write it
    here without racing that worktree)."""
    rc, out, err = _run(["git", "-C", str(ctx.repo_root), "worktree", "list", "--porcelain"])
    if rc != 0:
        return [Finding("worktree", SKIPPED, f"git worktree list failed: {err or rc}")]
    blocks: list[dict] = []
    cur: dict = {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
                cur = {}
        elif line.startswith("worktree "):
            cur = {"path": line[len("worktree ") :]}
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch ") :]
    if cur:
        blocks.append(cur)
    target_ref = f"refs/heads/{ctx.branch}"
    findings: list[Finding] = []
    for block in blocks:
        if block.get("branch") != target_ref:
            continue
        try:
            wpath = Path(block["path"]).resolve()
        except OSError:
            continue
        if wpath != ctx.repo_root:
            findings.append(
                Finding(
                    "worktree",
                    CRITICAL,
                    f"branch '{ctx.branch}' is already checked out in another worktree: {wpath}",
                    {"path": str(wpath), "branch": ctx.branch},
                )
            )
    return findings


def probe_open_pr(ctx: Ctx) -> list[Finding]:
    """An open PR already exists for the branch (someone may be iterating on it)."""
    rc, out, err = _run(
        ["gh", "pr", "list", "--head", ctx.branch, "--state", "open", "--json", "number,url,state"],
        cwd=str(ctx.repo_root),
    )
    if rc != 0:
        return [Finding("open_pr", SKIPPED, f"gh unavailable ({err or rc}); skipped PR check")]
    try:
        prs = json.loads(out) if out else []
    except json.JSONDecodeError:
        return [Finding("open_pr", SKIPPED, "gh output was not JSON")]
    return [
        Finding(
            "open_pr",
            WARNING,
            f"open PR #{pr.get('number')} already targets branch '{ctx.branch}': {pr.get('url')}",
            {"number": pr.get("number"), "url": pr.get("url")},
        )
        for pr in prs
    ]


def probe_recent_writes(ctx: Ctx, now: float | None = None) -> list[Finding]:
    """Uncommitted files modified within the window -- heuristic signal another
    session may be mid-write on this checkout. WARNING only: a bulk `git checkout`
    stamps uniform mtimes, so this both false-positives and false-negatives and is
    never escalated to CRITICAL nor relied on alone."""
    rc, out, err = _run(["git", "-C", str(ctx.repo_root), "status", "--porcelain"])
    if rc != 0:
        return [Finding("recent_writes", SKIPPED, f"git status failed: {err or rc}")]
    if now is None:
        now = time.time()
    window_s = ctx.window_min * 60
    recent: list[str] = []
    for line in out.splitlines():
        rel = line[3:].strip() if len(line) > 3 else ""
        if not rel:
            continue
        if " -> " in rel:  # rename: "old -> new"
            rel = rel.split(" -> ", 1)[1]
        rel = rel.strip('"')
        try:
            mtime = (ctx.repo_root / rel).stat().st_mtime
        except OSError:
            continue
        if (now - mtime) <= window_s:
            recent.append(rel)
    if recent:
        return [
            Finding(
                "recent_writes",
                WARNING,
                f"{len(recent)} uncommitted file(s) modified in the last {ctx.window_min}m "
                "(another session may be mid-write -- heuristic)",
                {"files": recent[:20], "count": len(recent)},
            )
        ]
    return []


PROBES = (probe_sessions, probe_worktree, probe_open_pr, probe_recent_writes)

_MARKERS = {CRITICAL: "[!]", WARNING: "[~]", SKIPPED: "[-]"}


def _render_human(payload: dict, findings: list[Finding]) -> None:
    repo = payload["repo"]
    branch = payload["branch"]
    print(f"preflight: {repo} (branch '{branch}', window {payload['windowMin']}m)")
    if not findings:
        print("  clear -- no other sessions, worktrees, PRs, or recent writes detected")
        return
    for f in findings:
        print(f"  {_MARKERS.get(f.severity, '[?]')} {f.check}: {f.message}")
    if payload["collision"]:
        print("\nCOLLISION RISK -- another session/worktree already owns this work. Stop and coordinate.")


def run(
    *,
    branch: str | None = None,
    window_min: int = DEFAULT_WINDOW_MIN,
    self_session: str | None = None,
    self_pid: int | None = None,
    use_json: bool = False,
    compact: bool = False,
    select: str | None = None,
) -> int:
    rc, top, _ = _run(["git", "rev-parse", "--show-toplevel"])
    if rc != 0 or not top:
        msg = "not inside a git repository"
        if use_json:
            print(json.dumps({"error": msg}))
        else:
            print(f"preflight: {msg}", file=sys.stderr)
        return USAGE_ERROR
    repo_root = Path(top).resolve()

    if branch is None:
        rcb, br, _ = _run(["git", "-C", str(repo_root), "rev-parse", "--abbrev-ref", "HEAD"])
        branch = br if rcb == 0 and br else "HEAD"

    own_session_ids = {s for s in (self_session, os.environ.get("CLAUDE_SESSION_ID")) if s}
    own_pids = _ancestor_pids()
    if self_pid:
        own_pids.add(self_pid)

    ctx = Ctx(repo_root, branch, window_min, own_session_ids, own_pids)

    findings: list[Finding] = []
    for probe in PROBES:
        try:
            findings.extend(probe(ctx))
        except Exception as exc:  # one probe must never crash the whole preflight
            findings.append(Finding(probe.__name__, SKIPPED, f"probe error: {exc}"))

    has_critical = any(f.severity == CRITICAL for f in findings)
    payload = {
        "repo": str(repo_root),
        "branch": branch,
        "windowMin": window_min,
        "collision": has_critical,
        "findings": [vars(f) for f in findings],
    }

    rendered = agent_output(payload, use_json=use_json, compact=compact, select=select, long_fields=("findings",))
    if rendered is not None:
        print(rendered)
    else:
        _render_human(payload, findings)

    return CONFLICT if has_critical else SUCCESS


def main() -> None:
    env_window = os.environ.get("DEUS_PREFLIGHT_WINDOW_MIN")
    try:
        default_window = int(env_window) if env_window else DEFAULT_WINDOW_MIN
    except ValueError:
        default_window = DEFAULT_WINDOW_MIN

    parser = argparse.ArgumentParser(
        description="Detect collisions with other sessions before writing to a repo."
    )
    parser.add_argument("--branch", default=None, help="Intended branch (default: current HEAD)")
    parser.add_argument(
        "--minutes",
        type=int,
        default=default_window,
        help=f"Liveness/recency window in minutes (default {default_window}; env DEUS_PREFLIGHT_WINDOW_MIN)",
    )
    parser.add_argument("--self", dest="self_session", default=None, help="This session's id, to exclude it")
    parser.add_argument("--self-pid", dest="self_pid", type=int, default=None, help="This session's pid, to exclude it")
    parser.add_argument("--json", dest="use_json", action="store_true", help="Emit structured JSON")
    parser.add_argument("--compact", action="store_true", help="Truncate long fields in JSON output")
    parser.add_argument("--select", default=None, help="Comma-separated top-level fields to keep in JSON")
    args = parser.parse_args()

    try:
        code = run(
            branch=args.branch,
            window_min=args.minutes,
            self_session=args.self_session,
            self_pid=args.self_pid,
            use_json=args.use_json or is_agent_context(),
            compact=args.compact,
            select=args.select,
        )
    except Exception as exc:  # last-resort guard -> INTERNAL_ERROR, never a traceback to the caller
        if os.environ.get("DEUS_DEBUG"):
            raise  # surface the stack during development
        print(f"preflight: internal error: {exc}", file=sys.stderr)
        sys.exit(INTERNAL_ERROR)
    sys.exit(code)


if __name__ == "__main__":
    main()
