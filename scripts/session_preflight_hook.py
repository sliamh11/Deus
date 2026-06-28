#!/usr/bin/env python3
"""SessionStart hook: warn when another live session is already working this tree.

Runs the concurrent-session collision detector (session_preflight.py) at every
session start and, if a CRITICAL collision is found, surfaces it as a WARN banner
via additionalContext. This is the auto-invocation half of LIA-284: the detector
is otherwise dormant infrastructure -- this hook fires it "at the moment a session
starts claiming work".

Design (Enforcement Layer hook, host-enforced, SessionStart):
- WARN, never block. The hook always exits 0; a false CRITICAL that blocked every
  session would be worse than the collision it prevents. The output is context only.
- Network-free: invokes the detector with --critical-only so the advisory probes
  (notably the network gh-PR check) are skipped -- only the fast local CRITICAL
  probes run, keeping session start snappy.
- Self-exclusion via the SessionStart payload's session_id (matches the registry's
  full-UUID sessionId field) so the starting session never flags itself.
- Fail-safe silent: any error (bad stdin, detector missing, timeout, non-JSON) ->
  emit nothing, exit 0. The hook must never break session start.
- Tree-relative: the detector runs in the hook's cwd (the starting session's working
  directory), so a session launched inside a worktree checks THAT worktree's tree, not
  ~/deus -- the collision check always targets the tree the session will actually write to.

Opt-out: DEUS_PREFLIGHT_HOOK=0 disables it.  # LIA-284
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_DETECTOR = _SCRIPTS_DIR / "session_preflight.py"

# Bounded so a wedged detector can't hang session start. Keep BELOW the hook's
# settings.json timeout (currently 8s) so this fail-safes before the harness
# kills us -- if you tune the settings.json timeout, keep it > this constant.
_DETECTOR_TIMEOUT = 6

# Cap each finding line so an adversarial/long git branch name or realpath can't
# bloat the injected context (a branch name is unbounded; a realpath ~PATH_MAX).
_MAX_DETAIL_CHARS = 200


def _emit(context: str) -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )


def _read_session_id() -> str:
    """Best-effort: pull session_id from the SessionStart stdin payload."""
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        return ""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    # Field name is `session_id` (snake_case) -- the documented Claude Code hook
    # input schema (docs/SDK_DEEP_DIVE.md: BaseHookInput = {session_id, ...}; also
    # container/agent-runner reads hookInput.session_id). It matches the registry's
    # full-UUID `sessionId` value, so --self excludes the starting session.
    sid = data.get("session_id") if isinstance(data, dict) else None
    return sid if isinstance(sid, str) else ""


def _run_detector(session_id: str) -> "dict | None":
    """Run session_preflight.py --critical-only --json; return parsed dict or None.

    None on any failure (missing detector, timeout, crash, non-JSON) -> caller
    emits nothing. The detector is invoked in the current working directory so it
    checks the tree the session is actually starting in.
    """
    if not _DETECTOR.is_file():
        return None
    cmd = [sys.executable, str(_DETECTOR), "--critical-only", "--json"]
    if session_id:
        cmd += ["--self", session_id]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_DETECTOR_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    # The detector exits CONFLICT(6) on a collision and 0 when clear -- both carry
    # valid JSON on stdout, so a non-zero return is expected, not an error.
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _banner(result: dict) -> "str | None":
    """Build the WARN banner from a CONFLICT result, or None if not a conflict."""
    if result.get("status") != "CONFLICT":
        return None
    findings = result.get("findings")
    if not isinstance(findings, list) or not findings:
        return None
    toplevel = result.get("toplevel")
    where = f" ({str(toplevel)[:_MAX_DETAIL_CHARS]})" if toplevel else ""
    lines = [
        f"⚠️ PREFLIGHT: {len(findings)} other live session(s) may be "
        f"working this git tree{where}:"
    ]
    for f in findings:
        detail = f.get("detail") if isinstance(f, dict) else None
        if detail:
            lines.append(f"  - {str(detail)[:_MAX_DETAIL_CHARS]}")
    lines.append(
        "  Pause before editing shared files; confirm with the user whether a "
        "worktree is needed (see: deus preflight)."
    )
    return "\n".join(lines)


def main() -> None:
    if os.environ.get("DEUS_PREFLIGHT_HOOK") == "0":  # LIA-284 opt-out
        # Still drain stdin so the producer never blocks on a full pipe.
        try:
            sys.stdin.read()
        except (OSError, UnicodeDecodeError):
            pass
        return
    session_id = _read_session_id()
    result = _run_detector(session_id)
    if result is None:
        return
    banner = _banner(result)
    if banner:
        _emit(banner)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # never break session start
        sys.stderr.write(f"[session-preflight-hook] {e}\n")
    sys.exit(0)
