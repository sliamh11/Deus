#!/usr/bin/env python3
"""SessionStart hook: sync Linear pending tasks into vault CLAUDE.md.

Runs sync_linear_pending.py at every new session start to keep the pending:
block in vault CLAUDE.md at most 60 seconds stale (cache TTL), with zero
manual steps required.

Enforcement Layer hook (host-enforced, SessionStart).
Classification: context injection, host-only. See hook-dispatch-system.md.

Skips when:
- DEUS_PENDING_SYNCED=1 -- deus-cmd.sh already ran the sync for this session.
  Guard is currently speculative (deus-cmd.sh does not yet set this var);
  it is defensive/future-proof for when deus-cmd.sh is updated.
- CWD is a worktree (.git is a file) -- worktree agents get context via task prompt
- LINEAR_API_KEY is unset or Linear is unreachable -- fails silently, exit 0
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent

# Reuse the single safe pending-splice helper (indented-only replace + column-0
# body-key guard). Importing sync_linear_pending as a library is safe: its work
# is under main()/__main__. Do not add module-level side effects there.
sys.path.insert(0, str(_SCRIPTS_DIR))
from sync_linear_pending import _safe_replace_pending  # noqa: E402


def _load_config() -> dict:
    cfg_path = Path.home() / ".config" / "deus" / "config.json"
    try:
        return json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _vault_path(config: dict) -> "Path | None":
    # Dual lookup: env var override first, then config fallback.
    # Matches the pattern established by vault_context_hook.py.
    env = os.environ.get("DEUS_VAULT_PATH")
    if env:
        return Path(env).expanduser()
    vp = config.get("vault_path", "")
    if vp:
        return Path(vp).expanduser()
    return None


def _update_vault_pending(vault: "Path", pending_stdout: str) -> None:
    """Replace the pending: block in vault CLAUDE.md with fresh Linear content.

    pending_stdout is the raw stdout of sync_linear_pending.py -- the indented
    body lines only (no 'pending:' header). This function prepends the header
    and regex-replaces the entire block in-place.

    Note: if vault CLAUDE.md has no existing pending: block (e.g. first-time
    setup), the regex finds no match and the file is left unchanged. Run
    /compress first to plant the initial pending: block.
    """
    claude_md = vault / "CLAUDE.md"
    if not claude_md.exists():
        return

    content = claude_md.read_text(encoding="utf-8", errors="replace")

    # Shared safe splice: replaces only the indented pending block and ABORTS
    # (ValueError) rather than ever dropping a column-0 rule key. On no-match or
    # guard violation, leave the file unchanged (same as the prior no-match path).
    try:
        new_content = _safe_replace_pending(content, pending_stdout)
    except ValueError as e:
        sys.stderr.write(f"[linear-pending-hook] skipped pending update: {e}\n")
        return

    if new_content != content:
        claude_md.write_text(new_content, encoding="utf-8")


def main() -> None:
    try:
        sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        pass

    # Guard: skip if deus-cmd.sh already ran the sync for this session
    if os.environ.get("DEUS_PENDING_SYNCED") == "1":
        return

    # Skip in worktrees -- worktree agents get context via task prompt
    cwd = Path.cwd()
    if (cwd / ".git").is_file():
        return

    sync_script = _SCRIPTS_DIR / "sync_linear_pending.py"
    if not sync_script.exists():
        return

    try:
        result = subprocess.run(
            [sys.executable, str(sync_script)],
            capture_output=True,
            text=True,
            timeout=12,
        )
        if result.returncode == 0 and result.stdout.strip():
            config = _load_config()
            vault = _vault_path(config)
            if vault and vault.is_dir():
                _update_vault_pending(vault, result.stdout)
    except Exception as e:
        sys.stderr.write(f"[linear-pending-hook] {e}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(f"[linear-pending-hook] {e}\n")
    sys.exit(0)
