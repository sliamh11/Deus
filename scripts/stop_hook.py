#!/usr/bin/env python3
"""
Deus Stop Hook
Fires when Claude Code finishes a turn. Extracts the last few exchanges
from the transcript and writes a lightweight checkpoint to the Obsidian
vault so /resume can restore context across sessions or after /compact.

No LLM calls — works offline, no quota risk, fast.
Throttled: at most one checkpoint per 30 minutes.
Silent on all errors (must not interrupt Claude Code).

Input: JSON from Claude Code on stdin
  { "session_id": "...", "hook_event_name": "Stop", "transcript_path": "..." }
"""

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────


def _load_checkpoints_dir() -> Path:
    """Resolve vault Checkpoints/ path from config.json or DEUS_VAULT_PATH env var."""
    env_path = os.environ.get("DEUS_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser() / "Checkpoints"
    cfg_path = Path("~/.config/deus/config.json").expanduser()
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            if cfg.get("vault_path"):
                return Path(cfg["vault_path"]).expanduser() / "Checkpoints"
        except (json.JSONDecodeError, OSError):
            pass
    # Silent fallback — stop hook must never block Claude Code
    return Path("~/.deus/checkpoints").expanduser()


CHECKPOINTS_DIR = _load_checkpoints_dir()
THROTTLE_MINUTES = 30
MIN_TURNS = 4       # skip trivial sessions
KEEP_TURNS = 6      # turns to include in checkpoint (last N with text)
MAX_TURN_CHARS = 400  # truncate each turn at this length

# ── Helpers ───────────────────────────────────────────────────────────────────

def should_checkpoint() -> bool:
    if not CHECKPOINTS_DIR.exists():
        return True
    files = sorted(
        CHECKPOINTS_DIR.glob("*.md"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not files:
        return True
    age = datetime.now() - datetime.fromtimestamp(files[0].stat().st_mtime)
    return age > timedelta(minutes=THROTTLE_MINUTES)


def read_transcript(path: str) -> list[dict]:
    """Parse JSONL transcript into [{role, text}] — only turns with actual text."""
    p = Path(path)
    if not p.exists():
        return []
    turns = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("type") not in ("user", "assistant"):
            continue
        msg = entry.get("message", {})
        role = msg.get("role", entry.get("type"))
        content = msg.get("content", "")
        if isinstance(content, str):
            text = content.strip()
        elif isinstance(content, list):
            # Extract only text blocks (skip tool_use, thinking, etc.)
            text = "\n".join(
                b.get("text", "").strip()
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
        else:
            continue
        if text:
            turns.append({"role": role, "text": text})
    return turns


def extract_topic(turns: list[dict]) -> str:
    """Derive a slug from the first real user message (skip injected command content)."""
    import re
    for t in turns:
        if t["role"] != "user":
            continue
        text = t["text"]
        # Skip command-injected messages (XML tags, command expansions with paths/steps)
        if "<command-" in text or "/Users/" in text or re.search(r"\n\d+\.\s+\w", text):
            continue
        # Strip any remaining tags and clean up
        text = re.sub(r"<[^>]+>", " ", text)
        words = [
            w.lower().strip(".,!?:")
            for w in text.split()
            if w and not w.startswith("/") and not w.startswith("http") and len(w) > 2
        ]
        if len(words) >= 2:
            return "-".join(words[:4])
    return "session"


def write_checkpoint(turns: list[dict]):
    CHECKPOINTS_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    path = CHECKPOINTS_DIR / f"{now.strftime('%Y-%m-%d-%H')}.md"

    topic = extract_topic(turns)
    recent = turns[-KEEP_TURNS:]

    lines = []
    for t in recent:
        text = t["text"][:MAX_TURN_CHARS]
        if len(t["text"]) > MAX_TURN_CHARS:
            text += "…"
        lines.append(f"**{t['role']}:** {text}")

    body = "\n\n".join(lines)

    content = f"""---
type: checkpoint
created: {now.strftime('%Y-%m-%dT%H:%M')}
session_topic: {topic}
status: auto
---

## Recent Context
{body}
"""
    path.write_text(content, encoding="utf-8")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    try:
        hook_data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return

    if not should_checkpoint():
        return

    transcript_path = hook_data.get("transcript_path", "")
    if not transcript_path:
        return

    turns = read_transcript(transcript_path)
    if len(turns) < MIN_TURNS:
        return

    write_checkpoint(turns)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # Always silent
