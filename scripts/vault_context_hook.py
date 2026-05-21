#!/usr/bin/env python3
"""SessionStart hook: inject vault identity for sessions that lack it.

CLI sessions (via deus-cmd.sh) set DEUS_VAULT_PRELOADED=1 and inject vault
context via --append-system-prompt. This hook covers all other session types:
- Agent View (remote-control) sessions
- Ad-hoc Claude Code sessions opened in ~/deus

Skips when:
- DEUS_VAULT_PRELOADED=1 (CLI already has vault context)
- CWD is a worktree (.git is a file) — worktree agents get context via task prompt
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent

SEMANTIC_CACHE = Path.home() / ".deus" / "resume_semantic_cache.txt"
SEMANTIC_TTL = 14400  # 4 hours
MAX_SECTION_CHARS = 12000


def _load_config() -> dict:
    cfg_path = Path.home() / ".config" / "deus" / "config.json"
    try:
        return json.loads(cfg_path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _vault_path(config: dict) -> Path | None:
    env = os.environ.get("DEUS_VAULT_PATH")
    if env:
        return Path(env).expanduser()
    vp = config.get("vault_path", "")
    if vp:
        return Path(vp).expanduser()
    return None


def _load_vault_files(vault: Path, config: dict) -> str:
    autoload = config.get("vault_autoload", ["CLAUDE.md"])
    sections = []
    for fname in autoload:
        fpath = vault / fname
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")[:MAX_SECTION_CHARS]
            if content.strip():
                sections.append(f"=== VAULT: {fname} ===\n{content}")
        except OSError:
            continue
    return "\n\n".join(sections)


def _load_checkpoint(vault: Path) -> str:
    today = date.today().strftime("%Y-%m-%d")
    cp_dir = vault / "Checkpoints"
    if not cp_dir.is_dir():
        return ""
    matches = sorted(cp_dir.glob(f"{today}-*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not matches:
        return ""
    try:
        content = matches[0].read_text(encoding="utf-8", errors="replace")
        return f"=== MID-SESSION CHECKPOINT ===\n{content}" if content.strip() else ""
    except OSError:
        return ""


def _load_recent_sessions() -> str:
    indexer = _SCRIPTS_DIR / "memory_indexer.py"
    if not indexer.exists():
        return ""
    try:
        # sys.executable ensures same interpreter as the hook runner
        result = subprocess.run(
            [sys.executable, str(indexer), "--recent", "3"],
            capture_output=True, text=True, timeout=5,
        )
        output = result.stdout.strip()
        return f"=== RECENT SESSIONS ===\n{output}" if output else ""
    except (subprocess.TimeoutExpired, OSError):
        return ""


def _load_semantic_cache() -> str:
    if not SEMANTIC_CACHE.exists():
        return ""
    try:
        age = time.time() - SEMANTIC_CACHE.stat().st_mtime
        if age >= SEMANTIC_TTL:
            return ""
        content = SEMANTIC_CACHE.read_text(encoding="utf-8", errors="replace").strip()
        return f"=== RELATED SESSIONS ===\n{content}" if content else ""
    except OSError:
        return ""


def main() -> None:
    try:
        sys.stdin.read()
    except (OSError, UnicodeDecodeError):
        pass

    if os.environ.get("DEUS_VAULT_PRELOADED") == "1":
        return

    cwd = Path.cwd()
    if (cwd / ".git").is_file():
        return

    config = _load_config()
    vault = _vault_path(config)
    if not vault or not vault.is_dir():
        return

    sections = []

    vault_files = _load_vault_files(vault, config)
    if vault_files:
        sections.append(vault_files)

    checkpoint = _load_checkpoint(vault)
    if checkpoint:
        sections.append(checkpoint)

    recent = _load_recent_sessions()
    if recent:
        sections.append(recent)

    semantic = _load_semantic_cache()
    if semantic:
        sections.append(semantic)

    if not sections:
        return

    context = "\n\n".join(sections)
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    }
    json.dump(output, sys.stdout)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stderr.write(f"[vault-context-hook] {e}\n")
    sys.exit(0)
