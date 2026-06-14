#!/usr/bin/env python3
"""
Memory-tree PostToolUse hook. Re-embeds a vault node when Write/Edit/MultiEdit
touches a tracked markdown file. Silent; runs when the memory-tree DB exists
(opt out with DEUS_MEMORY_TREE=0). The re-embed runs in a detached worker so the
edit never blocks on an embedding round-trip — only the vector (ranking) is
briefly stale, never the served text (read fresh from disk), and the Stop-hook
drift scan reconciles anything a worker misses.

Input: Claude Code hook JSON on stdin, e.g.
  {"hook_event_name": "PostToolUse", "tool_name": "Edit",
   "tool_input": {"file_path": "/abs/path/to/file.md", ...}, ...}

Exit is always 0 — a slow or failing hook must never block Claude Code.
"""

import json
import os
import sys
from pathlib import Path


def _vault_root() -> Path | None:
    env = os.environ.get("DEUS_VAULT_PATH")
    if env:
        return Path(env).expanduser()
    cfg = Path("~/.config/deus/config.json").expanduser()
    if cfg.exists():
        try:
            vp = json.loads(cfg.read_text()).get("vault_path", "")
        except (json.JSONDecodeError, OSError):
            return None
        return Path(vp).expanduser() if vp else None
    return None


def _file_path_from_hook(data: dict) -> str | None:
    """Extract file_path from tool_input across Write/Edit/MultiEdit payloads."""
    tool_input = data.get("tool_input") or {}
    fp = tool_input.get("file_path")
    if isinstance(fp, str) and fp:
        return fp
    return None


def _auto_memory_root() -> Path | None:
    env = os.environ.get("DEUS_AUTO_MEMORY_DIR")
    if env:
        return Path(env).expanduser()
    return None


def dispatch(data: dict) -> str:
    """Pure dispatch: returns a status string for tests; does not raise.

    Statuses: gate_off | bad_input | no_vault | not_vault_file | not_markdown |
              reembedded | unchanged | discovered | not_in_tree | no_id |
              no_description | missing | skipped_dir | already_tracked |
              embed_failed | import_failed | ext_reembedded | ext_not_in_tree
    """
    sys.path.insert(0, str(Path(__file__).parent))
    try:
        import memory_tree as mt
    except ImportError:
        return "import_failed"

    if not mt.tree_automation_enabled():
        return "gate_off"
    fp = _file_path_from_hook(data)
    if not fp:
        return "bad_input"

    abs_path = Path(fp).expanduser().resolve()
    if abs_path.suffix != ".md":
        return "not_markdown"

    # Check auto-memory dir first (external population).
    ext_root = _auto_memory_root()
    if ext_root is not None:
        try:
            rel_to_ext = abs_path.relative_to(ext_root.resolve())
            ns_path = mt.EXTERNAL_NAMESPACE + str(rel_to_ext)
            try:
                db = mt.open_db()
                status = mt.reembed_file(mt.resolve_vault_path(), ns_path, db)
                if status == "reembedded":
                    return "ext_reembedded"
                if status == "not_in_tree":
                    return "ext_not_in_tree"
                return status
            except Exception as exc:
                print(f"WARN: ext reembed failed: {exc}", file=sys.stderr)
                return "embed_failed"
        except (ValueError, OSError):
            pass

    # Fall through to vault path check.
    vault = _vault_root()
    if vault is None:
        return "no_vault"
    try:
        rel = abs_path.relative_to(vault.resolve())
    except (ValueError, OSError):
        return "not_vault_file"
    try:
        db = mt.open_db()
        status = mt.reembed_file(vault, str(rel), db)
        if status == "not_in_tree":
            return mt.discover_node(vault, str(rel), db)
        return status
    except Exception:
        return "embed_failed"


def _precheck_db_path() -> Path:
    """Cheap parent-side DB-path resolution (no memory_tree import) — keep this
    byte-for-byte in sync with memory_tree.DB_PATH's env/default
    (DEUS_MEMORY_TREE_DB, fallback ~/.deus/memory_tree.db) so the parent's spawn
    pre-check and the worker's authoritative mt.tree_automation_enabled() agree."""
    return Path(
        os.environ.get("DEUS_MEMORY_TREE_DB", "~/.deus/memory_tree.db")
    ).expanduser()


def _reembed_timeout() -> float:
    """Wall-clock ceiling for the detached worker (LIA-235: bound the child)."""
    try:
        v = float(os.environ.get("DEUS_MEMORY_TREE_REEMBED_TIMEOUT", "120"))
        return v if v > 0 else 120.0
    except (TypeError, ValueError):
        return 120.0


def main():
    """Fire-and-forget: spawn a detached worker to re-embed, then return at once
    so the edit never blocks. Cheap parent gate (no memory_tree import) skips the
    spawn on opt-out or when no tree DB exists."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return
    fp = _file_path_from_hook(data)
    if not fp:
        return
    if os.environ.get("DEUS_MEMORY_TREE") == "0":
        return
    if not _precheck_db_path().exists():
        return
    import subprocess

    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--worker", fp],
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _run_worker(file_path: str) -> None:
    """Detached worker: run the synchronous dispatch() under a wall-clock timer so
    a hung embed can't leave an orphan. Cross-platform (threading.Timer + os._exit,
    no POSIX-only signal.alarm). A timeout is an EXPECTED slow-embed condition the
    Stop-scan reconciles, so it exits 0 silently."""
    import threading

    # os._exit (not sys.exit) is intentional: a hard exit from the timer thread
    # to kill a hung embed without waiting on interpreter teardown.
    timer = threading.Timer(_reembed_timeout(), os._exit, args=(0,))
    timer.daemon = True
    timer.start()
    try:
        dispatch({"tool_input": {"file_path": file_path}})
    finally:
        timer.cancel()


if __name__ == "__main__":
    if "--worker" in sys.argv:
        try:
            _run_worker(sys.argv[sys.argv.index("--worker") + 1])
        except Exception:
            # Worker is silent — the Stop-hook drift scan reconciles misses.
            pass
    else:
        try:
            main()
        except Exception as e:
            # Never crash Claude Code, but surface the failure on stderr instead
            # of vanishing silently (LIA-246).
            sys.stderr.write(f"[memory-tree-hook] {type(e).__name__}: {e}\n")
