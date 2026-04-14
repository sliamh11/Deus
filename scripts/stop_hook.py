#!/usr/bin/env python3
"""
Deus Stop Hook
Fires when Claude Code finishes a turn. Extracts the last few exchanges
from the transcript and writes a lightweight checkpoint to the vault
so /resume can restore context across sessions or after /compact.

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


def _load_vault_root() -> Path | None:
    """Resolve vault root from DEUS_VAULT_PATH env var or config.json."""
    env_path = os.environ.get("DEUS_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser()
    cfg_path = Path("~/.config/deus/config.json").expanduser()
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            vp = cfg.get("vault_path")
            if vp:
                return Path(vp).expanduser()
        except (json.JSONDecodeError, OSError):
            pass
    return None


def _load_checkpoints_dir() -> Path:
    """Resolve vault Checkpoints/ path. Silent fallback so the hook never blocks."""
    vault = _load_vault_root()
    if vault is not None:
        return vault / "Checkpoints"
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


# ── Memory-tree drift scan (Phase 5) ──────────────────────────────────────────

def _scan_vault_drift(vault: Path, limit: int = 5) -> int:
    """Re-embed up to `limit` tracked files whose mtime exceeds the last node
    update. Hash-gated via reembed_file — most edits cost 0. Gated by
    DEUS_MEMORY_TREE=1. Silent on all errors. Returns number attempted."""
    if os.environ.get("DEUS_MEMORY_TREE", "0") != "1":
        return 0
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import memory_tree as mt  # type: ignore
    except ImportError:
        return 0
    try:
        db = mt.open_db()
        rows = db.execute(
            "SELECT path, updated_at FROM nodes WHERE orphaned_at IS NULL"
        ).fetchall()
    except Exception:
        return 0
    candidates: list[tuple[int, str]] = []
    for path, updated_at in rows:
        try:
            full = vault / path
            if not full.exists():
                continue
            mtime = int(full.stat().st_mtime)
            if mtime > (updated_at or 0):
                candidates.append((mtime - (updated_at or 0), path))
        except OSError:
            continue
    candidates.sort(reverse=True)
    attempted = 0
    for _, path in candidates[:limit]:
        try:
            mt.reembed_file(vault, path, db)
            attempted += 1
        except Exception:
            continue
    return attempted


# ── Entry point ───────────────────────────────────────────────────────────────

def _maybe_drift_scan():
    vault = _load_vault_root()
    if vault is not None:
        try:
            _scan_vault_drift(vault, limit=5)
        except Exception:
            pass


def main():
    try:
        hook_data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        _maybe_drift_scan()
        return

    if should_checkpoint():
        transcript_path = hook_data.get("transcript_path", "")
        if transcript_path:
            turns = read_transcript(transcript_path)
            if len(turns) >= MIN_TURNS:
                write_checkpoint(turns)

    _maybe_drift_scan()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # Always silent
