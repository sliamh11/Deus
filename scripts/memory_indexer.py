#!/usr/bin/env python3
"""
Deus Memory Indexer
Semantic search over session logs using sqlite-vec + Gemini embeddings.

Usage:
  python3 memory_indexer.py --add <path/to/session_log.md>
  python3 memory_indexer.py --query "linear algebra exam prep"  [--top 3]
  python3 memory_indexer.py --rebuild
  python3 memory_indexer.py --extract <path/to/session_log.md>
  python3 memory_indexer.py --wander [topic1 topic2 ...]
"""

import argparse
import json
import os
import re
import sqlite3
import struct
import sys
from datetime import datetime
from pathlib import Path

# Allow running as a direct script — add project root to sys.path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

import sqlite_vec
from google import genai
from google.genai import types as genai_types

from evolution.config import (
    EMBED_DIM,
    EVOLUTION_DB_PATH,
    GEN_MODELS,
    load_api_key as _load_api_key,
)

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path("~/.config/deus/config.json").expanduser()
DB_PATH = Path("~/.deus/memory.db").expanduser()
LAST_RESUME_LEARNINGS = Path("~/.deus/last_resume_learnings.txt").expanduser()
HEALTH_LOG_PATH = Path("~/.deus/memory_health.jsonl").expanduser()


def _load_vault_path() -> Path:
    """Load vault path from config.json or DEUS_VAULT_PATH env var."""
    # 1. Environment variable override
    env_path = os.environ.get("DEUS_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser()
    # 2. Config file
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            if cfg.get("vault_path"):
                return Path(cfg["vault_path"]).expanduser()
        except (json.JSONDecodeError, OSError):
            pass
    # 3. Fatal — no vault configured
    print(
        "ERROR: Memory vault not configured.\n"
        "Set DEUS_VAULT_PATH or add vault_path to ~/.config/deus/config.json\n"
        "Run `deus setup` or /setup in Claude Code to configure.",
        file=sys.stderr,
    )
    sys.exit(1)


_vault_root = _load_vault_path()
VAULT_SESSION_LOGS = _vault_root / "Session-Logs"
VAULT_ATOMS = _vault_root / "Atoms"
DEDUP_L2_THRESHOLD = 0.55  # ≈ cosine similarity 0.85 for unit-normalized vectors
# Recency boost for --query --recency-boost (subtracted from L2 distance).
RECENCY_BOOST_7D = 0.3    # last 7 days — strong boost
RECENCY_BOOST_30D = 0.15  # 7-30 days — moderate boost

# Domain keyword map for automatic atom classification (no LLM call needed)
DOMAIN_KEYWORDS: dict[str, set[str]] = {
    "dev": {"code", "typescript", "python", "docker", "npm", "git", "test", "bug",
            "refactor", "api", "deploy", "container", "ci", "build", "lint",
            "commit", "branch", "merge", "webpack", "eslint", "jest", "node"},
    "study": {"exam", "study", "textbook", "lecture", "theorem", "proof", "notes",
              "flashcard", "syllabus", "homework", "quiz", "course", "calculus",
              "algebra", "physics", "mechanics", "integral", "derivative"},
    "trading": {"trade", "stock", "option", "portfolio", "ticker", "market",
                "position", "strike", "etf", "ibkr", "tradingview", "chart",
                "candle", "indicator", "earnings", "dividend"},
    "personal": {"family", "friend", "meal", "exercise", "sleep", "mood",
                 "appointment", "birthday", "hobby", "drum", "music", "roommate"},
}

# Category-based initial confidence priors (replaces hardcoded 0.50)
CONFIDENCE_PRIOR: dict[str, float] = {
    "fact": 0.70,
    "decision": 0.70,
    "constraint": 0.65,
    "preference": 0.55,
    "belief": 0.40,
}

_client: genai.Client | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def load_api_key() -> str:
    try:
        return _load_api_key()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


def embed(text: str) -> list[float]:
    from evolution.providers.embeddings import embed as _provider_embed
    return _provider_embed(text)


def serialize(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def deserialize(buf: bytes) -> list[float]:
    n = len(buf) // 4
    return list(struct.unpack(f"{n}f", buf))


# ── DB ────────────────────────────────────────────────────────────────────────

def _backfill_fts(db: sqlite3.Connection) -> None:
    """Insert any entries rows not yet in the standalone FTS5 index.

    Compares counts; if FTS5 is behind, inserts missing rows by rowid.
    Idempotent and fast at Deus vault scale (~hundreds of rows).
    Silently skips if FTS5 is unavailable.
    """
    try:
        fts_count = db.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0]
        entries_count = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        if fts_count < entries_count:
            # Insert all entries rows that don't yet have an FTS5 counterpart
            db.execute("""
                INSERT INTO entries_fts(rowid, chunk)
                SELECT e.id, e.chunk FROM entries e
                WHERE e.id NOT IN (SELECT rowid FROM entries_fts)
            """)
            db.commit()
    except sqlite3.OperationalError:
        pass


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    db.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            path     TEXT NOT NULL,
            date     TEXT,
            chunk    TEXT NOT NULL,
            type     TEXT NOT NULL,
            tldr     TEXT,
            topics   TEXT,
            decisions TEXT
        )
    """)
    db.execute(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS embeddings
        USING vec0(embedding float[{EMBED_DIM}])
    """)
    # Backward-compatible: add atom columns if upgrading an existing DB
    for col, definition in [
        ("confidence", "REAL DEFAULT 0.0"),
        ("corroborations", "INTEGER DEFAULT 0"),
        ("source_chunk", "TEXT"),
        ("expired_at", "TEXT DEFAULT NULL"),
        ("expired_reason", "TEXT DEFAULT NULL"),
        ("domain", "TEXT DEFAULT 'general'"),
    ]:
        try:
            db.execute(f"ALTER TABLE entries ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass
    # FTS5 full-text index for hybrid BM25+vector search (standalone, not a content table)
    try:
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS entries_fts
            USING fts5(chunk, tokenize='porter unicode61')
        """)
    except sqlite3.OperationalError:
        pass  # FTS5 unavailable on this SQLite build — hybrid search degrades to ANN-only
    db.commit()
    _backfill_fts(db)
    return db


def entry_exists(db: sqlite3.Connection, path: str) -> bool:
    row = db.execute("SELECT 1 FROM entries WHERE path = ? LIMIT 1", [path]).fetchone()
    return row is not None


def delete_entries(db: sqlite3.Connection, path: str):
    ids = [r[0] for r in db.execute("SELECT id FROM entries WHERE path = ?", [path]).fetchall()]
    for eid in ids:
        db.execute("DELETE FROM embeddings WHERE rowid = ?", [eid])
        try:
            db.execute("DELETE FROM entries_fts WHERE rowid = ?", [eid])
        except sqlite3.OperationalError:
            pass
    db.execute("DELETE FROM entries WHERE path = ?", [path])
    db.commit()


# ── Parsing ───────────────────────────────────────────────────────────────────

def extract_frontmatter(content: str) -> dict:
    """Extract key fields from YAML frontmatter."""
    m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return {}
    fm_text = m.group(1)
    result = {"raw": m.group(0)}

    # date
    dm = re.search(r"^date:\s*(.+)$", fm_text, re.MULTILINE)
    result["date"] = dm.group(1).strip() if dm else ""

    # tldr (block scalar or inline)
    tldr_m = re.search(r"^tldr:\s*\|?\n?(.*?)(?=\n\w|\Z)", fm_text, re.DOTALL | re.MULTILINE)
    if tldr_m:
        result["tldr"] = re.sub(r"\n\s+", " ", tldr_m.group(1)).strip()

    # topics
    topics_m = re.search(r"^topics:\s*\[(.+?)\]", fm_text, re.MULTILINE)
    if topics_m:
        result["topics"] = topics_m.group(1).strip()

    # decisions (YAML list)
    decisions_block = re.search(r"^decisions:\n((?:\s+-.*\n?)+)", fm_text, re.MULTILINE)
    if decisions_block:
        items = re.findall(r'^\s+-\s+"?(.+?)"?\s*$', decisions_block.group(1), re.MULTILINE)
        result["decisions"] = "; ".join(items)

    return result


def extract_decisions_section(content: str) -> str:
    """Extract ## Decisions Made body (stop at next ##)."""
    m = re.search(r"## Decisions Made\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
    if not m:
        return ""
    return m.group(1).strip()


_TURN_RE = re.compile(r"(?:^|\n)\*\*(user|assistant)\*\*:\s*", re.IGNORECASE)
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def resolve_wikilinks(text: str) -> str:
    """Replace [[page]] → page and [[page|display]] → display text."""
    return _WIKILINK_RE.sub(lambda m: (m.group(2) or m.group(1)).strip(), text)

_TARGET_CHUNK_TOKENS = int(os.environ.get("DEUS_TURN_CHUNK_TOKENS", "400"))
_MIN_CHUNK_TOKENS = 80


def _split_turns(body: str) -> list[str]:
    """Split session body on **user**:/**assistant**: markers.

    Returns [] if no markers found — plain-prose sessions are untouched.
    """
    parts = _TURN_RE.split(body)
    if len(parts) <= 1:
        return []
    # parts: [pre-text, role1, content1, role2, content2, ...]
    turns = []
    for i in range(1, len(parts), 2):
        role = parts[i]
        content = parts[i + 1] if i + 1 < len(parts) else ""
        turns.append(f"**{role}**: {content.strip()}")
    return turns


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: words × 1.3 (English prose heuristic)."""
    return int(len(text.split()) * 1.3)


def _make_turn_windows(turns: list[str], target: int = _TARGET_CHUNK_TOKENS) -> list[str]:
    """Greedy grouping of turns into ~target-token windows.

    Windows below _MIN_CHUNK_TOKENS are discarded (e.g. single short greetings).
    """
    windows: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for turn in turns:
        t = _estimate_tokens(turn)
        if current and current_tokens + t > target:
            text = "\n\n".join(current)
            if _estimate_tokens(text) >= _MIN_CHUNK_TOKENS:
                windows.append(text)
            current, current_tokens = [], 0
        current.append(turn)
        current_tokens += t
    if current:
        text = "\n\n".join(current)
        if _estimate_tokens(text) >= _MIN_CHUNK_TOKENS:
            windows.append(text)
    return windows


def chunks_for_log(path: Path, content: str) -> list[dict]:
    """Return chunks to index for a single session log."""
    fm = extract_frontmatter(content)
    if not fm:
        return []

    chunks = []

    # Chunk 1: frontmatter (dense signal)
    fm_text = fm["raw"]
    if len(fm_text) > 50:
        chunks.append({
            "chunk": fm_text,
            "type": "frontmatter",
            "date": fm.get("date", ""),
            "tldr": fm.get("tldr", ""),
            "topics": fm.get("topics", ""),
            "decisions": fm.get("decisions", ""),
        })

    # Chunk 2: decisions section body (if present and non-trivial)
    dec_body = resolve_wikilinks(extract_decisions_section(content))
    if len(dec_body) > 30:
        chunks.append({
            "chunk": f"Decisions from {path.stem}:\n{dec_body}",
            "type": "decisions",
            "date": fm.get("date", ""),
            "tldr": fm.get("tldr", ""),
            "topics": fm.get("topics", ""),
            "decisions": fm.get("decisions", ""),
        })

    # Chunk group 3: turn-level windows (only for conversation-style sessions)
    # Strip frontmatter before scanning for turn markers
    fm_raw = fm.get("raw", "")
    body = content[len(fm_raw):].strip() if fm_raw else content
    body = resolve_wikilinks(body)
    turns = _split_turns(body)
    if turns:
        for window in _make_turn_windows(turns):
            chunks.append({
                "chunk": window,
                "type": "turn",
                "date": fm.get("date", ""),
                "tldr": fm.get("tldr", ""),
                "topics": fm.get("topics", ""),
                "decisions": fm.get("decisions", ""),
            })

    return chunks


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_add(path_str: str, extract: bool = True):
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    db = open_db()
    # Remove stale entries for this path (re-indexing)
    delete_entries(db, str(path))

    content = path.read_text(encoding="utf-8")
    chunks = chunks_for_log(path, content)
    if not chunks:
        print(f"No indexable content in {path.name}")
        return

    indexed = 0
    for chunk in chunks:
        vec = embed(chunk["chunk"])
        cur = db.execute(
            "INSERT INTO entries (path, date, chunk, type, tldr, topics, decisions) VALUES (?,?,?,?,?,?,?)",
            [str(path), chunk["date"], chunk["chunk"], chunk["type"],
             chunk["tldr"], chunk["topics"], chunk["decisions"]],
        )
        rowid = cur.lastrowid
        db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
                   [rowid, serialize(vec)])
        try:
            db.execute("INSERT INTO entries_fts(rowid, chunk) VALUES (?, ?)",
                       [rowid, chunk["chunk"]])
        except sqlite3.OperationalError:
            pass
        indexed += 1

    db.commit()
    print(f"Indexed {indexed} chunk(s) from {path.name}")

    if extract:
        try:
            cmd_extract(str(path))
        except SystemExit:
            pass  # cmd_extract uses sys.exit(1) for missing files — already resolved above
        except Exception as exc:
            print(f"  WARN: atom extraction failed for {path.name}: {exc}", file=sys.stderr)


COMPACT_SESSION_THRESHOLD = 12  # auto-enable compact mode above this many sessions


def cmd_recent(n: int = 3, days: bool = False, compact: bool = False):
    """Return recent session frontmatters. Pure filesystem — no API calls.

    When days=False (legacy): return last N sessions, sorted by date then mtime.
    When days=True: return ALL sessions from the last N calendar days, sorted by
    date descending then mtime descending (newest first within each day).

    Compact mode (auto-triggered at >= COMPACT_SESSION_THRESHOLD sessions, or via
    --compact flag): truncates decisions to 60 chars, strips full vault paths,
    collapses cluster children to a count-only header.
    """
    if not VAULT_SESSION_LOGS.exists():
        print(f"ERROR: session logs not found at {VAULT_SESSION_LOGS}", file=sys.stderr)
        sys.exit(1)

    log_files = [f for f in VAULT_SESSION_LOGS.rglob("*.md") if ".obsidian" not in str(f)]

    # Parse date: prefer parent folder name (YYYY-MM-DD), fallback to frontmatter
    def get_date(p: Path) -> str:
        folder = p.parent.name
        if re.match(r"^\d{4}-\d{2}-\d{2}$", folder):
            return folder
        fm = extract_frontmatter(p.read_text(encoding="utf-8"))
        return fm.get("date", "0000-00-00")

    dated = [(get_date(f), f) for f in log_files]
    # Sort by date descending, then mtime descending (newest file first within same day)
    dated.sort(key=lambda x: (x[0], x[1].stat().st_mtime), reverse=True)

    if days:
        # Collect all unique dates, take the first N, return all sessions from those days
        seen_dates: list[str] = []
        for date, _ in dated:
            if date not in seen_dates:
                seen_dates.append(date)
            if len(seen_dates) > n:
                break
        target_dates = set(seen_dates[:n])
        selected = [(d, p) for d, p in dated if d in target_dates]
    else:
        selected = dated[:n]

    # Auto-enable compact when session count exceeds threshold
    if not compact and len(selected) >= COMPACT_SESSION_THRESHOLD:
        compact = True

    # Compact-aware formatting helpers
    def fmt_decisions(decisions: str) -> str:
        if not compact or not decisions:
            return f" | {decisions}" if decisions else ""
        # 80-char limit: median real decision is ~71 chars, preserves ~95% of content
        truncated = decisions[:80] + "…" if len(decisions) > 80 else decisions
        return f" | {truncated}"

    def fmt_path(path: Path) -> str:
        if compact:
            return f"  (log: {path.stem})"
        return f"  (full log: {path})"

    # Group sessions by date for clustering on busy days
    from collections import OrderedDict
    by_date: OrderedDict[str, list[tuple[str, Path, dict]]] = OrderedDict()
    for date, path in selected:
        content = path.read_text(encoding="utf-8")
        fm = extract_frontmatter(content)
        by_date.setdefault(date, []).append((date, path, fm))

    lines = ["## Recent Sessions"]

    for date, sessions in by_date.items():
        if len(sessions) >= 4:
            # Cluster by topics when 4+ sessions on the same day
            clusters: dict[str, list[tuple[Path, dict]]] = {}
            for _, path, fm in sessions:
                topics = fm.get("topics", "") or ""
                # Use first topic as cluster key, fallback to "General"
                first_topic = topics.split(",")[0].strip() if topics else "General"
                first_topic = first_topic.strip("[] ")
                if not first_topic:
                    first_topic = "General"
                clusters.setdefault(first_topic, []).append((path, fm))

            for topic, items in clusters.items():
                if len(items) >= 2:
                    if compact:
                        # Compact: header only — no individual entries
                        tldrs = "; ".join(
                            t for t in (
                                (fm.get("tldr", "") or "").split(".")[0][:40]
                                for _, fm in items[:3]
                            ) if t
                        )
                        covering = f", covering: {tldrs}" if tldrs else ""
                        lines.append(f"- [{date} | {topic}] ({len(items)} sessions{covering})")
                    else:
                        # Full: group header + indented items
                        lines.append(f"- [{date} | {topic}] ({len(items)} sessions)")
                        for path, fm in items:
                            name = path.stem.replace("-", " ")
                            tldr = (fm.get("tldr", "") or "").split(".")[0][:80]
                            lines.append(f"  - {name} — {tldr}")
                            lines.append(f"    (full log: {path})")
                else:
                    # Single item — flat format
                    path, fm = items[0]
                    name = path.stem.replace("-", " ")
                    tldr = (fm.get("tldr", "") or "").split(".")[0][:80]
                    decisions = fm.get("decisions", "") or ""
                    dec_part = fmt_decisions(decisions)
                    lines.append(f"- [{date} | {name}]{dec_part} — {tldr}")
                    lines.append(fmt_path(path))
        else:
            for _, path, fm in sessions:
                name = path.stem.replace("-", " ")
                tldr = (fm.get("tldr", "") or "").split(".")[0][:80]
                decisions = fm.get("decisions", "") or ""
                dec_part = fmt_decisions(decisions)
                lines.append(f"- [{date} | {name}]{dec_part} — {tldr}")
                lines.append(fmt_path(path))

    # Continuity indicator
    total_sessions = len(list(VAULT_SESSION_LOGS.rglob("*.md"))) if VAULT_SESSION_LOGS.exists() else 0
    total_atoms = len(list(VAULT_ATOMS.glob("*.md"))) if VAULT_ATOMS.exists() else 0
    total_days = len(by_date)
    parts = [f"{len(selected)} sessions across {total_days} day{'s' if total_days != 1 else ''}"]
    if total_atoms > 0:
        parts.append(f"{total_atoms} atoms")
    # Check for reflections in evolution DB (optional — evolution may not be set up)
    try:
        if EVOLUTION_DB_PATH.exists():
            _db = sqlite3.connect(EVOLUTION_DB_PATH)
            reflection_count = _db.execute(
                "SELECT COUNT(*) FROM reflections WHERE archived_at IS NULL"
            ).fetchone()[0]
            _db.close()
            if reflection_count > 0:
                parts.append(f"{reflection_count} active reflections")
    except (sqlite3.OperationalError, Exception):
        pass
    compact_suffix = " (compact)" if compact else ""
    lines.append(f"\nContinuity: {' · '.join(parts)} (total: {total_sessions} sessions, {total_atoms} atoms){compact_suffix}")

    print("\n".join(lines))


def cmd_learnings(since_days: int = 7, max_items: int = 3):
    """Surface recently strengthened or new high-confidence atoms since last /resume.

    Delta tracking: compares against ~/.deus/last_resume_learnings.txt to avoid
    showing the same learnings twice. Outputs nothing if no new learnings exist.
    """
    atom_count = len(list(VAULT_ATOMS.glob("*.md"))) if VAULT_ATOMS.exists() else 0
    if atom_count == 0:
        print("## What's Emerging\n- Your learnings will appear here as you use Deus. "
              "Each session extracts atomic facts that grow stronger through corroboration.")
        return

    from datetime import date as _date, timedelta
    today = _date.today()
    cutoff = today - timedelta(days=since_days)

    # Load previously shown learnings for delta tracking
    previously_shown: set[str] = set()
    if LAST_RESUME_LEARNINGS.exists():
        previously_shown = set(LAST_RESUME_LEARNINGS.read_text().strip().splitlines())

    # Scan all atoms
    candidates: list[dict] = []
    for atom_path in VAULT_ATOMS.glob("*.md"):
        content = atom_path.read_text(encoding="utf-8")
        fm = extract_frontmatter(content)
        if not fm:
            continue

        created_at = fm.get("date") or fm.get("raw", "")
        updated_at = ""
        corroborations = 1
        confidence = 0.5
        category = "fact"
        expired = False

        # Parse frontmatter fields from raw block
        raw = fm.get("raw", "")
        for line in raw.splitlines():
            if line.startswith("created_at:"):
                created_at = line.split(":", 1)[1].strip()
            elif line.startswith("updated_at:"):
                updated_at = line.split(":", 1)[1].strip()
            elif line.startswith("corroborations:"):
                try:
                    corroborations = int(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("confidence:"):
                try:
                    confidence = float(line.split(":", 1)[1].strip())
                except ValueError:
                    pass
            elif line.startswith("category:"):
                category = line.split(":", 1)[1].strip()
            elif line.startswith("ttl_days:"):
                ttl_str = line.split(":", 1)[1].strip()
                if ttl_str not in ("null", ""):
                    try:
                        ttl = int(ttl_str)
                        if created_at and (today - _date.fromisoformat(created_at)).days > ttl:
                            expired = True
                    except (ValueError, TypeError):
                        pass

        if expired:
            continue

        if not updated_at:
            updated_at = created_at

        try:
            update_date = _date.fromisoformat(updated_at)
        except (ValueError, TypeError):
            continue

        if update_date < cutoff:
            continue

        # Extract body text (after second ---)
        body = ""
        parts = content.split("---", 2)
        if len(parts) >= 3:
            body = parts[2].strip()
        if not body:
            continue

        # Skip if already shown in a previous /resume
        if atom_path.name in previously_shown:
            continue

        # Classify: strengthened (updated > created, 2+ corroborations) vs new insight
        is_strengthened = (updated_at != created_at) and corroborations >= 2

        candidates.append({
            "path": atom_path,
            "name": atom_path.name,
            "body": body,
            "category": category,
            "corroborations": corroborations,
            "confidence": confidence,
            "is_strengthened": is_strengthened,
            "updated_at": updated_at,
        })

    if not candidates:
        return

    # Sort: strengthened patterns first, then by confidence desc, then recency
    candidates.sort(key=lambda x: (x["is_strengthened"], x["confidence"], x["updated_at"]), reverse=True)
    selected = candidates[:max_items]

    lines = ["## What's Emerging"]
    for item in selected:
        prefix = "Pattern confirmed" if item["is_strengthened"] else "New insight"
        suffix = f" (seen across {item['corroborations']} sessions)" if item["corroborations"] >= 2 else ""
        lines.append(f"- {prefix}: {item['body']}{suffix}")

    print("\n".join(lines))

    # Update delta tracking file
    LAST_RESUME_LEARNINGS.parent.mkdir(parents=True, exist_ok=True)
    shown_names = previously_shown | {item["name"] for item in selected}
    LAST_RESUME_LEARNINGS.write_text("\n".join(sorted(shown_names)) + "\n")


def _fts_escape(query: str) -> str:
    """Strip FTS5 special syntax to prevent parse errors on arbitrary user queries."""
    cleaned = re.sub(r'["()*\-]', " ", query)
    cleaned = re.sub(r"\b(AND|OR|NOT|NEAR)\b", " ", cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split())


def _fts_query(db: sqlite3.Connection, query: str, k: int) -> list[tuple[str, int]]:
    """FTS5 BM25-ranked search. Returns [(path, 1-based rank)], deduped by path.

    Returns [] if FTS5 is unavailable or the query matches nothing.
    FTS5 rank() is negative — lower (more negative) = better match.
    """
    escaped = _fts_escape(query)
    if not escaped.strip():
        return []
    try:
        rows = db.execute(
            """
            SELECT e.path, entries_fts.rank
            FROM entries_fts
            JOIN entries e ON e.id = entries_fts.rowid
            WHERE entries_fts MATCH ?
              AND e.type != 'atom'
            ORDER BY entries_fts.rank
            LIMIT ?
            """,
            [escaped, k * 3],
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    # Dedup by path, keep first (best) FTS5 rank per path
    seen: dict[str, int] = {}
    result: list[tuple[str, int]] = []
    for path, _rank in rows:
        if path not in seen:
            seen[path] = len(result) + 1
            result.append((path, seen[path]))
    return result[:k]


def _rrf_fuse(
    ann_ranked: list[tuple[str, int]],
    fts_ranked: list[tuple[str, int]],
    k_rrf: int = 60,
    top: int = 10,
) -> list[str]:
    """Reciprocal Rank Fusion. Returns paths sorted by fused score (descending).

    score(path) = Σ 1 / (k_rrf + rank_i) across all result lists.
    Paths appearing in both lists score higher than paths in only one.
    """
    scores: dict[str, float] = {}
    for path, rank in ann_ranked:
        scores[path] = scores.get(path, 0.0) + 1.0 / (k_rrf + rank)
    for path, rank in fts_ranked:
        scores[path] = scores.get(path, 0.0) + 1.0 / (k_rrf + rank)
    return [p for p, _ in sorted(scores.items(), key=lambda x: -x[1])[:top]]


def cmd_query(query: str, top: int = 3, recency_boost: bool = False,
              show_source: bool = False, domain: str | None = None):
    db = open_db()

    # Check if anything is indexed
    count = db.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    if count == 0:
        print("(index empty — run --rebuild first)", file=sys.stderr)
        sys.exit(1)

    has_atoms = db.execute("SELECT COUNT(*) FROM entries WHERE type = 'atom'").fetchone()[0] > 0

    q_vec = embed(query)
    rows = db.execute(
        """
        SELECT e.path, e.date, e.tldr, e.topics, e.decisions, e.type,
               e.confidence, e.corroborations, v.distance, e.source_chunk
        FROM embeddings v
        JOIN entries e ON e.id = v.rowid
        WHERE v.embedding MATCH ?
          AND k = ?
          AND (e.expired_at IS NULL OR e.expired_at > date('now'))
        ORDER BY v.distance
        """,
        [serialize(q_vec), max(top * 6, 30)],  # 6x headroom: turn chunks multiply rows per session
    ).fetchall()

    # Partition into atoms and sessions; deduplicate sessions by path
    atom_results: list[dict] = []
    seen: dict[str, dict] = {}

    for path, date, tldr, topics, decisions, chunk_type, confidence, corroborations, dist, source_chunk in rows:
        if chunk_type == "atom":
            # Apply domain filter if specified
            if domain:
                entry_domain = db.execute(
                    "SELECT domain FROM entries WHERE path = ? LIMIT 1", [path]
                ).fetchone()
                if entry_domain and entry_domain[0] != domain:
                    continue
            if has_atoms and len(atom_results) < top:
                atom_results.append({
                    "path": path, "chunk": tldr, "confidence": confidence or 0.0,
                    "corroborations": corroborations or 1,
                    "source_chunk": source_chunk or "",
                    "dist": dist,
                })
        else:
            if path not in seen or (chunk_type == "frontmatter" and seen[path]["type"] != "frontmatter"):
                seen[path] = {
                    "path": path, "date": date, "tldr": tldr,
                    "topics": topics, "decisions": decisions,
                    "type": chunk_type, "dist": dist,
                }

    # ── Hybrid RRF fusion ─────────────────────────────────────────────────────
    # Build ANN ranked list (paths in order of first appearance in seen)
    ann_ranked = [(path, i + 1) for i, path in enumerate(seen.keys())]
    fts_ranked = _fts_query(db, query, k=top * 2)

    if fts_ranked:
        fused_paths = _rrf_fuse(ann_ranked, fts_ranked, top=top * 2)
        # Rebuild seen in fused order; fetch metadata for FTS-only hits
        fused_seen: dict[str, dict] = {}
        for path in fused_paths:
            if path in seen:
                fused_seen[path] = seen[path]
            else:
                row = db.execute(
                    "SELECT path, date, tldr, topics, decisions, type FROM entries "
                    "WHERE path = ? LIMIT 1", [path]
                ).fetchone()
                if row:
                    fused_seen[path] = {
                        "path": row[0], "date": row[1], "tldr": row[2],
                        "topics": row[3], "decisions": row[4],
                        "type": row[5], "dist": 999.0,
                    }
        seen = fused_seen

    # Re-rank sessions by recency-adjusted distance
    if recency_boost and seen:
        from datetime import date as _date
        today = _date.today()
        for entry in seen.values():
            try:
                entry_date = _date.fromisoformat(entry["date"])
                age_days = (today - entry_date).days
                if age_days <= 7:
                    entry["dist"] -= RECENCY_BOOST_7D
                elif age_days <= 30:
                    entry["dist"] -= RECENCY_BOOST_30D
            except (ValueError, TypeError):
                pass
        # Re-sort by adjusted distance and keep top N
        ranked = sorted(seen.values(), key=lambda e: e["dist"])[:top]
        seen = {e["path"]: e for e in ranked}

    if not seen and not atom_results:
        sys.exit(1)  # trigger fallback in /resume

    lines = []

    # Confidence-weighted re-ranking: lower score = better (L2 distance - confidence bonus)
    for a in atom_results:
        a["score"] = a.get("dist", 999.0) - 0.25 * a["confidence"]
    atom_results.sort(key=lambda a: a["score"])

    # Known Facts — only atoms with ≥ 2 corroborations (well-established)
    high_conf = [a for a in atom_results if a["corroborations"] >= 2]
    if high_conf:
        lines.append("## Known Facts")
        for a in high_conf:
            cat = "fact"
            atom_p = Path(a["path"])
            if atom_p.exists():
                m = re.search(r"^category:\s*(\S+)", atom_p.read_text(), re.MULTILINE)
                if m:
                    cat = m.group(1)
            text = a["chunk"] or ""
            lines.append(f"- [{cat} | {a['confidence']:.2f}] {text} ({a['corroborations']}x)")
        if show_source and a.get("source_chunk"):
            excerpt = a["source_chunk"][:400].replace("\n", "\n    ")
            lines.append(f"  Source context:\n    {excerpt}")
        lines.append("")

    if seen:
        lines.append("## Relevant Past Sessions")
        for entry in list(seen.values())[:top]:
            date = entry["date"] or "unknown date"
            name = Path(entry["path"]).stem.replace("-", " ")
            tldr = (entry["tldr"] or "").split(".")[0][:80]
            decisions = entry["decisions"] or ""
            dec_part = f" | {decisions}" if decisions else ""
            lines.append(f"- [{date} | {name}]{dec_part} — {tldr}")
            lines.append(f"  (full log: {entry['path']})")

    print("\n".join(lines))


def cmd_wander(seeds: list[str], steps: int = 3, top_k: int = 10):
    """
    Spreading activation over session log topics (no embeddings needed).
    Builds a topic co-occurrence graph from session logs, then spreads activation
    from seed topics to discover cross-domain connections.
    """
    from collections import defaultdict

    if not VAULT_SESSION_LOGS.exists():
        print(f"ERROR: session logs not found at {VAULT_SESSION_LOGS}", file=sys.stderr)
        sys.exit(1)

    today = datetime.now().date()

    # Build weighted co-occurrence graph: edge_weight[t1][t2] += recency_weight
    edge_weight: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    topic_sessions: dict[str, list[str]] = defaultdict(list)

    log_files = sorted(VAULT_SESSION_LOGS.rglob("*.md"))
    log_files = [f for f in log_files if ".obsidian" not in str(f)]

    for log_file in log_files:
        fm = extract_frontmatter(log_file.read_text(encoding="utf-8"))
        if not fm.get("topics"):
            continue

        topics = [t.strip() for t in fm["topics"].split(",")]

        try:
            from datetime import date as _date
            session_date = _date.fromisoformat(fm.get("date", ""))
            age_days = (today - session_date).days
            recency = 2.0 if age_days <= 30 else 1.5 if age_days <= 90 else 1.0
        except (ValueError, TypeError):
            recency = 1.0

        for t in topics:
            topic_sessions[t].append(log_file.stem)

        for i, t1 in enumerate(topics):
            for t2 in topics[i + 1:]:
                edge_weight[t1][t2] += recency
                edge_weight[t2][t1] += recency

    if not edge_weight:
        print("No topics found in session logs.")
        return

    all_topics = set(edge_weight.keys())

    # Resolve seeds: exact then partial match
    resolved: list[str] = []
    for seed in seeds:
        s = seed.lower()
        if s in all_topics:
            resolved.append(s)
        else:
            resolved.extend(t for t in all_topics if s in t or t in s)

    # If no seeds provided or nothing matched, use topics from the 3 most recent sessions
    if not resolved:
        for log_file in sorted(log_files, reverse=True)[:3]:
            fm = extract_frontmatter(log_file.read_text(encoding="utf-8"))
            if fm.get("topics"):
                resolved.extend(t.strip() for t in fm["topics"].split(","))
        resolved = list(dict.fromkeys(resolved))[:5]

    if not resolved:
        print("Could not find any seed topics.")
        return

    # Spreading activation (decay=0.7, lateral inhibition to top_k per step)
    DECAY = 0.7
    activation: dict[str, float] = {t: 1.0 for t in resolved}
    all_visited: set[str] = set(resolved)

    for _ in range(steps):
        spread: dict[str, float] = {}
        for topic, strength in activation.items():
            neighbors = edge_weight.get(topic, {})
            total = sum(neighbors.values()) or 1.0
            for neighbor, weight in neighbors.items():
                if neighbor not in all_visited:
                    spread[neighbor] = spread.get(neighbor, 0.0) + strength * DECAY * (weight / total)
        if not spread:
            break
        top = sorted(spread.items(), key=lambda x: -x[1])[:top_k]
        activation = dict(top)
        all_visited.update(activation)

    print(f"## Wander: seeds = [{', '.join(resolved)}]\n")

    if activation:
        print(f"### Connected Topics\n")
        for topic, strength in sorted(activation.items(), key=lambda x: -x[1])[:top_k]:
            sessions = topic_sessions.get(topic, [])
            hint = f" ← {sessions[-1]}" if sessions else ""
            print(f"- **{topic}** ({strength:.2f}){hint}")

    # Collision candidates: activated topic pairs that don't directly co-occur
    # but share common neighbors (unexpected cross-domain bridges)
    activated_list = [t for t, _ in sorted(activation.items(), key=lambda x: -x[1])[:15]]
    collisions = []
    for i, t1 in enumerate(activated_list):
        for t2 in activated_list[i + 1:]:
            if t2 not in edge_weight.get(t1, {}):
                shared = set(edge_weight.get(t1, {}).keys()) & set(edge_weight.get(t2, {}).keys())
                if shared:
                    collisions.append((t1, t2, sorted(shared)[:3]))

    print(f"\n### Collision Candidates (indirect bridges)\n")
    if collisions:
        for t1, t2, via in collisions[:5]:
            print(f"- **{t1}** ↔ **{t2}**  (via: {', '.join(via)})")
    else:
        print("(none)")


def slugify(text: str) -> str:
    words = re.sub(r"[^a-z0-9\s]", "", text.lower()).split()
    return "-".join(words[:5])


def _extract_content_for_llm(content: str, max_chars: int = 6000) -> str:
    """Return a token-efficient slice of a session log for the extraction LLM."""
    if len(content) <= max_chars:
        return content
    # Keep frontmatter + Decisions Made + Key Learnings only
    parts = []
    fm_m = re.match(r"^---\n.*?\n---", content, re.DOTALL)
    if fm_m:
        parts.append(fm_m.group(0))
    for section in ("## Decisions Made", "## Key Learnings"):
        sec_m = re.search(rf"{re.escape(section)}\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
        if sec_m:
            parts.append(f"{section}\n{sec_m.group(1).strip()}")
    trimmed = "\n\n".join(parts)
    return trimmed[:max_chars]


def extract_atoms(content: str) -> list[dict]:
    """Call Gemini Flash to extract 2-5 atomic facts from a session log."""
    prompt = (
        "You are an atomic fact extractor for a personal knowledge system.\n\n"
        "Given a session log, extract 2-5 atomic facts the user would want a future AI assistant to remember. "
        "Each fact must be:\n"
        "- A single sentence, timeless (not \"today we did X\" but \"prefers X over Y\")\n"
        "- About the USER's preferences, identity, or stable decisions — not about what happened\n"
        "- Actionable across future sessions\n\n"
        "Categories:\n"
        "- preference: user likes/dislikes, style choices (ttl: 365 days)\n"
        "- constraint: hard rules, always/never requirements (ttl: 365 days)\n"
        "- belief: opinions, worldview, tentative inferences (ttl: 90 days)\n"
        "- fact: identity info, context, environment details (no ttl)\n"
        "- decision: architectural or tool choices that affect future work (no ttl)\n\n"
        "Respond with ONLY a JSON array, no markdown fencing:\n"
        '[{"text": "...", "category": "preference|constraint|belief|fact|decision"}]\n\n'
        "If nothing is worth extracting (casual/social session with no stable decisions), respond with: []\n\n"
        f"SESSION LOG:\n{_extract_content_for_llm(content)}"
    )
    for model in GEN_MODELS:
        try:
            response = _client.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(temperature=0.1, max_output_tokens=1024),
            )
            raw = response.text.strip()
            # Strip markdown fencing (opening and closing) if model adds it
            if raw.startswith("```"):
                raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
            atoms = json.loads(raw)
            return [a for a in atoms if isinstance(a, dict) and "text" in a and "category" in a]
        except json.JSONDecodeError:
            return []
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                print(f"  quota exhausted on {model}, trying fallback...", file=sys.stderr)
                continue  # try next model
            # Non-quota error (bad key, network, etc.) — no point retrying
            print(f"  WARN: extraction error ({model}): {e}", file=sys.stderr)
            return []
    print("  WARN: all generation models quota-exhausted, skipping extraction.", file=sys.stderr)
    return []


def find_duplicate_atom(db: sqlite3.Connection, vec: list[float]) -> int | None:
    """Return the entry id of an existing atom within DEDUP_L2_THRESHOLD, or None."""
    atom_count = db.execute("SELECT COUNT(*) FROM entries WHERE type = 'atom'").fetchone()[0]
    if atom_count == 0:
        return None  # short-circuit: nothing to compare against
    row = db.execute(
        """
        SELECT e.id, v.distance
        FROM embeddings v
        JOIN entries e ON e.id = v.rowid
        WHERE e.type = 'atom'
          AND (e.expired_at IS NULL OR e.expired_at > date('now'))
          AND v.embedding MATCH ?
          AND k = 1
        ORDER BY v.distance
        """,
        [serialize(vec)],
    ).fetchone()
    if row and row[1] <= DEDUP_L2_THRESHOLD:
        return row[0]
    return None


def bump_corroboration(db: sqlite3.Connection, entry_id: int):
    """Increment corroborations, recompute confidence, and update the atom .md file."""
    row = db.execute("SELECT path, corroborations FROM entries WHERE id = ?", [entry_id]).fetchone()
    if not row:
        return
    new_corr = row[1] + 1
    # Read category from atom file for category-aware confidence prior
    cat = "fact"
    atom_path = Path(row[0])
    if atom_path.exists():
        text = atom_path.read_text()
        m = re.search(r"^category:\s*(\S+)", text, re.MULTILINE)
        if m:
            cat = m.group(1)
    prior = CONFIDENCE_PRIOR.get(cat, 0.50)
    new_conf = min(prior + new_corr * 0.1, 0.95)
    today = datetime.now().strftime("%Y-%m-%d")
    db.execute(
        "UPDATE entries SET corroborations = ?, confidence = ? WHERE id = ?",
        [new_corr, new_conf, entry_id],
    )
    if atom_path.exists():
        text = atom_path.read_text()
        text = re.sub(r"^confidence:.*$", f"confidence: {new_conf:.2f}", text, flags=re.MULTILINE)
        text = re.sub(r"^corroborations:.*$", f"corroborations: {new_corr}", text, flags=re.MULTILINE)
        text = re.sub(r"^updated_at:.*$", f"updated_at: {today}", text, flags=re.MULTILINE)
        atom_path.write_text(text)


def invalidate_atom(db: sqlite3.Connection, entry_id: int, reason: str):
    """Soft-delete an atom by setting expired_at. Atom file is updated with expired frontmatter."""
    row = db.execute("SELECT path FROM entries WHERE id = ?", [entry_id]).fetchone()
    if not row:
        return
    today = datetime.now().strftime("%Y-%m-%d")
    db.execute(
        "UPDATE entries SET expired_at = ?, expired_reason = ? WHERE id = ?",
        [today, reason, entry_id],
    )
    db.commit()
    atom_path = Path(row[0])
    if atom_path.exists():
        text = atom_path.read_text()
        if re.search(r"^expired_at:", text, re.MULTILINE):
            text = re.sub(r"^expired_at:.*$", f"expired_at: {today}", text, flags=re.MULTILINE)
        else:
            text = text.replace("\n---\n", f"\nexpired_at: {today}\nexpired_reason: {reason}\n---\n", 1)
        atom_path.write_text(text)
    print(f"  invalidated: {atom_path.name} ({reason})")


def classify_domain(text: str) -> str:
    """Classify atom text into a domain using keyword matching. No LLM call."""
    words = set(re.findall(r'\w+', text.lower()))
    best_domain = "general"
    best_count = 0
    for domain, keywords in DOMAIN_KEYWORDS.items():
        count = len(words & keywords)
        if count > best_count:
            best_count = count
            best_domain = domain
    return best_domain


def write_atom_file(atom: dict, source_path: str, today: str,
                    source_excerpt: str = "", domain: str = "general") -> Path:
    """Write an atom to the vault Atoms/ directory and return its path."""
    VAULT_ATOMS.mkdir(parents=True, exist_ok=True)
    cat = atom["category"]
    conf = CONFIDENCE_PRIOR.get(cat, 0.50)
    ttl_map = {"fact": None, "decision": None, "preference": 365, "constraint": 365, "belief": 90}
    ttl = ttl_map.get(cat, 365)
    ttl_line = f"ttl_days: {ttl}" if ttl is not None else "ttl_days: null"
    slug = slugify(atom["text"])
    path = VAULT_ATOMS / f"{cat}-{slug}.md"
    counter = 2
    while path.exists():
        path = VAULT_ATOMS / f"{cat}-{slug}-{counter}.md"
        counter += 1
    # Build source_excerpt block for frontmatter (truncated to cap file size)
    excerpt_lines = ""
    if source_excerpt:
        truncated = source_excerpt[:2000]
        indented = "\n".join("  " + line for line in truncated.splitlines())
        excerpt_lines = f"source_excerpt: |\n{indented}\n"
    path.write_text(
        f"---\ntype: atom\ncategory: {cat}\ntags: []\n"
        f"confidence: {conf:.2f}\ncorroborations: 1\ndomain: {domain}\n"
        f"source: {source_path}\ncreated_at: {today}\nupdated_at: {today}\n{ttl_line}\n"
        f"{excerpt_lines}---\n"
        f"{atom['text']}\n"
    )
    return path


def cmd_extract(session_path: str):
    path = Path(session_path).expanduser().resolve()
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    content = path.read_text(encoding="utf-8")

    # Pre-flight: skip sessions with no stable decisions (saves 1 LLM call)
    has_decisions = bool(
        re.search(r"^decisions:\s*\n\s+-", content, re.MULTILINE)
        or re.search(r"## Decisions Made", content)
    )
    if not has_decisions:
        print("No decisions content — skipping extraction.")
        return

    # Compute once — this is exactly what the LLM will see; store alongside atoms
    source_excerpt = _extract_content_for_llm(content)

    atoms = extract_atoms(content)
    if not atoms:
        print("No atoms extracted.")
        return

    db = open_db()
    today = datetime.now().strftime("%Y-%m-%d")
    new_count, corroborated_count = 0, 0

    # Load existing atom texts for cheap text-equality dedup before embedding
    existing_texts = {
        r[0].strip().lower()
        for r in db.execute("SELECT chunk FROM entries WHERE type = 'atom'").fetchall()
    }

    for atom in atoms:
        text_lower = atom["text"].strip().lower()

        # 1. Text equality check — free, no API call
        if text_lower in existing_texts:
            row = db.execute(
                "SELECT id FROM entries WHERE type = 'atom' AND lower(chunk) = ? LIMIT 1",
                [text_lower],
            ).fetchone()
            if row:
                bump_corroboration(db, row[0])
            corroborated_count += 1
            print(f"  corroborated (text match): {atom['text'][:70]}")
            continue

        # 2. Embedding similarity check
        try:
            vec = embed(atom["text"])
        except Exception as e:
            print(f"  WARN: embed failed, skipping atom: {e}", file=sys.stderr)
            continue
        existing_id = find_duplicate_atom(db, vec)
        if existing_id:
            bump_corroboration(db, existing_id)
            corroborated_count += 1
            print(f"  corroborated: {atom['text'][:70]}")
        else:
            cat = atom["category"]
            domain = classify_domain(atom["text"])
            conf = CONFIDENCE_PRIOR.get(cat, 0.50)
            atom_path = write_atom_file(atom, str(path), today, source_excerpt, domain=domain)
            cur = db.execute(
                "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, source_chunk, domain) "
                "VALUES (?, ?, ?, 'atom', ?, '', ?, 1, ?, ?)",
                [str(atom_path), today, atom["text"], atom["text"], conf, source_excerpt, domain],
            )
            db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
                       [cur.lastrowid, serialize(vec)])
            new_count += 1
            print(f"  new atom [{domain}]: {atom['text'][:70]}")

    db.commit()
    print(f"Extracted {new_count + corroborated_count} atoms ({new_count} new, {corroborated_count} corroborated)")


def cmd_rebuild():
    if not VAULT_SESSION_LOGS.exists():
        print(f"ERROR: session logs not found at {VAULT_SESSION_LOGS}", file=sys.stderr)
        sys.exit(1)

    # Wipe and recreate DB — safety check: never delete a file with evolution data
    if DB_PATH.exists():
        import sqlite3 as _sql
        _check = _sql.connect(DB_PATH)
        _tables = {r[0] for r in _check.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        _check.close()
        _evolution_tables = {"interactions", "reflections", "principles"}
        if _tables & _evolution_tables:
            print(f"ABORT: {DB_PATH} contains evolution tables {_tables & _evolution_tables}. "
                  f"Refusing to delete. Evolution data should be in DEUS_EVOLUTION_DB, not here.",
                  file=sys.stderr)
            sys.exit(1)
        DB_PATH.unlink()
    db = open_db()
    db.close()

    log_files = sorted(VAULT_SESSION_LOGS.rglob("*.md"))
    log_files = [f for f in log_files if ".obsidian" not in str(f)]
    print(f"Found {len(log_files)} session logs. Indexing...")

    ok = 0
    for f in log_files:
        try:
            cmd_add(str(f), extract=False)  # skip per-session extraction during bulk rebuild
            ok += 1
        except Exception as exc:
            print(f"  WARN: skipped {f.name}: {exc}", file=sys.stderr)

    # Re-index atoms (skip files already in DB with matching updated_at — mtime guard)
    atom_ok = 0
    if VAULT_ATOMS.exists():
        atom_files = sorted(VAULT_ATOMS.glob("*.md"))
        print(f"\nFound {len(atom_files)} atoms. Re-indexing...")
        db = open_db()
        for af in atom_files:
            try:
                content = af.read_text(encoding="utf-8")
                fm = extract_frontmatter(content)
                # Body = everything after the closing ---
                body = content[content.rfind("---") + 3:].strip()
                if not body:
                    continue
                # Mtime skip: if path + updated_at already in DB, skip embed call
                existing = db.execute(
                    "SELECT id FROM entries WHERE path = ? LIMIT 1", [str(af)]
                ).fetchone()
                if existing:
                    continue
                vec = embed(body)
                conf = float(fm.get("confidence", 0.5))
                corr = int(fm.get("corroborations", 1))
                date_str = fm.get("created_at", "")
                # Read domain and expired_at from atom frontmatter
                raw = fm.get("raw", "")
                atom_domain = "general"
                atom_expired_at = None
                atom_expired_reason = None
                for line in raw.splitlines():
                    if line.startswith("domain:"):
                        atom_domain = line.split(":", 1)[1].strip()
                    elif line.startswith("expired_at:"):
                        val = line.split(":", 1)[1].strip()
                        if val and val != "null":
                            atom_expired_at = val
                    elif line.startswith("expired_reason:"):
                        atom_expired_reason = line.split(":", 1)[1].strip() or None
                # If no domain in frontmatter, classify from body
                if atom_domain == "general":
                    atom_domain = classify_domain(body)
                # Restore source_excerpt from .md frontmatter into source_chunk column
                stored_excerpt = None
                excerpt_m = re.search(
                    r"^source_excerpt:\s*\|\n((?:  .*\n?)*)", raw, re.MULTILINE
                )
                if excerpt_m:
                    stored_excerpt = re.sub(r"^ {2}", "", excerpt_m.group(1), flags=re.MULTILINE).strip()
                cur = db.execute(
                    "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, source_chunk, domain, expired_at, expired_reason) "
                    "VALUES (?, ?, ?, 'atom', ?, ?, ?, ?, ?, ?, ?, ?)",
                    [str(af), date_str, body, body, fm.get("tags", ""), conf, corr, stored_excerpt, atom_domain, atom_expired_at, atom_expired_reason],
                )
                db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
                           [cur.lastrowid, serialize(vec)])
                atom_ok += 1
            except Exception as exc:
                print(f"  WARN: skipped atom {af.name}: {exc}", file=sys.stderr)
        db.commit()

    print(f"\nDone. {ok}/{len(log_files)} logs + {atom_ok} atoms indexed into {DB_PATH}")


# ── Health analytics ─────────────────────────────────────────────────────────

def _collect_health_metrics(db: sqlite3.Connection) -> dict:
    """Snapshot current memory system quality metrics from DB + filesystem."""
    from datetime import date as _date
    try:
        rows = db.execute(
            "SELECT path, confidence, corroborations, source_chunk, expired_at, domain "
            "FROM entries WHERE type='atom'"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = db.execute(
            "SELECT path, confidence, corroborations, NULL, NULL, 'general' FROM entries WHERE type='atom'"
        ).fetchall()
    total_atoms = len(rows)
    snapshot: dict = {"date": _date.today().isoformat(), "atoms": total_atoms}
    if total_atoms > 0:
        live_rows = [r for r in rows if not r[4]]  # r[4] = expired_at
        snapshot["expired"] = len(rows) - len(live_rows)
        snapshot["avg_confidence"] = round(sum(r[1] or 0.0 for r in live_rows) / max(len(live_rows), 1), 3)
        snapshot["corr_1x"]    = sum(1 for r in live_rows if (r[2] or 1) == 1)
        snapshot["corr_2x"]    = sum(1 for r in live_rows if (r[2] or 1) == 2)
        snapshot["corr_3plus"] = sum(1 for r in live_rows if (r[2] or 1) >= 3)
        snapshot["source_chunk_coverage"] = round(sum(1 for r in live_rows if r[3]) / max(len(live_rows), 1), 3)
        cats: dict[str, int] = {}
        for path, *_ in live_rows:
            stem = Path(path).stem
            cat = stem.split("-")[0] if "-" in stem else "unknown"
            cats[cat] = cats.get(cat, 0) + 1
        snapshot["categories"] = cats
        domains: dict[str, int] = {}
        for r in live_rows:
            d = r[5] or "general"
            domains[d] = domains.get(d, 0) + 1
        snapshot["domains"] = domains
    else:
        snapshot.update({
            "avg_confidence": 0.0, "corr_1x": 0, "corr_2x": 0, "corr_3plus": 0,
            "source_chunk_coverage": 0.0, "categories": {}, "expired": 0, "domains": {},
        })
    snapshot["sessions"] = (
        len([f for f in VAULT_SESSION_LOGS.rglob("*.md") if ".obsidian" not in str(f)])
        if VAULT_SESSION_LOGS.exists() else 0
    )
    return snapshot


def cmd_health(save: bool = True) -> None:
    """Print a memory health report and persist a daily snapshot for trend tracking.

    Tracks 6 improvement signals over time:
      atoms          — total extracted facts (growth = learning new knowledge)
      avg_confidence — rising = facts getting corroborated across sessions
      corroborations — 1x/2x/3x+ distribution; more 3x+ = stronger memory
      source_coverage— % of atoms with traceable context
      categories     — diversity of fact types extracted
      velocity       — atoms/day and corroboration rate between snapshots

    Snapshots persisted to ~/.deus/memory_health.jsonl (append-only JSONL).
    One snapshot per calendar day — idempotent within a day.
    """
    db = open_db()
    current = _collect_health_metrics(db)
    db.close()

    history: list[dict] = []
    if HEALTH_LOG_PATH.exists():
        for line in HEALTH_LOG_PATH.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    history.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    prev = history[-1] if history else None

    def _delta(curr: float, prev_val: float | None, higher_is_better: bool = True) -> str:
        if prev_val is None:
            return ""
        d = curr - prev_val
        if abs(d) < 1e-4:
            return " (→)"
        arrow = "↑" if (d > 0) == higher_is_better else "↓"
        sign = "+" if d > 0 else ""
        return f" ({arrow} {sign}{d:.3g})"

    total = current["atoms"] or 1
    out = [
        "## Memory Health",
        f"  snapshot: {current['date']}",
        "",
        f"Atoms: {current['atoms']}"
        + _delta(current["atoms"], prev["atoms"] if prev else None),
    ]
    if current.get("expired", 0) > 0:
        out.append(f"  expired: {current['expired']}")
    cats = current.get("categories", {})
    if cats:
        out.append("  categories: " + "  ".join(
            f"{k}:{v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])
        ))
    domains = current.get("domains", {})
    if domains:
        out.append("  domains: " + "  ".join(
            f"{k}:{v}" for k, v in sorted(domains.items(), key=lambda x: -x[1])
        ))
    out.append(
        f"  avg confidence: {current['avg_confidence']:.3f}"
        + _delta(current["avg_confidence"], prev.get("avg_confidence") if prev else None)
    )
    out.append(
        f"  corroborations: 1×={current['corr_1x']} ({100*current['corr_1x']//total}%)  "
        f"2×={current['corr_2x']} ({100*current['corr_2x']//total}%)  "
        f"3×+={current['corr_3plus']} ({100*current['corr_3plus']//total}%)"
        + _delta(
            current["corr_2x"] + current["corr_3plus"],
            (prev["corr_2x"] + prev["corr_3plus"]) if prev and "corr_2x" in prev else None,
        )
    )
    out.append(
        f"  source coverage: {100*current['source_chunk_coverage']:.1f}%"
        + _delta(current["source_chunk_coverage"],
                 prev.get("source_chunk_coverage") if prev else None)
    )
    sess = current["sessions"]
    ratio = f"{current['atoms']/sess:.1f}" if sess > 0 else "n/a"
    out.append(
        f"\nSessions: {sess}"
        + _delta(sess, prev["sessions"] if prev else None)
        + f"  (atom/session ratio: {ratio})"
    )

    out.append("")
    if prev:
        out.append(f"## Trends vs last snapshot ({prev['date']})")
        improvements, regressions = [], []
        atom_d = current["atoms"] - prev["atoms"]
        if atom_d > 0:
            improvements.append(f"+{atom_d} new atoms extracted")
        elif atom_d < 0:
            regressions.append(f"{atom_d} atoms lost (rebuild without re-extract?)")
        corr_now  = current["corr_2x"] + current["corr_3plus"]
        corr_prev = prev.get("corr_2x", 0) + prev.get("corr_3plus", 0)
        if corr_now - corr_prev > 0:
            improvements.append(f"+{corr_now - corr_prev} corroboration events (knowledge confirmed)")
        conf_d = current["avg_confidence"] - prev.get("avg_confidence", current["avg_confidence"])
        if conf_d > 0.005:
            improvements.append(f"confidence +{conf_d:.3f} (facts strengthening)")
        elif conf_d < -0.005:
            regressions.append(f"confidence {conf_d:.3f} (new weak atoms diluting average)")
        cov_d = current["source_chunk_coverage"] - prev.get("source_chunk_coverage", 0)
        if cov_d > 0.01:
            improvements.append(f"source coverage +{100*cov_d:.1f}pp (more traceable atoms)")
        for item in improvements:
            out.append(f"  ✓ {item}")
        for item in regressions:
            out.append(f"  ⚠ {item}")
        if not improvements and not regressions:
            out.append("  → no significant changes")
        try:
            from datetime import date as _date
            days_apart = (
                _date.fromisoformat(current["date"]) - _date.fromisoformat(prev["date"])
            ).days
            if days_apart > 0:
                out.append(
                    f"\n  Velocity: {atom_d/days_apart:.1f} atoms/day  "
                    f"({corr_now - corr_prev} corroborations over {days_apart}d)"
                )
        except (ValueError, TypeError):
            pass
    else:
        out.append("(run --health again after more sessions to see trends)")

    print("\n".join(out))

    if save:
        already_today = prev and prev.get("date") == current["date"]
        if not already_today:
            HEALTH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with HEALTH_LOG_PATH.open("a") as f:
                f.write(json.dumps(current) + "\n")
            print(f"\n[snapshot saved → {HEALTH_LOG_PATH}]")


def cmd_prune(dry_run: bool = False):
    """Enforce TTL-based expiry and clean up DB orphans.

    - Atoms whose TTL has elapsed get expired_at set (soft-delete).
    - DB rows whose atom file no longer exists get hard-deleted.
    """
    from datetime import date as _date
    db = open_db()
    today = _date.today()

    # 1. TTL enforcement
    ttl_expired = 0
    rows = db.execute(
        "SELECT id, path FROM entries WHERE type = 'atom' AND expired_at IS NULL"
    ).fetchall()
    for entry_id, path_str in rows:
        atom_path = Path(path_str)
        if not atom_path.exists():
            continue
        content = atom_path.read_text(encoding="utf-8")
        ttl_days = None
        created_at = None
        for line in content.splitlines():
            if line.startswith("ttl_days:"):
                val = line.split(":", 1)[1].strip()
                if val not in ("null", ""):
                    try:
                        ttl_days = int(val)
                    except ValueError:
                        pass
            elif line.startswith("created_at:"):
                created_at = line.split(":", 1)[1].strip()
        if ttl_days is not None and created_at:
            try:
                age = (today - _date.fromisoformat(created_at)).days
                if age > ttl_days:
                    if dry_run:
                        print(f"  [dry-run] would expire: {atom_path.name} (age={age}d, ttl={ttl_days}d)")
                    else:
                        invalidate_atom(db, entry_id, "ttl")
                    ttl_expired += 1
            except (ValueError, TypeError):
                pass

    # 2. Orphan cleanup: DB rows whose file is gone
    orphans = 0
    all_atom_rows = db.execute("SELECT id, path FROM entries WHERE type = 'atom'").fetchall()
    for entry_id, path_str in all_atom_rows:
        if not Path(path_str).exists():
            if dry_run:
                print(f"  [dry-run] would delete orphan: {Path(path_str).name}")
            else:
                db.execute("DELETE FROM embeddings WHERE rowid = ?", [entry_id])
                try:
                    db.execute("DELETE FROM entries_fts WHERE rowid = ?", [entry_id])
                except sqlite3.OperationalError:
                    pass
                db.execute("DELETE FROM entries WHERE id = ?", [entry_id])
            orphans += 1

    if not dry_run:
        db.commit()

    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}Prune complete: {ttl_expired} TTL-expired, {orphans} orphans cleaned")


def cmd_invalidate(path_str: str, reason: str):
    """Manually invalidate a specific atom by path."""
    db = open_db()
    path = Path(path_str).expanduser().resolve()
    row = db.execute("SELECT id FROM entries WHERE path = ? AND type = 'atom' LIMIT 1", [str(path)]).fetchone()
    if not row:
        print(f"ERROR: no atom entry found for {path}", file=sys.stderr)
        sys.exit(1)
    invalidate_atom(db, row[0], reason)


def cmd_gaps(top: int = 10):
    """Identify knowledge gaps: high-frequency session topics with low atom coverage."""
    if not VAULT_SESSION_LOGS.exists():
        print(f"ERROR: session logs not found at {VAULT_SESSION_LOGS}", file=sys.stderr)
        sys.exit(1)

    # 1. Count topic frequency across sessions
    session_topic_count: dict[str, int] = {}
    log_files = [f for f in VAULT_SESSION_LOGS.rglob("*.md") if ".obsidian" not in str(f)]
    for log_file in log_files:
        fm = extract_frontmatter(log_file.read_text(encoding="utf-8"))
        if not fm.get("topics"):
            continue
        for topic in fm["topics"].split(","):
            t = topic.strip().lower()
            if t:
                session_topic_count[t] = session_topic_count.get(t, 0) + 1

    # 2. Count atom coverage per topic
    atom_topic_coverage: dict[str, int] = {}
    if VAULT_ATOMS.exists():
        for atom_file in VAULT_ATOMS.glob("*.md"):
            body = atom_file.read_text(encoding="utf-8").lower()
            for topic in session_topic_count:
                if topic in body:
                    atom_topic_coverage[topic] = atom_topic_coverage.get(topic, 0) + 1

    # 3. Find gaps: frequent topics with no/low atom coverage
    gaps = []
    for topic, session_count in session_topic_count.items():
        if session_count < 3:
            continue
        atom_count = atom_topic_coverage.get(topic, 0)
        if atom_count <= 1:
            label = f"{atom_count} atom (weak)" if atom_count == 1 else "0 atoms"
            gaps.append((topic, session_count, atom_count, label))

    gaps.sort(key=lambda x: (-x[1], x[2]))

    print("## Knowledge Gaps")
    if gaps:
        for topic, sess, _, label in gaps[:top]:
            print(f"- {topic} — {sess} sessions, {label}")
    else:
        print("- No significant gaps found (all frequent topics have atom coverage)")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Deus memory indexer")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--add", metavar="PATH", help="Index a single session log")
    group.add_argument("--query", metavar="TEXT", help="Semantic search, returns top-K results")
    group.add_argument("--rebuild", action="store_true", help="Rebuild full index from scratch")
    group.add_argument(
        "--wander",
        nargs="*",
        metavar="TOPIC",
        help="Spreading activation from seed topics (no API key needed). "
             "E.g. --wander linear-algebra mechanics. Omit topics to seed from recent sessions.",
    )
    group.add_argument("--extract", metavar="PATH",
                       help="Extract atomic facts from a session log (uses Gemini Flash)")
    group.add_argument("--recent", type=int, metavar="N",
                       help="Return last N session frontmatters by date (no API call)")
    group.add_argument("--recent-days", type=int, metavar="N",
                       help="Return ALL sessions from the last N calendar days (no API call)")
    group.add_argument("--learnings", action="store_true",
                       help="Surface recently strengthened/new atoms since last /resume (no API call)")
    group.add_argument("--health", action="store_true",
                       help="Print memory health report (atom quality, confidence, coverage trends) "
                            "and save a daily snapshot to ~/.deus/memory_health.jsonl (no API call)")
    group.add_argument("--prune", action="store_true",
                       help="Enforce TTL expiry + clean orphan DB rows (no API call)")
    group.add_argument("--invalidate", metavar="PATH",
                       help="Manually invalidate (soft-delete) an atom by path")
    group.add_argument("--gaps", action="store_true",
                       help="Show knowledge gaps: frequent topics with low atom coverage (no API call)")
    parser.add_argument("--no-extract", action="store_true",
                        help="Skip atom extraction when using --add (useful for CI/benchmarks)")
    parser.add_argument("--top", type=int, default=3, help="Number of results for --query")
    parser.add_argument("--since", type=int, default=7,
                        help="Lookback window in days for --learnings (default: 7)")
    parser.add_argument("--steps", type=int, default=3, help="Activation steps for --wander")
    parser.add_argument("--recency-boost", action="store_true",
                        help="Boost recent results in --query (last 7d strong, 30d moderate)")
    parser.add_argument("--source", action="store_true",
                        help="Show source excerpt below each atom in --query results")
    parser.add_argument("--compact", action="store_true",
                        help="Compact output for --recent/--recent-days: truncate decisions, strip paths, "
                             "collapse cluster entries. Auto-triggered at >= 12 sessions.")
    parser.add_argument("--no-save", action="store_true",
                        help="Skip saving snapshot for --health (useful for CI/dry-run)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview --prune changes without applying them")
    parser.add_argument("--reason", default="manual",
                        help="Reason for --invalidate (default: manual)")
    parser.add_argument("--domain", metavar="DOMAIN",
                        help="Filter --query results by domain (dev/study/trading/personal/general)")
    args = parser.parse_args()

    global _client
    # Commands that need no API key
    if args.wander is not None:
        cmd_wander(args.wander or [], steps=args.steps, top_k=args.top or 10)
        return
    if args.recent is not None:
        cmd_recent(args.recent, compact=args.compact)
        return
    if args.recent_days is not None:
        cmd_recent(args.recent_days, days=True, compact=args.compact)
        return
    if args.learnings:
        cmd_learnings(since_days=args.since, max_items=args.top)
        return
    if args.health:
        cmd_health(save=not args.no_save)
        return
    if args.prune:
        cmd_prune(dry_run=args.dry_run)
        return
    if args.invalidate:
        cmd_invalidate(args.invalidate, reason=args.reason)
        return
    if args.gaps:
        cmd_gaps(top=args.top)
        return

    _client = genai.Client(api_key=load_api_key())

    if args.add:
        cmd_add(args.add, extract=not args.no_extract)
    elif args.query:
        cmd_query(args.query, top=args.top, recency_boost=args.recency_boost,
                  show_source=args.source, domain=args.domain)
    elif args.rebuild:
        cmd_rebuild()
    elif args.extract:
        cmd_extract(args.extract)


if __name__ == "__main__":
    main()
