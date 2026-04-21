#!/usr/bin/env python3
"""
Memory-tree PostToolUse hook. Re-embeds a vault node when Write/Edit/MultiEdit
touches a tracked markdown file. Silent; gated by DEUS_MEMORY_TREE=1.

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
    if os.environ.get("DEUS_MEMORY_TREE", "0") != "1":
        return "gate_off"
    fp = _file_path_from_hook(data)
    if not fp:
        return "bad_input"

    abs_path = Path(fp).expanduser().resolve()
    if abs_path.suffix != ".md":
        return "not_markdown"

    sys.path.insert(0, str(Path(__file__).parent))
    try:
        import memory_tree as mt
    except ImportError:
        return "import_failed"

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


def main():
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError):
        return
    dispatch(data)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
