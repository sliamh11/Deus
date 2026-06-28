#!/usr/bin/env python3
"""Read-only preflight: detect whether another session is already working this
git checkout before you start writing to it (LIA-284, multi-session parallelism).

Four probes against the current repo:
  1. Another live Claude session on the same working tree   -> CRITICAL
  2. The current branch checked out in another worktree     -> CRITICAL
  3. An open PR already targeting the current branch         -> WARNING
  4. Uncommitted files modified within the recency window    -> WARNING

Any CRITICAL finding exits CONFLICT(6) so a caller/hook can stop before the first
write; WARNING-only findings are advisory and exit SUCCESS(0).

Agent-native protocol (docs/decisions/printing-press-adoption.md): --json / --compact
/ --select, and DEUS_AGENT_NATIVE=1 auto-enables JSON.

Cross-platform: liveness uses session ``updatedAt`` freshness (portable) plus, on POSIX,
``os.kill(pid, 0)`` to confirm the pid is alive. On non-POSIX the pid check is skipped
(liveness = updatedAt-only, slightly weaker, no functional loss). Self-exclusion uses
``os.getpid()`` / ``os.getppid()`` (stdlib, identical on every platform) plus
--self / --self-pid / CLAUDE_SESSION_ID -- deliberately NO /proc or sysctl ancestor walk
(those are Linux-only / macOS-only and not portable).

Design: a table-driven probe pipeline. Each probe is a pure function
``probe(ctx) -> list[Finding]``; ``main()`` resolves the Context once, runs every probe,
and maps the highest severity to the exit code. No GoF pattern applies at this scale.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _exit_codes import (  # noqa: E402
    CONFLICT,
    INTERNAL_ERROR,
    SUCCESS,
    USAGE_ERROR,
)
from _agent_io import agent_output, is_agent_context  # noqa: E402

DEFAULT_WINDOW_MIN = 30
GIT_TIMEOUT = 10
GH_TIMEOUT = 15

CRITICAL = "CRITICAL"
WARNING = "WARNING"


class _UsageError(Exception):
    """Caller-fixable precondition failure (e.g. not a git repo)."""


@dataclass
class Finding:
    severity: str
    code: str
    detail: str

    def as_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code, "detail": self.detail}


@dataclass
class Context:
    toplevel: str
    branch: str | None
    window_min: int
    self_session_ids: set[str]
    self_pids: set[int]
    now_ms: int


# --------------------------------------------------------------------------- #
# helpers (module-level so tests can monkeypatch them)
# --------------------------------------------------------------------------- #
def _run_git(args: list[str], cwd: str | None = None, timeout: int = GIT_TIMEOUT):
    """Run a git command; return (returncode, stdout, stderr). Never raises."""
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (OSError, ValueError, subprocess.SubprocessError):
        # ValueError covers an embedded null byte in cwd (from a malformed session
        # file's cwd field) -> treat as a failed probe and skip, never crash main().
        return 1, "", "git invocation failed"


def _git_toplevel(path: str) -> str | None:
    rc, out, _ = _run_git(["rev-parse", "--show-toplevel"], cwd=path)
    if rc != 0:
        return None
    top = out.strip()
    return os.path.realpath(top) if top else None


def _pid_alive(pid: int) -> bool:
    """POSIX: os.kill(pid, 0). Non-POSIX: unknown -> assume alive (updatedAt gates)."""
    if os.name != "posix":
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, owned by another user
    except OSError:
        return True


def _sessions_dir():
    # The live session REGISTRY: ~/.claude/sessions/<pid>.json carries pid, cwd,
    # status (busy/idle) and updatedAt, and the file is rewritten as a heartbeat
    # while the session is alive. This is the liveness source -- NOT
    # ~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl, which are append-only
    # conversation transcripts with no pid/cwd/liveness signal.
    return os.path.join(os.path.expanduser("~"), ".claude", "sessions")


def _load_sessions():
    """Yield session dicts from ~/.claude/sessions/*.json; skip unreadable/malformed.

    Each yielded dict gets a synthetic ``_mtime_ms`` (file mtime in ms): the JSON
    ``updatedAt`` field only advances on a status transition and can lag many minutes
    behind a long-busy session, whereas the file is rewritten as a heartbeat. Liveness
    uses the fresher of the two so a busy session is not mistaken for abandoned.
    """
    d = _sessions_dir()
    if not os.path.isdir(d):
        return
    for name in sorted(os.listdir(d)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(d, name)
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            mtime_ms = int(os.path.getmtime(path) * 1000)
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            data["_mtime_ms"] = mtime_ms
            yield data


def _resolve_window_min(cli_value: int | None) -> int:
    """CLI flag wins; else env DEUS_PREFLIGHT_WINDOW_MIN (LIA-284); else default.

    Guarded parse: non-int / <= 0 falls back to the default (mirrors the
    ``Number.isFinite(n) && n > 0`` discipline for env-derived numbers).
    """
    if cli_value is not None and cli_value > 0:
        return cli_value
    raw = os.environ.get("DEUS_PREFLIGHT_WINDOW_MIN")  # LIA-284: multi-session preflight window
    if raw:
        try:
            n = int(raw)
        except ValueError:
            n = 0
        if n > 0:
            return n
    return DEFAULT_WINDOW_MIN


# --------------------------------------------------------------------------- #
# probes
# --------------------------------------------------------------------------- #
def probe_live_session_same_tree(ctx: Context) -> list[Finding]:
    """A live session whose cwd resolves to this same working tree."""
    findings: list[Finding] = []
    window_ms = ctx.window_min * 60 * 1000
    for s in _load_sessions():
        sid = s.get("sessionId")
        pid = s.get("pid")
        cwd = s.get("cwd")
        updated = s.get("updatedAt")
        if not cwd or not isinstance(updated, int):
            continue
        if sid and sid in ctx.self_session_ids:
            continue
        if isinstance(pid, int) and pid in ctx.self_pids:
            continue
        if _git_toplevel(cwd) != ctx.toplevel:
            continue
        # Freshness = the more recent of the status timestamp and the heartbeat mtime
        # (updatedAt lags a long-busy session; the file rewrite does not).
        mtime_ms = s.get("_mtime_ms", 0)
        last_active = updated if updated >= mtime_ms else mtime_ms
        age_ms = ctx.now_ms - last_active
        if age_ms > window_ms:
            continue
        if isinstance(pid, int) and not _pid_alive(pid):
            continue
        age_s = max(0, age_ms // 1000)
        sid_disp = (sid or "?")[:8]
        findings.append(
            Finding(
                CRITICAL,
                "live_session_same_tree",
                f"session {sid_disp} (pid {pid}) live on this tree, updated {age_s}s ago",
            )
        )
    return findings


def probe_branch_in_another_worktree(ctx: Context) -> list[Finding]:
    """The current branch is checked out in a worktree other than this one."""
    if not ctx.branch:
        return []
    rc, out, _ = _run_git(["worktree", "list", "--porcelain"], cwd=ctx.toplevel)
    if rc != 0:
        return []
    findings: list[Finding] = []
    path: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
            if path and branch == ctx.branch:
                rp = os.path.realpath(path)
                if rp != ctx.toplevel:
                    findings.append(
                        Finding(
                            CRITICAL,
                            "branch_in_other_worktree",
                            f"branch '{ctx.branch}' is checked out at {rp}",
                        )
                    )
        elif line == "":
            path = None
    return findings


def probe_open_pr_for_branch(ctx: Context) -> list[Finding]:
    """An open PR already has this branch as its head (best-effort; gh optional)."""
    if not ctx.branch:
        return []
    try:
        proc = subprocess.run(
            ["gh", "pr", "list", "--head", ctx.branch, "--state", "open", "--json", "number,title"],
            cwd=ctx.toplevel,
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return []  # gh absent / timeout -> advisory probe, skip silently
    if proc.returncode != 0:
        return []
    try:
        prs = json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    findings: list[Finding] = []
    for pr in prs if isinstance(prs, list) else []:
        title = str(pr.get("title", ""))[:60]
        findings.append(
            Finding(
                WARNING,
                "open_pr_for_branch",
                f"PR #{pr.get('number')} already targets '{ctx.branch}': {title}",
            )
        )
    return findings


def probe_recent_uncommitted(ctx: Context) -> list[Finding]:
    """Uncommitted files modified within the window (someone may be editing now).

    mtime-based, so it intentionally over-reports: ``git checkout -- <f>``, ``touch``,
    or a rebuild that rewrites a file all bump mtime without a human edit. That is why
    this is a WARNING (advisory, exit 0), never a CRITICAL.
    """
    rc, out, _ = _run_git(["status", "--porcelain"], cwd=ctx.toplevel)
    if rc != 0:
        return []
    window_s = ctx.window_min * 60
    now_s = ctx.now_ms / 1000
    recent: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:  # rename: "old -> new"
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        full = os.path.join(ctx.toplevel, path)
        try:
            mtime = os.path.getmtime(full)
        except OSError:
            continue
        if now_s - mtime <= window_s:
            recent.append(path)
    if not recent:
        return []
    sample = ", ".join(recent[:3])
    more = f" (+{len(recent) - 3} more)" if len(recent) > 3 else ""
    return [
        Finding(
            WARNING,
            "recent_uncommitted_edits",
            f"{len(recent)} uncommitted file(s) modified within {ctx.window_min}m: {sample}{more}",
        )
    ]


PROBES = [
    probe_live_session_same_tree,
    probe_branch_in_another_worktree,
    probe_open_pr_for_branch,
    probe_recent_uncommitted,
]

# The two probes that can produce a CRITICAL (blocking) finding -- both are local
# (git/filesystem) and fast. --critical-only runs exactly these, skipping the
# advisory WARNING probes: probe_open_pr_for_branch makes a network `gh pr list`
# call, so a session-start gate that only surfaces CRITICAL collisions avoids that
# latency entirely rather than waiting on a timeout.
CRITICAL_PROBES = [
    probe_live_session_same_tree,
    probe_branch_in_another_worktree,
]


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def _build_context(args) -> Context:
    cwd = os.getcwd()
    toplevel = _git_toplevel(cwd)
    if toplevel is None:
        raise _UsageError("not inside a git repository")
    rc, out, _ = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=toplevel)
    branch = out.strip() if rc == 0 else None
    if branch in ("", "HEAD"):
        branch = None  # detached HEAD -> branch probes are no-ops
    self_ids: set[str] = set()
    if args.self:
        self_ids.add(args.self)
    env_sid = os.environ.get("CLAUDE_SESSION_ID")
    if env_sid:
        self_ids.add(env_sid)
    self_pids = {os.getpid(), os.getppid()}
    if args.self_pid:
        self_pids.add(args.self_pid)
    return Context(
        toplevel=toplevel,
        branch=branch,
        window_min=_resolve_window_min(args.window_min),
        self_session_ids=self_ids,
        self_pids=self_pids,
        now_ms=int(time.time() * 1000),
    )


def _render_human(status: str, exit_code: int, findings: list[Finding]) -> str:
    lines = [f"{f.severity}  {f.code}  {f.detail}" for f in findings]
    if status == "CONFLICT":
        lines.append(f"CONFLICT: another session may be working this tree (exit {exit_code})")
    elif findings:
        lines.append(f"OK with warnings: no blocking collision (exit {exit_code})")
    else:
        lines.append(f"OK: no concurrent-session collision detected (exit {exit_code})")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="session_preflight.py",
        description="Detect concurrent-session collisions before writing to a checkout.",
    )
    parser.add_argument(
        "--window-min",
        type=int,
        default=None,
        help=f"Recency/liveness window in minutes (default {DEFAULT_WINDOW_MIN}; "
        "env DEUS_PREFLIGHT_WINDOW_MIN)",
    )
    parser.add_argument(
        "--self", dest="self", default=None, help="This session's sessionId, excluded from checks"
    )
    parser.add_argument(
        "--self-pid", type=int, default=None, help="This session's pid, excluded from checks"
    )
    parser.add_argument(
        "--critical-only",
        action="store_true",
        help="Run only the CRITICAL (blocking) probes; skip advisory WARNING probes "
        "(notably the network gh-PR check). Used by the session-start hook.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument("--compact", action="store_true", help="Compact JSON (strip nulls)")
    parser.add_argument("--select", default=None, help="Comma-separated field projection")
    args = parser.parse_args(argv)

    use_json = args.json or is_agent_context()

    try:
        ctx = _build_context(args)
    except _UsageError as exc:
        if use_json:
            print(json.dumps({"status": "ERROR", "exit_code": USAGE_ERROR, "error": str(exc)}))
        else:
            print(f"USAGE: {exc}", file=sys.stderr)
        return USAGE_ERROR

    probes = CRITICAL_PROBES if args.critical_only else PROBES
    try:
        findings: list[Finding] = []
        for probe in probes:
            findings.extend(probe(ctx))
    except Exception as exc:  # pragma: no cover - defensive last resort
        print(f"INTERNAL: {exc}", file=sys.stderr)
        return INTERNAL_ERROR

    has_critical = any(f.severity == CRITICAL for f in findings)
    exit_code = CONFLICT if has_critical else SUCCESS
    status = "CONFLICT" if has_critical else ("WARN" if findings else "OK")

    payload = {
        "status": status,
        "exit_code": exit_code,
        "toplevel": ctx.toplevel,
        "branch": ctx.branch,
        "findings": [f.as_dict() for f in findings],
    }

    out = agent_output(payload, use_json=use_json, compact=args.compact, select=args.select)
    print(out if out is not None else _render_human(status, exit_code, findings))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
