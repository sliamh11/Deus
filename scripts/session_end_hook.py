#!/usr/bin/env python3
"""
SessionEnd hook: enqueue + debounced worker for the deterministic auto-save
mechanism (see auto_compress.py for what actually gets written and why the
mechanism has no LLM turn / injection surface).

Parent mode (the hook itself) must return fast and exit 0 always -- errors
surface on stderr, never block Claude Code.

Fires only for Claude Code sessions (SessionEnd is a Claude-Code-specific
hook event; other backends get zero auto-save coverage under this design --
a known, accepted scope limit, not a silent gap, consistent with
docs/decisions/hook-dispatch-facade-correction.md's finding that hook
*triggers* are Claude-Code-coupled).

Input: SessionEnd JSON on stdin,
  {"session_id", "transcript_path", "cwd", "hook_event_name", "reason"}

Worker mode (--worker <session_id>): sleeps out the debounce window, then
validates, claims, and runs auto_compress.py. The `com.deus.maintenance`
daily sweep (scripts/maintenance/compress_sweep.py) is the safety net for
anything a crashed/killed worker misses.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
from _time import utc_now  # noqa: E402
from stop_hook import (  # noqa: E402
    BG_COMPRESS_MIN_TURNS,
    _compress_already_ran,
    _count_transcript_turns,
    _is_bg_session,
    _load_vault_root,
)

QUEUE_DIR = Path(os.environ.get("DEUS_AUTO_COMPRESS_QUEUE_DIR", "~/.deus/compress_queue")).expanduser()
LOG_PATH = Path(os.environ.get("DEUS_AUTO_COMPRESS_LOG", "~/.deus/auto_compress.log")).expanduser()


def _debounce_minutes() -> float:
    try:
        v = float(os.environ.get("DEUS_AUTO_COMPRESS_DEBOUNCE_MIN", "30"))
        return v if v > 0 else 30.0
    except (TypeError, ValueError):
        return 30.0


def _worker_ceiling() -> int:
    try:
        v = int(os.environ.get("DEUS_AUTO_COMPRESS_TIMEOUT", "120"))
        return v if v > 0 else 120
    except (TypeError, ValueError):
        return 120


def _entry_path(session_id: str) -> Path:
    return QUEUE_DIR / f"{session_id}.json"


def _log_line(payload: dict) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError:
        pass


def _enqueue(session_id: str, transcript_path: str, cwd: str) -> None:
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    entry = _entry_path(session_id)
    tmp = entry.with_suffix(".json.tmp")
    try:
        # Sub-second precision matters: a session can reopen within the same
        # wall-clock second the queue entry was written, and truncating to a
        # whole second would silently mask that as "unchanged".
        mtime = Path(transcript_path).stat().st_mtime
    except OSError:
        mtime = 0.0
    payload = {
        "session_id": session_id,
        "transcript_path": transcript_path,
        "cwd": cwd,
        "queued_at": utc_now().isoformat(),
        "transcript_mtime": mtime,
        "attempts": 0,
    }
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, entry)


def _spawn_worker(session_id: str) -> None:
    """A session ending multiple times in quick succession (re-enqueue on
    each SessionEnd) spawns multiple concurrent sleeping worker processes for
    the same session_id -- harmless, not a race: each re-reads the entry
    after its own sleep, and only one wins the atomic os.rename claim in
    _worker_body; every loser sees FileNotFoundError and exits as a no-op."""
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker", session_id],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def main() -> None:
    """Fast parent path: validate, enqueue, spawn, return. Never raises past
    __main__'s own guard -- a slow/failing hook must not block Claude Code."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return

    if _is_bg_session():
        return  # bg sessions have their own separate blocking Stop-gate
    if os.environ.get("DEUS_AUTO_COMPRESS") == "0":
        return  # opt-out

    session_id = data.get("session_id")
    transcript_path = data.get("transcript_path")
    cwd = data.get("cwd")
    if not session_id or not transcript_path or not cwd:
        return

    if _load_vault_root(cwd=Path(cwd)) is None:
        return  # no vault resolvable -- avoid a wasted spawn

    if _count_transcript_turns(transcript_path) < BG_COMPRESS_MIN_TURNS:
        return  # trivial session

    if _compress_already_ran(transcript_path):
        return  # user already ran /compress interactively this session

    _enqueue(session_id, transcript_path, cwd)
    _spawn_worker(session_id)


def _run_worker(session_id: str) -> None:
    """Detached worker. Arms a wall-clock belt first (LIA-235 pattern) so a
    hung child can't leave an orphan."""
    ceiling = _worker_ceiling()
    debounce_s = _debounce_minutes() * 60
    timer = threading.Timer(debounce_s + ceiling + 120, os._exit, args=(0,))
    timer.daemon = True
    timer.start()
    try:
        _worker_body(session_id, debounce_s, ceiling)
    finally:
        timer.cancel()


def _worker_body(session_id: str, debounce_s: float, ceiling: int) -> None:
    time.sleep(debounce_s)

    entry = _entry_path(session_id)
    if not entry.exists():
        return  # already claimed/processed by another worker or the sweep

    try:
        payload = json.loads(entry.read_text())
    except (json.JSONDecodeError, OSError):
        return

    transcript_path = payload["transcript_path"]
    try:
        current_mtime = Path(transcript_path).stat().st_mtime
    except OSError:
        current_mtime = -1.0

    if current_mtime != payload.get("transcript_mtime"):
        # Session was re-opened after enqueue -- re-arm with fresh timestamps
        # rather than compressing a still-live session. The re-opened
        # session's own SessionEnd will re-spawn a worker when it truly ends;
        # the daily sweep is the backstop if it never comes.
        payload["queued_at"] = utc_now().isoformat()
        payload["transcript_mtime"] = current_mtime
        tmp = entry.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, entry)
        return

    if _compress_already_ran(transcript_path):
        return  # user compressed manually while this worker was sleeping

    running = entry.with_suffix(".json.running")
    try:
        os.rename(entry, running)
    except FileNotFoundError:
        return  # lost the claim race to another worker/the sweep

    process_entry(payload, running, ceiling)


def process_entry(payload: dict, running_path: Path, ceiling: int) -> None:
    """Shared claim->run->outcome path, reused by the sweep for stale
    recovery. `running_path` is the already-claimed `.json.running` file."""
    t0 = time.time()
    session_id = payload["session_id"]
    result = subprocess.run(
        [
            sys.executable, str(SCRIPTS_DIR / "auto_compress.py"),
            "--session-id", session_id,
            "--transcript-path", payload["transcript_path"],
            "--cwd", payload["cwd"],
            "--timeout", str(ceiling),
        ],
        timeout=ceiling + 10,
        capture_output=True,
        text=True,
    )
    duration_ms = int((time.time() - t0) * 1000)
    success = result.returncode == 0

    log_entry = {
        "ts": utc_now().isoformat(),
        "session_id": session_id,
        "is_error": not success,
        "duration_ms": duration_ms,
    }
    if not success and result.stderr:
        log_entry["stderr"] = result.stderr[-500:]
    _log_line(log_entry)

    if success:
        try:
            running_path.unlink()
        except OSError:
            pass
    else:
        payload["attempts"] = payload.get("attempts", 0) + 1
        json_path = running_path.with_suffix("")
        tmp = json_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, json_path)
        try:
            running_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    if "--worker" in sys.argv:
        try:
            _run_worker(sys.argv[sys.argv.index("--worker") + 1])
        except Exception as e:
            sys.stderr.write(f"[session-end-hook worker] {type(e).__name__}: {e}\n")
    else:
        try:
            main()
        except Exception as e:
            sys.stderr.write(f"[session-end-hook] {type(e).__name__}: {e}\n")
