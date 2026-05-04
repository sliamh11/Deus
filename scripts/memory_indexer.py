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
import time
from datetime import datetime  # noqa: F401 -- kept for fromtimestamp / type imports
from pathlib import Path

# Allow running as a direct script — add project root to sys.path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Local helpers — _time.py lives next to this script.
_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)
from _time import local_now, utc_now  # noqa: E402

import sqlite_vec
from google import genai
from google.genai import types as genai_types

from evolution.config import (
    EMBED_DIM,
    EVOLUTION_DB_PATH,
    GEN_MODELS,
    load_api_key as _load_api_key,
)
from evolution.providers.embeddings import warmup_embedding_provider

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path("~/.config/deus/config.json").expanduser()
DB_PATH = Path(os.environ.get("DEUS_DB", "~/.deus/memory.db")).expanduser()
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
VAULT_ENTITIES = _vault_root / "Entities"
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

CATEGORY_SECTIONS: dict[str, tuple[str, str]] = {
    "constraint": ("Active Constraints", "Enforce these — they are verified rules or limits."),
    "decision":   ("Prior Decisions", "Decisions already made — follow unless explicitly revisited."),
    "fact":       ("Known Facts", "Established facts with strong corroboration."),
    "preference": ("Preferences", "User preferences — respect unless overridden."),
    "belief":     ("Working Beliefs", "Consider but don't assert — these may evolve."),
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


def embed_batch(texts: list[str]) -> list[list[float]]:
    from evolution.providers.embeddings import embed_batch as _provider_embed_batch
    return _provider_embed_batch(texts)


def serialize(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def deserialize(buf: bytes) -> list[float]:
    n = len(buf) // 4
    return list(struct.unpack(f"{n}f", buf))


# ── DB ────────────────────────────────────────────────────────────────────────

def _backfill_category(db: sqlite3.Connection) -> None:
    """Populate NULL category columns from atom file frontmatter.

    Per-row filesystem errors are caught and default to 'fact'.
    SQLite errors propagate — a broken DB should fail loudly.
    """
    try:
        rows = db.execute(
            "SELECT id, path FROM entries WHERE category IS NULL AND type = 'atom' AND orphaned_at IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        return
    if not rows:
        return
    for entry_id, path_str in rows:
        cat = "fact"
        try:
            text = Path(path_str).read_text()
            m = re.search(r"^category:\s*(\S+)", text, re.MULTILINE)
            if m:
                cat = m.group(1)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  WARN: backfill category fallback for {path_str}: {exc}", file=sys.stderr)
        db.execute("UPDATE entries SET category = ? WHERE id = ?", [cat, entry_id])
    db.commit()


def _backfill_fts(db: sqlite3.Connection) -> None:
    """Insert any entries rows not yet in the standalone FTS5 index.

    Compares counts; if FTS5 is behind, inserts missing rows by rowid.
    Idempotent and fast at Deus vault scale (~hundreds of rows).
    Silently skips if FTS5 is unavailable.
    """
    try:
        fts_count = db.execute("SELECT COUNT(*) FROM entries_fts").fetchone()[0]
        entries_count = db.execute("SELECT COUNT(*) FROM entries WHERE orphaned_at IS NULL").fetchone()[0]
        if fts_count < entries_count:
            # Insert active entries rows that don't yet have an FTS5 counterpart
            db.execute("""
                INSERT INTO entries_fts(rowid, chunk)
                SELECT e.id, e.chunk FROM entries e
                WHERE e.id NOT IN (SELECT rowid FROM entries_fts)
                AND e.orphaned_at IS NULL
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
    # safe: EMBED_DIM is a module-level int constant imported from
    # evolution.config. SQLite cannot parameterize vec0 dimensions.
    # See PR #9 in docs/decisions/error-discipline.md.
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
        ("category", "TEXT DEFAULT NULL"),
    ]:
        try:
            # safe: col + definition come from the literal tuple-list above.
            # SQLite DDL cannot parameterize identifiers or column types.
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
    # Phase 2: entity/relationship graph tables
    db.execute("""
        CREATE TABLE IF NOT EXISTS entities (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            entity_type   TEXT NOT NULL,
            domain        TEXT,
            first_seen    TEXT NOT NULL,
            last_seen     TEXT NOT NULL,
            mention_count INTEGER DEFAULT 1,
            summary       TEXT,
            UNIQUE(name, entity_type)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS relationships (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            source_id      INTEGER NOT NULL REFERENCES entities(id),
            target_id      INTEGER NOT NULL REFERENCES entities(id),
            rel_type       TEXT NOT NULL,
            confidence     REAL DEFAULT 0.5,
            first_seen     TEXT NOT NULL,
            last_seen      TEXT NOT NULL,
            evidence_count INTEGER DEFAULT 1,
            expired_at     TEXT,
            UNIQUE(source_id, target_id, rel_type)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS atom_entities (
            atom_id   INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
            entity_id INTEGER NOT NULL REFERENCES entities(id),
            PRIMARY KEY (atom_id, entity_id)
        )
    """)
    try:
        db.execute("CREATE INDEX IF NOT EXISTS idx_atom_entities_entity ON atom_entities(entity_id)")
    except sqlite3.OperationalError:
        pass
    # Phase 3: entity articles + digest tables
    db.execute("""
        CREATE TABLE IF NOT EXISTS entity_articles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_id    INTEGER NOT NULL REFERENCES entities(id),
            vault_path   TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            source_hash  TEXT NOT NULL,
            UNIQUE(entity_id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS digests (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            level      TEXT NOT NULL,
            period_key TEXT NOT NULL,
            content    TEXT NOT NULL,
            atom_ids   TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(level, period_key)
        )
    """)
    try:
        db.execute("ALTER TABLE entries ADD COLUMN query_intent TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    # Phase 4: access/query logging, synthesis, privacy, temperature
    db.execute("""
        CREATE TABLE IF NOT EXISTS access_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entry_id    INTEGER NOT NULL REFERENCES entries(id),
            accessed_at TEXT NOT NULL,
            access_type TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS query_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            query_text   TEXT NOT NULL,
            intent       TEXT,
            result_count INTEGER DEFAULT 0,
            atom_hit     INTEGER DEFAULT 0,
            queried_at   TEXT NOT NULL
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS synthesis_suggestions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_a_id INTEGER NOT NULL REFERENCES entities(id),
            entity_b_id INTEGER NOT NULL REFERENCES entities(id),
            bridge_text TEXT NOT NULL,
            confidence  REAL DEFAULT 0.5,
            created_at  TEXT NOT NULL,
            dismissed   INTEGER DEFAULT 0,
            UNIQUE(entity_a_id, entity_b_id)
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS pending_conflicts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            older_id    INTEGER NOT NULL,
            newer_id    INTEGER NOT NULL,
            older_text  TEXT,
            newer_text  TEXT,
            created_at  TEXT NOT NULL,
            resolved    INTEGER DEFAULT 0,
            resolution  TEXT,
            UNIQUE(older_id, newer_id)
        )
    """)
    for col, definition in [
        ("privacy", "TEXT DEFAULT 'internal'"),
        ("temperature", "REAL DEFAULT 1.0"),
        ("orphaned_at", "TEXT DEFAULT NULL"),
        ("orphan_reason", "TEXT DEFAULT NULL"),
    ]:
        try:
            # safe: col + definition come from the literal tuple-list above.
            # SQLite DDL cannot parameterize identifiers or column types.
            db.execute(f"ALTER TABLE entries ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass
    db.commit()
    _backfill_category(db)
    _backfill_fts(db)
    return db


def entry_exists(db: sqlite3.Connection, path: str) -> bool:
    row = db.execute("SELECT 1 FROM entries WHERE path = ? AND orphaned_at IS NULL LIMIT 1", [path]).fetchone()
    return row is not None


def soft_delete_entries(db: sqlite3.Connection, path: str, reason: str = "re-indexed"):
    """Mark entries for a path as orphaned (soft-delete). See ADR: no-db-deletion.md."""
    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "UPDATE entries SET orphaned_at = ?, orphan_reason = ? WHERE path = ? AND orphaned_at IS NULL",
        [now, reason, path],
    )
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

def _collect_chunks_for_file(path: Path):
    """Read a file and return (path, chunks) — no embedding, no DB writes yet."""
    content = path.read_text(encoding="utf-8")
    return path, chunks_for_log(path, content)


def _write_chunks_batched(
    db, path_to_chunks: list[tuple[Path, list[dict]]]
) -> int:
    """Batch-embed all chunks across multiple files, then persist.

    One HTTP call (or a few, sub-batched inside the provider) handles every
    chunk in the input — the hot-path optimization for bulk indexing. Falls
    back to per-chunk embed if the batched helper is unavailable.
    """
    flat: list[tuple[Path, dict]] = []
    for path, chunks in path_to_chunks:
        for ch in chunks:
            flat.append((path, ch))
    if not flat:
        return 0

    texts = [ch["chunk"] for _, ch in flat]
    try:
        vecs = embed_batch(texts)
    except Exception as exc:
        # If the provider doesn't support true batching (or it failed),
        # fall back to sequential embeds so the caller still makes progress.
        print(
            f"  WARN: batch embed failed ({exc}); falling back to sequential",
            file=sys.stderr,
        )
        vecs = [embed(t) for t in texts]

    indexed = 0
    for (path, chunk), vec in zip(flat, vecs):
        cur = db.execute(
            "INSERT INTO entries (path, date, chunk, type, tldr, topics, decisions) VALUES (?,?,?,?,?,?,?)",
            [
                str(path),
                chunk["date"],
                chunk["chunk"],
                chunk["type"],
                chunk["tldr"],
                chunk["topics"],
                chunk["decisions"],
            ],
        )
        rowid = cur.lastrowid
        db.execute(
            "INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
            [rowid, serialize(vec)],
        )
        try:
            db.execute(
                "INSERT INTO entries_fts(rowid, chunk) VALUES (?, ?)",
                [rowid, chunk["chunk"]],
            )
        except sqlite3.OperationalError:
            pass
        indexed += 1
    return indexed


def cmd_add(path_str: str, extract: bool = True):
    path = Path(path_str).expanduser().resolve()
    if not path.exists():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        sys.exit(1)

    db = open_db()
    # Soft-delete stale entries for this path (re-indexing)
    soft_delete_entries(db, str(path), reason="re-indexed")

    _, chunks = _collect_chunks_for_file(path)
    if not chunks:
        print(f"No indexable content in {path.name}")
        return

    indexed = _write_chunks_batched(db, [(path, chunks)])
    db.commit()
    print(f"Indexed {indexed} chunk(s) from {path.name}")

    if extract:
        try:
            cmd_extract(str(path))
        except SystemExit:
            pass
        except Exception as exc:
            print(f"  WARN: atom extraction failed for {path.name}: {exc}", file=sys.stderr)

    try:
        stale_count = 0
        articles = db.execute(
            "SELECT ea.entity_id, ea.source_hash FROM entity_articles ea"
        ).fetchall()
        for entity_id, old_hash in articles:
            new_hash = _compute_entity_source_hash(db, entity_id)
            if new_hash != old_hash:
                stale_count += 1
        if stale_count > 0:
            print(f"  [stale] {stale_count} entity article(s) need recompile (run --compile)")
    except (sqlite3.OperationalError, Exception):
        pass


def cmd_add_dir(dir_str: str, extract: bool = True):
    """Batch-index every .md file under a directory.

    Written for bulk workloads (benchmarks, initial vault ingestion): chunks
    are collected up front, embedded in a single batched HTTP call, then
    persisted. This is the path that avoids the "spawn N subprocesses, make N
    one-shot HTTP requests" hazard that stalled LongMemEval around example 31.
    """
    dir_path = Path(dir_str).expanduser().resolve()
    if not dir_path.is_dir():
        print(f"ERROR: directory not found: {dir_path}", file=sys.stderr)
        sys.exit(1)

    files = sorted(
        p for p in dir_path.rglob("*.md") if ".obsidian" not in str(p)
    )
    if not files:
        print(f"No .md files in {dir_path}")
        return

    db = open_db()
    path_to_chunks: list[tuple[Path, list[dict]]] = []
    for f in files:
        soft_delete_entries(db, str(f), reason="re-indexed")
        _, chunks = _collect_chunks_for_file(f)
        if chunks:
            path_to_chunks.append((f, chunks))

    indexed = _write_chunks_batched(db, path_to_chunks)
    db.commit()
    print(f"Indexed {indexed} chunk(s) across {len(path_to_chunks)} file(s) in {dir_path.name}")

    if extract:
        for f, _ in path_to_chunks:
            try:
                cmd_extract(str(f))
            except SystemExit:
                pass
            except Exception as exc:
                print(f"  WARN: atom extraction failed for {f.name}: {exc}", file=sys.stderr)

    # Phase 3: check for stale entity articles
    try:
        stale_count = 0
        articles = db.execute(
            "SELECT ea.entity_id, ea.source_hash FROM entity_articles ea"
        ).fetchall()
        for entity_id, old_hash in articles:
            new_hash = _compute_entity_source_hash(db, entity_id)
            if new_hash != old_hash:
                stale_count += 1
        if stale_count > 0:
            print(f"  [stale] {stale_count} entity article(s) need recompile (run --compile)")
    except (sqlite3.OperationalError, Exception):
        pass


COMPACT_SESSION_THRESHOLD = 12  # auto-enable compact mode above this many sessions


_STOP_WORDS = frozenset(
    "a an the and or but in on of to for with from by at is was were are be "
    "been has had have do does did will would shall should can could may might "
    "it its this that these those i we he she they my our his her their "
    "not no nor so if then than up out about into over after before".split()
)


def _subject_from_tldr(tldr: str) -> str:
    words = [w for w in re.split(r"[\s\-—:,./]+", tldr.lower()) if w and w not in _STOP_WORDS]
    return words[0] if words else ""


def _first_topic(fm: dict) -> str:
    topics = fm.get("topics", "") or ""
    if topics:
        first = topics.split(",")[0].strip("[] ").lower()
        if first:
            return first
    tldr = fm.get("tldr", "") or ""
    if tldr:
        return _subject_from_tldr(tldr)
    return ""


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
        # Dedup by primary topic so a burst on one subject doesn't bury unrelated contexts.
        seen_topics: set[str] = set()
        selected: list[tuple[str, Path]] = []
        for date, path in dated:
            content = path.read_text(encoding="utf-8")
            fm = extract_frontmatter(content)
            topic = _first_topic(fm)
            if topic not in seen_topics:
                seen_topics.add(topic)
                selected.append((date, path))
                if len(selected) >= n:
                    break

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
    by_cat: dict[str, list[dict]] = {}
    for item in selected:
        by_cat.setdefault(item["category"], []).append(item)
    for cat_key, (header, _framing) in CATEGORY_SECTIONS.items():
        bucket = by_cat.pop(cat_key, None)
        if not bucket:
            continue
        lines.append(f"### {header}")
        for item in bucket:
            prefix = "Pattern confirmed" if item["is_strengthened"] else "New insight"
            suffix = f" (seen across {item['corroborations']} sessions)" if item["corroborations"] >= 2 else ""
            lines.append(f"- {prefix}: {item['body']}{suffix}")
    for cat_key, bucket in by_cat.items():
        lines.append(f"### {cat_key.title()}")
        for item in bucket:
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
              show_source: bool = False, domain: str | None = None,
              intent: str | None = None, as_of: str | None = None,
              privacy: str | None = None,
              allowed_privacy: list[str] | None = None):
    db = open_db()

    # Check if anything is indexed
    count = db.execute("SELECT COUNT(*) FROM entries WHERE orphaned_at IS NULL").fetchone()[0]
    if count == 0:
        print("(index empty — run --rebuild first)", file=sys.stderr)
        sys.exit(1)

    # Intent classification
    resolved_intent = intent or classify_query_intent(query)

    has_atoms = db.execute("SELECT COUNT(*) FROM entries WHERE type = 'atom' AND orphaned_at IS NULL").fetchone()[0] > 0

    # Exhaustive mode: widen the search
    effective_top = 20 if resolved_intent == "exhaustive" else top

    q_vec = embed(query)

    # Build query with optional --as-of filter
    where_clauses = [
        "v.embedding MATCH ?",
        "k = ?",
        "(e.expired_at IS NULL OR e.expired_at > date('now'))",
        "e.orphaned_at IS NULL",
    ]
    params: list = [serialize(q_vec), max(effective_top * 6, 30)]
    if as_of:
        where_clauses.append("e.date <= ?")
        params.append(as_of)

    # safe: where_clauses is built from local literal-string fragments
    # ("v.embedding MATCH ?", "k = ?", ..., "e.date <= ?"); user values
    # bound through `params`. See PR #9 in docs/decisions/error-discipline.md.
    rows = db.execute(
        f"""
        SELECT e.path, e.date, e.tldr, e.topics, e.decisions, e.type,
               e.confidence, e.corroborations, v.distance, e.source_chunk,
               e.category
        FROM embeddings v
        JOIN entries e ON e.id = v.rowid
        WHERE {' AND '.join(where_clauses)}
        ORDER BY v.distance
        """,
        params,
    ).fetchall()

    # Partition into atoms and sessions; deduplicate sessions by path
    atom_results: list[dict] = []
    seen: dict[str, dict] = {}
    privacy_filtered = 0  # track how many sensitive atoms were blocked

    # Resolve effective privacy allowlist once (not per-atom)
    effective_allowlist = _resolve_privacy_allowlist(allowed_privacy)

    for path, date, tldr, topics, decisions, chunk_type, confidence, corroborations, dist, source_chunk, category in rows:
        if chunk_type == "atom":
            # Apply domain filter if specified
            if domain:
                entry_domain = db.execute(
                    "SELECT domain FROM entries WHERE path = ? LIMIT 1", [path]
                ).fetchone()
                if entry_domain and entry_domain[0] != domain:
                    continue
            # Apply privacy filter
            entry_privacy = db.execute(
                "SELECT privacy FROM entries WHERE path = ? LIMIT 1", [path]
            ).fetchone()
            atom_privacy = entry_privacy[0] if entry_privacy else "internal"
            if effective_allowlist:
                if atom_privacy not in effective_allowlist:
                    privacy_filtered += 1
                    continue
            elif privacy:
                # Legacy single-level filter: show only this level
                if atom_privacy != privacy:
                    continue
            elif atom_privacy == "sensitive":
                # Default: exclude sensitive
                privacy_filtered += 1
                continue
            # Temperature: used for ranking, not exclusion.
            # Cold atoms rank lower but are never fully hidden.
            entry_temp = db.execute(
                "SELECT temperature FROM entries WHERE path = ? LIMIT 1", [path]
            ).fetchone()
            atom_temp = entry_temp[0] if entry_temp and entry_temp[0] is not None else 1.0
            if has_atoms and len(atom_results) < top:
                atom_results.append({
                    "path": path, "chunk": tldr, "confidence": confidence or 0.0,
                    "corroborations": corroborations or 1,
                    "source_chunk": source_chunk or "",
                    "dist": dist, "temperature": atom_temp,
                    "category": category or "fact",
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
                    "WHERE path = ? AND orphaned_at IS NULL LIMIT 1", [path]
                ).fetchone()
                if row:
                    fused_seen[path] = {
                        "path": row[0], "date": row[1], "tldr": row[2],
                        "topics": row[3], "decisions": row[4],
                        "type": row[5], "dist": 999.0,
                    }
        seen = fused_seen

    # Post-filter by as_of (FTS results bypass the ANN date filter)
    if as_of and seen:
        seen = {p: e for p, e in seen.items() if not e.get("date") or e["date"] <= as_of}

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

    # Confidence + temperature weighted re-ranking
    for a in atom_results:
        a["score"] = a.get("dist", 999.0) - 0.25 * a["confidence"] - 0.15 * a.get("temperature", 1.0)
    atom_results.sort(key=lambda a: a["score"])

    # Category-aware atom sections — only atoms with ≥ 2 corroborations
    high_conf = [a for a in atom_results if a["corroborations"] >= 2]
    if high_conf:
        by_cat: dict[str, list[dict]] = {}
        for a in high_conf:
            by_cat.setdefault(a["category"], []).append(a)
        # Within each category: atoms in the same 0.1 distance band
        # sort by corroborations desc (tiebreaker); otherwise by score.
        for cat_atoms in by_cat.values():
            cat_atoms.sort(key=lambda a: (round(a["score"] / 0.1), -a["corroborations"]))
        for cat_key, (header, framing) in CATEGORY_SECTIONS.items():
            bucket = by_cat.get(cat_key)
            if not bucket:
                continue
            lines.append(f"## {header}")
            lines.append(f"> {framing}")
            for a in bucket:
                text = a["chunk"] or ""
                lines.append(f"- [{cat_key} | {a['confidence']:.2f}] {text} ({a['corroborations']}x)")
                if show_source and a.get("source_chunk"):
                    excerpt = a["source_chunk"][:400].replace("\n", "\n    ")
                    lines.append(f"  Source context:\n    {excerpt}")
            lines.append("")
        # Atoms with unrecognized categories (not in CATEGORY_SECTIONS)
        for cat_key, bucket in by_cat.items():
            if cat_key in CATEGORY_SECTIONS:
                continue
            lines.append(f"## {cat_key.title()}")
            for a in bucket:
                text = a["chunk"] or ""
                lines.append(f"- [{cat_key} | {a['confidence']:.2f}] {text} ({a['corroborations']}x)")
                if show_source and a.get("source_chunk"):
                    excerpt = a["source_chunk"][:400].replace("\n", "\n    ")
                    lines.append(f"  Source context:\n    {excerpt}")
            lines.append("")

    if seen:
        if resolved_intent == "temporal" and as_of:
            lines.append(f"## Relevant Past Sessions (as-of {as_of})")
        else:
            lines.append("## Relevant Past Sessions")
        for entry in list(seen.values())[:effective_top]:
            date = entry["date"] or "unknown date"
            name = Path(entry["path"]).stem.replace("-", " ")
            tldr = (entry["tldr"] or "").split(".")[0][:80]
            decisions = entry["decisions"] or ""
            dec_part = f" | {decisions}" if decisions else ""
            lines.append(f"- [{date} | {name}]{dec_part} — {tldr}")
            lines.append(f"  (full log: {entry['path']})")

    # Exhaustive: append matching digests
    if resolved_intent == "exhaustive":
        try:
            digests = db.execute(
                "SELECT level, period_key, content FROM digests ORDER BY period_key DESC"
            ).fetchall()
            if digests:
                lines.append("")
                lines.append("## Period Digests")
                for level, pk, content in digests[:10]:
                    lines.append(f"### {level}: {pk}")
                    lines.append(content[:500])
                    lines.append("")
        except sqlite3.OperationalError:
            pass

    # Notify when atoms were filtered by privacy
    if privacy_filtered > 0:
        lines.append("")
        if effective_allowlist:
            lines.append(
                f"({privacy_filtered} result(s) blocked by channel privacy policy — "
                f"this channel can only access: {', '.join(effective_allowlist)}. "
                f"Change channel privacy via /settings memory_privacy=level1,level2)"
            )
        else:
            lines.append(
                f"({privacy_filtered} result(s) blocked by privacy filter — "
                f"sensitive atoms are excluded by default. "
                f"Use --privacy sensitive to query them explicitly.)"
            )

    print("\n".join(lines))

    # Phase 4: log query for blind-spot analysis
    try:
        log_query(db, query, resolved_intent,
                  result_count=len(seen), atom_hit=len(atom_results))
    except Exception:
        pass

    db.close()


def cmd_wander(seeds: list[str], steps: int = 3, top_k: int = 10, graph: bool = False):
    """
    Spreading activation over session log topics (no embeddings needed).
    Builds a topic co-occurrence graph from session logs, then spreads activation
    from seed topics to discover cross-domain connections.

    With --graph: uses the entity relationship graph instead (Phase 2).
    """
    if graph:
        cmd_wander_graph(seeds, steps=steps, top_k=top_k)
        return
    from collections import defaultdict

    if not VAULT_SESSION_LOGS.exists():
        print(f"ERROR: session logs not found at {VAULT_SESSION_LOGS}", file=sys.stderr)
        sys.exit(1)

    today = local_now().date()

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


def _is_quota_error(exc: Exception) -> bool:
    """Return True if `exc` looks like a Gemini per-minute / per-day quota error."""
    msg = str(exc)
    return "429" in msg or "RESOURCE_EXHAUSTED" in msg


def _generate_with_fallback(
    prompt,
    *,
    config=None,
    label: str = "gen",
):
    """Call Gemini generate_content cascading through GEN_MODELS on 429.

    Behavior:
      - For each model in GEN_MODELS: call once; on 429 sleep 1s and retry
        the SAME model once more; on second 429, move to the next model.
      - Non-429 exceptions propagate to the caller (do not silently swallow).
      - On full exhaustion: emit a JSON-parseable warning line and return None.

    `prompt`  — `contents` arg passed straight through to generate_content.
    `config`  — a `genai_types.GenerateContentConfig` (or None).
    `label`   — short tag for the structured log; helps identify the call site
                when greping `/compress` output.

    Returns the response object on first success, or None if all models are
    quota-exhausted.
    """
    if _client is None:
        # Keep parity with callers that would otherwise crash on attribute access.
        raise RuntimeError("Gemini client not initialized (call main() entry first).")

    kwargs = {"contents": prompt}
    if config is not None:
        kwargs["config"] = config

    for model in GEN_MODELS:
        for attempt in (1, 2):
            try:
                return _client.models.generate_content(model=model, **kwargs)
            except Exception as exc:
                if _is_quota_error(exc):
                    if attempt == 1:
                        # brief within-model backoff — helps per-minute RPM caps
                        time.sleep(1)
                        continue
                    # second 429: move on to the next model
                    print(
                        f"  quota exhausted on {model} ({label}), trying fallback...",
                        file=sys.stderr,
                    )
                    break
                # non-quota error: re-raise so caller can decide
                raise
    # All models exhausted — structured, JSON-parseable warning for log scraping.
    print(
        "WARN "
        + json.dumps(
            {
                "event": "gen_exhausted",
                "label": label,
                "models_tried": len(GEN_MODELS),
            }
        ),
        file=sys.stderr,
    )
    return None


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
    try:
        response = _generate_with_fallback(
            prompt,
            config=genai_types.GenerateContentConfig(temperature=0.1, max_output_tokens=1024),
            label="extract_atoms",
        )
    except Exception as e:
        # Non-quota error (bad key, network, etc.) — no point retrying
        print(f"  WARN: extraction error: {e}", file=sys.stderr)
        return []
    if response is None:
        print("  WARN: all generation models quota-exhausted, skipping extraction.", file=sys.stderr)
        return []
    try:
        raw = response.text.strip()
        # Strip markdown fencing (opening and closing) if model adds it
        if raw.startswith("```"):
            raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
        atoms = json.loads(raw)
        return [a for a in atoms if isinstance(a, dict) and "text" in a and "category" in a]
    except json.JSONDecodeError:
        return []


def find_duplicate_atom(db: sqlite3.Connection, vec: list[float]) -> int | None:
    """Return the entry id of an existing atom within DEDUP_L2_THRESHOLD, or None."""
    atom_count = db.execute("SELECT COUNT(*) FROM entries WHERE type = 'atom' AND orphaned_at IS NULL").fetchone()[0]
    if atom_count == 0:
        return None  # short-circuit: nothing to compare against
    row = db.execute(
        """
        SELECT e.id, v.distance
        FROM embeddings v
        JOIN entries e ON e.id = v.rowid
        WHERE e.type = 'atom'
          AND (e.expired_at IS NULL OR e.expired_at > date('now'))
          AND e.orphaned_at IS NULL
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
    row = db.execute("SELECT path, corroborations, category FROM entries WHERE id = ?", [entry_id]).fetchone()
    if not row:
        return
    new_corr = row[1] + 1
    cat = row[2] or "fact"
    if not row[2]:
        # DB category is NULL (pre-backfill atom) — fall back to filesystem
        atom_path_tmp = Path(row[0])
        try:
            text = atom_path_tmp.read_text()
            m = re.search(r"^category:\s*(\S+)", text, re.MULTILINE)
            if m:
                cat = m.group(1)
        except (OSError, UnicodeDecodeError) as exc:
            print(f"  WARN: bump_corroboration category fallback for {row[0]}: {exc}", file=sys.stderr)
    atom_path = Path(row[0])
    prior = CONFIDENCE_PRIOR.get(cat, 0.50)
    new_conf = min(prior + new_corr * 0.1, 0.95)
    today = local_now().strftime("%Y-%m-%d")
    db.execute(
        "UPDATE entries SET corroborations = ?, confidence = ? WHERE id = ?",
        [new_corr, new_conf, entry_id],
    )
    db.commit()
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
    today = local_now().strftime("%Y-%m-%d")
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


# ── Phase 2: Entity/relationship graph ───────────────────────────────────────


def upsert_entity(db: sqlite3.Connection, name: str, entity_type: str,
                  domain: str | None, date: str) -> int:
    """Insert or update an entity. Returns entity id."""
    name = name.strip().lower()
    entity_type = entity_type.strip().lower()
    domain = domain or classify_domain(name)
    db.execute(
        """INSERT INTO entities (name, entity_type, domain, first_seen, last_seen, mention_count)
           VALUES (?, ?, ?, ?, ?, 1)
           ON CONFLICT(name, entity_type) DO UPDATE SET
             last_seen = excluded.last_seen,
             mention_count = mention_count + 1""",
        [name, entity_type, domain, date, date],
    )
    row = db.execute(
        "SELECT id FROM entities WHERE name = ? AND entity_type = ?",
        [name, entity_type],
    ).fetchone()
    return row[0]


def upsert_relationship(db: sqlite3.Connection, source_id: int, target_id: int,
                        rel_type: str, confidence: float, date: str) -> int:
    """Insert or update a relationship edge. Returns relationship id."""
    rel_type = rel_type.strip().lower()
    db.execute(
        """INSERT INTO relationships (source_id, target_id, rel_type, confidence, first_seen, last_seen)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(source_id, target_id, rel_type) DO UPDATE SET
             last_seen = excluded.last_seen,
             evidence_count = evidence_count + 1,
             confidence = MIN(confidence + 0.05, 0.95),
             expired_at = NULL""",
        [source_id, target_id, rel_type, confidence, date, date],
    )
    row = db.execute(
        "SELECT id FROM relationships WHERE source_id = ? AND target_id = ? AND rel_type = ?",
        [source_id, target_id, rel_type],
    ).fetchone()
    return row[0]


def link_atom_entities(db: sqlite3.Connection, atom_id: int,
                       entity_refs: list[tuple[str, str]]):
    """Link an atom to its mentioned entities via the junction table."""
    for name, entity_type in entity_refs:
        name = name.strip().lower()
        entity_type = entity_type.strip().lower()
        row = db.execute(
            "SELECT id FROM entities WHERE name = ? AND entity_type = ?",
            [name, entity_type],
        ).fetchone()
        if row:
            db.execute(
                "INSERT OR IGNORE INTO atom_entities (atom_id, entity_id) VALUES (?, ?)",
                [atom_id, row[0]],
            )


def _ent_rel_prompt(content: str) -> str:
    """Build prompt for entity/relationship extraction."""
    return (
        "Extract entities and relationships from this session log.\n\n"
        "Entities are people, projects, tools, concepts, or organizations mentioned.\n"
        "Relationships connect two entities (e.g. 'user uses docker', 'deus depends_on sqlite').\n\n"
        "Return ONLY a JSON object, no markdown fencing:\n"
        '{"entities": [{"name": "...", "entity_type": "person|project|tool|concept|org", "summary": "..."}],\n'
        ' "relationships": [{"source": "...", "target": "...", "rel_type": "uses|works_on|prefers|knows|depends_on|related_to", "confidence": 0.0-1.0}]}\n\n'
        "Rules:\n"
        "- Max 10 entities, 10 relationships\n"
        "- Names should be lowercase, canonical (e.g. 'docker' not 'Docker containers')\n"
        "- Skip generic entities ('code', 'file', 'bug')\n"
        "- If nothing meaningful, return {\"entities\": [], \"relationships\": []}\n\n"
        f"SESSION LOG:\n{_extract_content_for_llm(content)}"
    )


def extract_entities_and_relations(content: str) -> dict:
    """Extract entities and relationships from a session log via Gemini Flash."""
    prompt = _ent_rel_prompt(content)
    try:
        response = _generate_with_fallback(
            prompt,
            config=genai_types.GenerateContentConfig(temperature=0.1, max_output_tokens=1024),
            label="extract_entities_and_relations",
        )
    except Exception as e:
        print(f"  WARN: entity extraction error: {e}", file=sys.stderr)
        return {"entities": [], "relationships": []}
    if response is None:
        print("  WARN: all models quota-exhausted, skipping entity extraction.", file=sys.stderr)
        return {"entities": [], "relationships": []}
    try:
        raw = response.text.strip()
        if raw.startswith("```"):
            raw = re.sub(r"```[a-z]*\n?", "", raw).strip()
        result = json.loads(raw)
        if isinstance(result, dict) and "entities" in result:
            return result
        return {"entities": [], "relationships": []}
    except json.JSONDecodeError:
        return {"entities": [], "relationships": []}


def _contradiction_prompt(fact_a: str, fact_b: str) -> str:
    """Build prompt for pairwise contradiction check."""
    return (
        "Compare these two facts about the same user. Reply with exactly one word:\n"
        "CONTRADICT — if they cannot both be true\n"
        "CONSISTENT — if they are compatible\n"
        "UNRELATED — if they are about different things\n\n"
        f"Fact A (existing): {fact_a}\n"
        f"Fact B (new): {fact_b}\n\n"
        "Reply with one word only:"
    )


def detect_contradictions(db: sqlite3.Connection, new_atom_id: int,
                          new_atom_text: str, new_atom_vec: list[float]) -> list[dict]:
    """Check new atom against similar existing atoms for contradictions.

    Returns list of conflicts found. Invalidates contradicted atoms via Phase 1's
    invalidate_atom(). Caps at 5 LLM calls. Skips atoms with L2 distance > 1.2.
    """
    conflicts: list[dict] = []
    try:
        rows = db.execute(
            """
            SELECT e.id, e.chunk, v.distance
            FROM embeddings v
            JOIN entries e ON e.id = v.rowid
            WHERE e.type = 'atom'
              AND (e.expired_at IS NULL OR e.expired_at > date('now'))
              AND e.id != ?
              AND v.embedding MATCH ?
              AND k = 10
            ORDER BY v.distance
            """,
            [new_atom_id, serialize(new_atom_vec)],
        ).fetchall()
    except Exception:
        return []

    llm_calls = 0
    consecutive_failures = 0
    for existing_id, existing_text, distance in rows:
        if distance > 1.2 or llm_calls >= 5 or consecutive_failures >= 3:
            break
        try:
            prompt = _contradiction_prompt(existing_text, new_atom_text)
            response = _generate_with_fallback(
                prompt,
                config=genai_types.GenerateContentConfig(temperature=0.0, max_output_tokens=10),
                label="detect_contradictions",
            )
            if response is None:
                # All models quota-exhausted — stop checking further pairs.
                break
            llm_calls += 1
            consecutive_failures = 0
            verdict = response.text.strip().upper().split()[0] if response.text else ""
            if verdict == "CONTRADICT":
                conflicts.append({"older_id": existing_id, "newer_id": new_atom_id,
                                  "older_text": existing_text})
                # Log to pending_conflicts for user review — never auto-invalidate
                today = local_now().strftime("%Y-%m-%d")
                try:
                    db.execute(
                        "INSERT OR IGNORE INTO pending_conflicts "
                        "(older_id, newer_id, older_text, newer_text, created_at) "
                        "VALUES (?, ?, ?, ?, ?)",
                        [existing_id, new_atom_id, existing_text, new_atom_text, today],
                    )
                except Exception:
                    pass
                print(f"  CONFLICT DETECTED (pending review): atom {existing_id} "
                      f"may be superseded by {new_atom_id} ({existing_text[:60]})")
        except Exception as e:
            consecutive_failures += 1
            print(f"  WARN: contradiction check failed ({consecutive_failures}/3): {e}", file=sys.stderr)
            continue

    return conflicts


def cmd_wander_graph(seeds: list[str], steps: int = 3, top_k: int = 10):
    """Spreading activation over the entity graph with fan-effect + lateral inhibition."""
    import math
    db = open_db()

    # Resolve seeds to entity ids
    all_entities = db.execute("SELECT id, name FROM entities").fetchall()
    if not all_entities:
        print("No entities in graph yet. Run --extract on some session logs first.")
        return

    entity_names = {row[1]: row[0] for row in all_entities}
    resolved: list[tuple[int, str]] = []
    for seed in seeds:
        s = seed.lower()
        if s in entity_names:
            resolved.append((entity_names[s], s))
        else:
            for name, eid in entity_names.items():
                if s in name or name in s:
                    resolved.append((eid, name))

    if not resolved:
        # Use entities from most recent atoms
        recent = db.execute(
            "SELECT ae.entity_id, ent.name FROM atom_entities ae "
            "JOIN entities ent ON ent.id = ae.entity_id "
            "JOIN entries e ON e.id = ae.atom_id "
            "ORDER BY e.date DESC LIMIT 5"
        ).fetchall()
        resolved = [(r[0], r[1]) for r in recent]

    if not resolved:
        print("Could not resolve any seed entities.")
        return

    # Build adjacency from relationships
    edges = db.execute(
        "SELECT source_id, target_id, confidence, evidence_count "
        "FROM relationships WHERE expired_at IS NULL"
    ).fetchall()
    import math
    adjacency: dict[int, list[tuple[int, float]]] = {}
    degree: dict[int, int] = {}
    for src, tgt, conf, ev_count in edges:
        weight = conf * math.log1p(ev_count)
        adjacency.setdefault(src, []).append((tgt, weight))
        adjacency.setdefault(tgt, []).append((src, weight))
        degree[src] = degree.get(src, 0) + 1
        degree[tgt] = degree.get(tgt, 0) + 1

    # Spreading activation — accumulate all discovered entities
    DECAY = 0.7
    frontier: dict[int, float] = {eid: 1.0 for eid, _ in resolved}
    all_activated: dict[int, float] = {}
    visited: set[int] = {eid for eid, _ in resolved}

    for _ in range(steps):
        spread: dict[int, float] = {}
        for node_id, strength in frontier.items():
            neighbors = adjacency.get(node_id, [])
            fan_penalty = math.sqrt(max(degree.get(node_id, 1), 1))
            for neighbor_id, weight in neighbors:
                if neighbor_id not in visited:
                    incoming = strength * DECAY * weight / fan_penalty
                    spread[neighbor_id] = spread.get(neighbor_id, 0.0) + incoming
        if not spread:
            break
        top = sorted(spread.items(), key=lambda x: -x[1])[:top_k]
        frontier = dict(top)
        for eid, strength in frontier.items():
            all_activated[eid] = max(all_activated.get(eid, 0.0), strength)
        visited.update(frontier)
    activation = all_activated

    # Resolve names for output
    id_to_name = {row[0]: row[1] for row in all_entities}

    print(f"## Graph Wander: seeds = [{', '.join(name for _, name in resolved)}]\n")
    if activation:
        print("### Activated Entities\n")
        for eid, strength in sorted(activation.items(), key=lambda x: -x[1])[:top_k]:
            name = id_to_name.get(eid, f"entity-{eid}")
            print(f"- **{name}** ({strength:.2f})")

    # Bridge candidates: activated pairs with no direct edge
    activated_list = [eid for eid, _ in sorted(activation.items(), key=lambda x: -x[1])[:15]]
    direct_edges = {(src, tgt) for src, tgt, _, _ in edges} | {(tgt, src) for src, tgt, _, _ in edges}
    bridges = []
    for i, e1 in enumerate(activated_list):
        for e2 in activated_list[i + 1:]:
            if (e1, e2) not in direct_edges:
                shared = set()
                for n1, _ in adjacency.get(e1, []):
                    for n2, _ in adjacency.get(e2, []):
                        if n1 == n2:
                            shared.add(n1)
                if shared:
                    bridges.append((e1, e2, shared))

    print("\n### Bridge Candidates\n")
    if bridges:
        for e1, e2, via in bridges[:5]:
            n1, n2 = id_to_name.get(e1, "?"), id_to_name.get(e2, "?")
            via_names = [id_to_name.get(v, "?") for v in list(via)[:3]]
            print(f"- **{n1}** ↔ **{n2}**  (via: {', '.join(via_names)})")
    else:
        print("(none)")
    db.close()


# ── Phase 3: Entity articles, compression, query routing ─────────────────────

import hashlib


def _compute_entity_source_hash(db: sqlite3.Connection, entity_id: int) -> str:
    """SHA-256 of sorted atom IDs + sorted edge tuples linked to this entity."""
    atom_ids = sorted(
        r[0] for r in db.execute(
            "SELECT atom_id FROM atom_entities WHERE entity_id = ?", [entity_id]
        ).fetchall()
    )
    edges = sorted(
        (r[0], r[1], r[2]) for r in db.execute(
            "SELECT source_id, target_id, rel_type FROM relationships "
            "WHERE source_id = ? OR target_id = ?", [entity_id, entity_id]
        ).fetchall()
    )
    blob = json.dumps({"atoms": atom_ids, "edges": edges}).encode()
    return hashlib.sha256(blob).hexdigest()


def _entity_article_prompt(entity: dict, relationships: list[dict], atoms: list[dict]) -> str:
    """Build Gemini prompt for entity article generation."""
    lines = [
        f"Write a concise knowledge article about \"{entity['name']}\" (type: {entity['entity_type']}).",
        "Synthesize the relationships and facts below into a coherent summary.",
        "Use markdown. Be factual and concise. No preamble.",
        "",
        "Relationships:",
    ]
    for rel in relationships:
        lines.append(f"- {rel['source']} --{rel['rel_type']}--> {rel['target']}")
    lines.append("")
    lines.append("Known facts:")
    for atom in atoms:
        lines.append(f"- {atom['text']}")
    return "\n".join(lines)


def generate_entity_article(db: sqlite3.Connection, entity_id: int) -> Path:
    """Generate a markdown article for an entity from its graph context."""
    VAULT_ENTITIES.mkdir(parents=True, exist_ok=True)

    entity_row = db.execute(
        "SELECT name, entity_type, domain, summary FROM entities WHERE id = ?", [entity_id]
    ).fetchone()
    if not entity_row:
        raise ValueError(f"entity {entity_id} not found")
    entity = {"name": entity_row[0], "entity_type": entity_row[1],
              "domain": entity_row[2], "summary": entity_row[3]}

    # Gather relationships (both directions)
    rels_raw = db.execute(
        "SELECT r.source_id, r.target_id, r.rel_type, es.name, et.name "
        "FROM relationships r "
        "JOIN entities es ON es.id = r.source_id "
        "JOIN entities et ON et.id = r.target_id "
        "WHERE (r.source_id = ? OR r.target_id = ?) AND r.expired_at IS NULL",
        [entity_id, entity_id]
    ).fetchall()
    relationships = [
        {"source": r[3], "target": r[4], "rel_type": r[2]} for r in rels_raw
    ]

    # Gather linked atoms
    atoms_raw = db.execute(
        "SELECT e.chunk FROM entries e "
        "JOIN atom_entities ae ON ae.atom_id = e.id "
        "WHERE ae.entity_id = ? AND e.type = 'atom' "
        "AND (e.expired_at IS NULL OR e.expired_at > date('now')) AND e.orphaned_at IS NULL",
        [entity_id]
    ).fetchall()
    atoms = [{"text": r[0]} for r in atoms_raw]

    prompt = _entity_article_prompt(entity, relationships, atoms)

    article_text = ""
    try:
        response = _generate_with_fallback(
            prompt,
            config=genai_types.GenerateContentConfig(temperature=0.3, max_output_tokens=2048),
            label="generate_entity_article",
        )
    except Exception as e:
        print(f"  WARN: article generation error: {e}", file=sys.stderr)
        return Path()
    if response is None:
        print("  WARN: all models quota-exhausted for article generation", file=sys.stderr)
        return Path()
    article_text = response.text.strip()
    if not article_text:
        return Path()

    slug = slugify(entity["name"])
    path = VAULT_ENTITIES / f"{slug}.md"
    today = local_now().strftime("%Y-%m-%d")
    source_hash = _compute_entity_source_hash(db, entity_id)

    path.write_text(
        f"---\ntype: entity-article\nentity: {entity['name']}\n"
        f"entity_type: {entity['entity_type']}\ngenerated_at: {today}\n---\n\n"
        f"{article_text}\n"
    )

    db.execute(
        "INSERT INTO entity_articles (entity_id, vault_path, generated_at, source_hash) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(entity_id) DO UPDATE SET vault_path=excluded.vault_path, "
        "generated_at=excluded.generated_at, source_hash=excluded.source_hash",
        [entity_id, str(path), today, source_hash]
    )
    db.commit()
    return path


def cmd_compile(entity_name: str | None = None, threshold: int = 3) -> None:
    """Generate entity articles. Auto-mode: entities with mention_count >= threshold."""
    db = open_db()
    if entity_name:
        row = db.execute(
            "SELECT id, name FROM entities WHERE name = ?", [entity_name.strip().lower()]
        ).fetchone()
        if not row:
            print(f"Entity not found: {entity_name}", file=sys.stderr)
            db.close()
            return
        path = generate_entity_article(db, row[0])
        if path and path.exists():
            print(f"  compiled: {row[1]} → {path.name}")
        db.close()
        return

    # Auto mode: select eligible entities
    entities = db.execute(
        "SELECT id, name, mention_count FROM entities WHERE mention_count >= ?",
        [threshold]
    ).fetchall()
    compiled, skipped = 0, 0
    for eid, name, mc in entities:
        new_hash = _compute_entity_source_hash(db, eid)
        existing = db.execute(
            "SELECT source_hash FROM entity_articles WHERE entity_id = ?", [eid]
        ).fetchone()
        if existing and existing[0] == new_hash:
            skipped += 1
            continue
        path = generate_entity_article(db, eid)
        if path and path.exists():
            compiled += 1
            print(f"  compiled: {name} → {path.name}")
    print(f"Compiled {compiled} articles ({skipped} fresh, skipped)")
    db.close()


def _get_period_key(date_str: str, level: str) -> str:
    """Convert a date string to a period key. '2024-06-15' + 'weekly' → '2024-W24'."""
    from datetime import date as _date
    d = _date.fromisoformat(date_str)
    if level == "monthly":
        return f"{d.year}-{d.month:02d}"
    # weekly: ISO week
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def compress_period(db: sqlite3.Connection, level: str, period_key: str) -> str:
    """Generate a digest for a time period. One Gemini Flash call."""
    # Collect sessions in this period
    all_entries = db.execute(
        "SELECT date, tldr, decisions FROM entries WHERE type IN ('frontmatter', 'session') AND date IS NOT NULL AND orphaned_at IS NULL"
    ).fetchall()
    matching = [
        (date, tldr, decisions) for date, tldr, decisions in all_entries
        if _get_period_key(date, level) == period_key
    ]
    if not matching:
        return ""

    prompt_lines = [
        f"Summarize this {level} period ({period_key}) of activity into a concise digest.",
        "Focus on decisions, outcomes, and themes. Use markdown bullets. Be concise.",
        ""
    ]
    for date, tldr, decisions in matching:
        prompt_lines.append(f"- [{date}] {tldr or ''}")
        if decisions:
            prompt_lines.append(f"  Decisions: {decisions}")

    prompt = "\n".join(prompt_lines)
    digest_text = ""
    try:
        response = _generate_with_fallback(
            prompt,
            config=genai_types.GenerateContentConfig(temperature=0.3, max_output_tokens=1024),
            label="compress_period",
        )
    except Exception as e:
        print(f"  WARN: digest generation error: {e}", file=sys.stderr)
        return ""
    if response is None:
        return ""
    digest_text = response.text.strip()
    if not digest_text:
        return ""

    today = local_now().strftime("%Y-%m-%d")
    db.execute(
        "INSERT INTO digests (level, period_key, content, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(level, period_key) DO UPDATE SET content=excluded.content, created_at=excluded.created_at",
        [level, period_key, digest_text, today]
    )
    db.commit()
    return digest_text


def cmd_compress_digests(level: str = "weekly") -> None:
    """Generate digests for periods that don't have one yet."""
    db = open_db()
    # Find all period keys present in entries
    all_entries = db.execute(
        "SELECT DISTINCT date FROM entries WHERE date IS NOT NULL AND type IN ('frontmatter', 'session') AND orphaned_at IS NULL"
    ).fetchall()
    all_periods = {_get_period_key(r[0], level) for r in all_entries if r[0]}

    # Find existing digests
    existing = {
        r[0] for r in db.execute(
            "SELECT period_key FROM digests WHERE level = ?", [level]
        ).fetchall()
    }

    missing = sorted(all_periods - existing)
    if not missing:
        print(f"All {level} digests up to date ({len(existing)} exist)")
        db.close()
        return

    generated = 0
    for period_key in missing:
        content = compress_period(db, level, period_key)
        if content:
            generated += 1
            print(f"  digest: {period_key}")
    print(f"Generated {generated} {level} digests ({len(existing)} already existed)")
    db.close()


def classify_query_intent(query: str) -> str:
    """Classify query intent via keyword heuristics. Returns factual/temporal/exhaustive/exploratory."""
    q = query.lower()
    if re.search(r'\b(what did we decide|what was|who is|define|what is)\b', q):
        return "factual"
    if re.search(r'\b(how has .* evolved|over time|since|progression|history of|timeline)\b', q):
        return "temporal"
    if re.search(r'\b(everything about|all .* on|complete picture|deep dive|comprehensive)\b', q):
        return "exhaustive"
    return "exploratory"


# ── Phase 4: Forgetting curves, cross-domain synthesis, privacy ──────────────

import math


def log_access(db: sqlite3.Connection, entry_id: int, access_type: str):
    """Record an access event for temperature computation."""
    now = utc_now().strftime("%Y-%m-%dT%H:%M:%S")
    db.execute(
        "INSERT INTO access_log (entry_id, accessed_at, access_type) VALUES (?, ?, ?)",
        [entry_id, now, access_type],
    )
    db.commit()


def log_query(db: sqlite3.Connection, query: str, intent: str,
              result_count: int = 0, atom_hit: int = 0):
    """Record a query for blind-spot analysis."""
    now = utc_now().strftime("%Y-%m-%dT%H:%M:%S")
    db.execute(
        "INSERT INTO query_log (query_text, intent, result_count, atom_hit, queried_at) "
        "VALUES (?, ?, ?, ?, ?)",
        [query, intent, result_count, atom_hit, now],
    )
    db.commit()


def compute_temperature(db: sqlite3.Connection, entry_id: int,
                        lambda_decay: float = 0.03) -> float:
    """Compute forgetting curve temperature: sum(exp(-lambda * days_since_access)).
    Higher = hotter (more recently/frequently accessed)."""
    from datetime import date as _date
    today = _date.today()
    accesses = db.execute(
        "SELECT accessed_at FROM access_log WHERE entry_id = ?", [entry_id]
    ).fetchall()
    if not accesses:
        # Baseline: atoms with no access history get a neutral temperature (0.5)
        # instead of 0.0, so they aren't permanently bottom-ranked.
        # They'll decay naturally once access_log entries start accumulating.
        return 0.5
    total = 0.0
    for (accessed_at,) in accesses:
        try:
            access_date = _date.fromisoformat(accessed_at[:10])
            days = (today - access_date).days
            total += math.exp(-lambda_decay * days)
        except (ValueError, TypeError):
            pass
    return round(total, 4)


def cmd_decay(dry_run: bool = False):
    """Recompute temperature for all live atoms."""
    db = open_db()
    atoms = db.execute(
        "SELECT id, path FROM entries WHERE type = 'atom' AND (expired_at IS NULL OR expired_at > date('now')) AND orphaned_at IS NULL"
    ).fetchall()
    hot, warm, cold = 0, 0, 0
    for entry_id, path in atoms:
        temp = compute_temperature(db, entry_id)
        if temp >= 0.5:
            hot += 1
        elif temp >= 0.1:
            warm += 1
        else:
            cold += 1
        if not dry_run:
            db.execute("UPDATE entries SET temperature = ? WHERE id = ?", [temp, entry_id])
    if not dry_run:
        db.commit()
    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}Temperature: {hot} hot (≥0.5), {warm} warm (0.1-0.5), {cold} cold (<0.1)")
    print(f"{prefix}Total: {len(atoms)} atoms")
    db.close()


VALID_PRIVACY_LEVELS = {"public", "internal", "private", "sensitive"}


def _resolve_privacy_allowlist(explicit: list[str] | None = None) -> list[str] | None:
    """Resolve the effective privacy allowlist from explicit arg or env var.

    Priority: explicit arg > DEUS_MEMORY_PRIVACY env var > None (caller uses default).
    Invalid levels are silently stripped. Empty result returns None.

    Note: DEUS_MEMORY_PRIVACY is injected by container-runner from validated
    /settings config. A container agent with shell access could unset it,
    falling back to the default (exclude sensitive only). This is acceptable
    given the semi-trusted agent threat model.
    """
    if explicit:
        return [p for p in explicit if p in VALID_PRIVACY_LEVELS] or None
    raw = os.environ.get("DEUS_MEMORY_PRIVACY", "")
    levels = [p.strip() for p in raw.split(",") if p.strip()]
    validated = [p for p in levels if p in VALID_PRIVACY_LEVELS]
    return validated or None


def _parse_allowed_privacy_arg(raw: str | None) -> list[str] | None:
    """Parse --allowed-privacy CLI value into a validated list."""
    if not raw:
        return None
    return [p.strip() for p in raw.split(",") if p.strip()] or None


PRIVACY_KEYWORDS: dict[str, list[str]] = {
    "sensitive": ["password", "token", "secret", "api_key", "credential", "ssn",
                  "credit card", "bank account", "social security", "trade", "stock",
                  "portfolio", "position", "strike", "option", "earnings"],
    "private": ["family", "friend", "relationship", "health", "medical", "personal",
                "roommate", "birthday", "mood", "diary", "journal"],
}


def classify_privacy(text: str, domain: str = "general") -> str:
    """Classify atom privacy level via keyword heuristics."""
    lower = text.lower()
    # PII patterns
    if re.search(r'\b\d{3}[-.]?\d{2}[-.]?\d{4}\b', lower):  # SSN-like
        return "sensitive"
    if re.search(r'\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b', lower):  # email
        return "sensitive"
    # Keyword matching
    for word in PRIVACY_KEYWORDS["sensitive"]:
        if word in lower:
            return "sensitive"
    if domain == "trading":
        return "sensitive"
    if domain == "personal":
        return "private"
    for word in PRIVACY_KEYWORDS["private"]:
        if word in lower:
            return "private"
    if domain == "study":
        return "public"
    return "internal"


def find_cross_domain_bridges(db: sqlite3.Connection,
                              min_activation: float = 0.3) -> list[dict]:
    """Find entity pairs in different domains with shared graph neighbors."""
    entities = db.execute(
        "SELECT id, name, domain FROM entities WHERE domain IS NOT NULL"
    ).fetchall()
    if len(entities) < 2:
        return []

    # Build adjacency from relationships
    adjacency: dict[int, set[int]] = {}
    edges = db.execute(
        "SELECT source_id, target_id FROM relationships WHERE expired_at IS NULL"
    ).fetchall()
    for src, tgt in edges:
        adjacency.setdefault(src, set()).add(tgt)
        adjacency.setdefault(tgt, set()).add(src)

    id_to_info = {eid: (name, domain) for eid, name, domain in entities}
    bridges = []
    seen_pairs: set[tuple[int, int]] = set()

    for e1_id, e1_name, e1_domain in entities:
        for e2_id, e2_name, e2_domain in entities:
            if e1_id >= e2_id or e1_domain == e2_domain:
                continue
            pair = (e1_id, e2_id)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            # Find shared neighbors
            n1 = adjacency.get(e1_id, set())
            n2 = adjacency.get(e2_id, set())
            shared = n1 & n2
            if shared:
                bridges.append({
                    "entity_a": e1_name, "entity_a_id": e1_id, "domain_a": e1_domain,
                    "entity_b": e2_name, "entity_b_id": e2_id, "domain_b": e2_domain,
                    "shared_neighbors": [id_to_info.get(n, (f"entity-{n}", None))[0] for n in shared],
                })
    return bridges


def generate_synthesis(db: sqlite3.Connection, entity_a_id: int,
                       entity_b_id: int) -> str:
    """Generate a cross-domain synthesis suggestion. Cached in synthesis_suggestions."""
    # Check cache
    existing = db.execute(
        "SELECT bridge_text FROM synthesis_suggestions "
        "WHERE entity_a_id = ? AND entity_b_id = ? AND dismissed = 0",
        [entity_a_id, entity_b_id]
    ).fetchone()
    if existing:
        return existing[0]

    a = db.execute("SELECT name, entity_type, domain FROM entities WHERE id = ?", [entity_a_id]).fetchone()
    b = db.execute("SELECT name, entity_type, domain FROM entities WHERE id = ?", [entity_b_id]).fetchone()
    if not a or not b:
        return ""

    prompt = (
        f"These two concepts come from different domains but share connections:\n"
        f"- {a[0]} ({a[1]}, domain: {a[2]})\n"
        f"- {b[0]} ({b[1]}, domain: {b[2]})\n\n"
        f"In 2-3 sentences, suggest how insights from one domain might apply to the other. "
        f"Be specific and actionable."
    )

    synthesis_text = ""
    try:
        response = _generate_with_fallback(
            prompt,
            config=genai_types.GenerateContentConfig(temperature=0.5, max_output_tokens=512),
            label="generate_synthesis",
        )
    except Exception as e:
        print(f"  WARN: synthesis error: {e}", file=sys.stderr)
        return ""
    if response is None:
        return ""
    synthesis_text = response.text.strip()
    if not synthesis_text:
        return ""

    today = local_now().strftime("%Y-%m-%d")
    db.execute(
        "INSERT INTO synthesis_suggestions (entity_a_id, entity_b_id, bridge_text, created_at) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(entity_a_id, entity_b_id) DO UPDATE SET bridge_text=excluded.bridge_text, "
        "created_at=excluded.created_at, dismissed=0",
        [entity_a_id, entity_b_id, synthesis_text, today]
    )
    db.commit()
    return synthesis_text


def cmd_synthesize(top: int = 3):
    """Find and display cross-domain synthesis suggestions."""
    db = open_db()
    bridges = find_cross_domain_bridges(db)
    if not bridges:
        print("No cross-domain bridges found (need entities in different domains with shared neighbors)")
        db.close()
        return

    print(f"## Cross-Domain Synthesis ({len(bridges)} bridge(s) found)\n")
    for bridge in bridges[:top]:
        text = generate_synthesis(db, bridge["entity_a_id"], bridge["entity_b_id"])
        if text:
            print(f"### {bridge['entity_a']} ({bridge['domain_a']}) ↔ {bridge['entity_b']} ({bridge['domain_b']})")
            print(f"  via: {', '.join(bridge['shared_neighbors'][:3])}")
            print(f"  {text}\n")
    db.close()


def cmd_blind_spots(top: int = 10):
    """Enhanced gap analysis: topic frequency + query log misses + entity orphans."""
    db = open_db()
    gaps: list[tuple[str, str, int]] = []  # (name, source, score)

    # 1. Topic-based gaps (from cmd_gaps logic)
    if VAULT_SESSION_LOGS.exists():
        session_topic_count: dict[str, int] = {}
        for log_file in VAULT_SESSION_LOGS.rglob("*.md"):
            if ".obsidian" in str(log_file):
                continue
            fm = extract_frontmatter(log_file.read_text(encoding="utf-8"))
            if not fm.get("topics"):
                continue
            for topic in fm["topics"].split(","):
                t = topic.strip().lower()
                if t:
                    session_topic_count[t] = session_topic_count.get(t, 0) + 1

        atom_coverage: dict[str, int] = {}
        if VAULT_ATOMS.exists():
            for atom_file in VAULT_ATOMS.glob("*.md"):
                body = atom_file.read_text(encoding="utf-8").lower()
                for topic in session_topic_count:
                    if topic in body:
                        atom_coverage[topic] = atom_coverage.get(topic, 0) + 1

        for topic, count in session_topic_count.items():
            if count >= 3 and atom_coverage.get(topic, 0) <= 1:
                gaps.append((topic, "topic-gap", count))

    # 2. Query misses (queries with 0 atom hits)
    try:
        misses = db.execute(
            "SELECT query_text, COUNT(*) as cnt FROM query_log "
            "WHERE atom_hit = 0 GROUP BY query_text ORDER BY cnt DESC LIMIT ?"
        , [top]).fetchall()
        for query_text, cnt in misses:
            gaps.append((query_text[:50], "query-miss", cnt))
    except sqlite3.OperationalError:
        pass

    # 3. Entity orphans (entities with no linked atoms)
    try:
        orphans = db.execute(
            "SELECT e.name FROM entities e "
            "WHERE NOT EXISTS (SELECT 1 FROM atom_entities ae WHERE ae.entity_id = e.id) "
            "AND e.mention_count >= 2"
        ).fetchall()
        for (name,) in orphans:
            gaps.append((name, "entity-orphan", 1))
    except sqlite3.OperationalError:
        pass

    gaps.sort(key=lambda x: -x[2])
    print("## Blind Spots")
    if gaps:
        for name, source, score in gaps[:top]:
            print(f"- [{source}] {name} (score: {score})")
    else:
        print("- No blind spots detected")
    db.close()


def cmd_resolve_conflicts():
    """Show pending contradictions for user review. Does NOT auto-invalidate."""
    db = open_db()
    try:
        conflicts = db.execute(
            "SELECT id, older_id, newer_id, older_text, newer_text, created_at "
            "FROM pending_conflicts WHERE resolved = 0"
        ).fetchall()
    except sqlite3.OperationalError:
        print("No pending_conflicts table. Nothing to review.")
        db.close()
        return

    if not conflicts:
        print("No pending conflicts to review.")
        db.close()
        return

    print(f"## {len(conflicts)} Pending Conflict(s)\n")
    for cid, older_id, newer_id, older_text, newer_text, created_at in conflicts:
        print(f"### Conflict #{cid} (detected {created_at})")
        print(f"  OLDER (atom {older_id}): {older_text[:120]}")
        print(f"  NEWER (atom {newer_id}): {newer_text[:120]}")
        print(f"  → To invalidate older: --invalidate-conflict {cid}")
        print(f"  → To dismiss:          --dismiss-conflict {cid}")
        print()
    db.close()


def cmd_invalidate_conflict(conflict_id: int):
    """Invalidate the older atom in a conflict after user review."""
    db = open_db()
    row = db.execute(
        "SELECT older_id, newer_id FROM pending_conflicts WHERE id = ? AND resolved = 0",
        [conflict_id],
    ).fetchone()
    if not row:
        print(f"Conflict #{conflict_id} not found or already resolved.")
        db.close()
        return
    older_id, newer_id = row
    invalidate_atom(db, older_id, reason=f"superseded by atom {newer_id} (user-confirmed)")
    db.execute(
        "UPDATE pending_conflicts SET resolved = 1, resolution = 'invalidated' WHERE id = ?",
        [conflict_id],
    )
    db.commit()
    db.close()


def cmd_dismiss_conflict(conflict_id: int):
    """Dismiss a false-positive conflict."""
    db = open_db()
    db.execute(
        "UPDATE pending_conflicts SET resolved = 1, resolution = 'dismissed' WHERE id = ?",
        [conflict_id],
    )
    db.commit()
    print(f"Conflict #{conflict_id} dismissed.")
    db.close()


def cmd_export(output_path: str, privacy_levels: list[str] | None = None):
    """Export atoms filtered by privacy to standalone markdown."""
    db = open_db()
    levels = _resolve_privacy_allowlist(privacy_levels) or ["public", "internal"]

    placeholders = ",".join("?" * len(levels))
    atoms = db.execute(
        f"SELECT path, chunk, confidence, domain, privacy FROM entries "
        f"WHERE type = 'atom' AND (expired_at IS NULL OR expired_at > date('now')) "
        f"AND orphaned_at IS NULL AND privacy IN ({placeholders})",
        levels,
    ).fetchall()

    out_path = Path(output_path).expanduser()
    lines = [f"# Deus Knowledge Export\n", f"Privacy levels: {', '.join(levels)}\n",
             f"Exported: {local_now().strftime('%Y-%m-%d')}\n", f"Atoms: {len(atoms)}\n", ""]
    for path, chunk, confidence, domain, privacy in atoms:
        lines.append(f"- [{domain}/{privacy}] ({confidence:.2f}) {chunk}")

    out_path.write_text("\n".join(lines))
    print(f"Exported {len(atoms)} atoms to {out_path}")
    db.close()


def write_atom_file(atom: dict, source_path: str, today: str,
                    source_excerpt: str = "", domain: str = "general",
                    privacy: str = "internal") -> Path:
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
        f"confidence: {conf:.2f}\ncorroborations: 1\ndomain: {domain}\nprivacy: {privacy}\n"
        f"source: {source_path}\ncreated_at: {today}\nupdated_at: {today}\n{ttl_line}\n"
        f"{excerpt_lines}---\n"
        f"{atom['text']}\n"
    )
    return path


def cmd_extract(session_path: str, no_contradict: bool = False):
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
    today = local_now().strftime("%Y-%m-%d")
    new_count, corroborated_count = 0, 0
    new_atom_ids: list[tuple[int, str, list[float]]] = []  # (entry_id, text, vec)

    # Load existing atom texts for cheap text-equality dedup before embedding
    existing_texts = {
        r[0].strip().lower()
        for r in db.execute("SELECT chunk FROM entries WHERE type = 'atom' AND orphaned_at IS NULL").fetchall()
    }

    for atom in atoms:
        text_lower = atom["text"].strip().lower()

        # 1. Text equality check — free, no API call
        if text_lower in existing_texts:
            row = db.execute(
                "SELECT id FROM entries WHERE type = 'atom' AND orphaned_at IS NULL AND lower(chunk) = ? LIMIT 1",
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
            privacy = classify_privacy(atom["text"], domain)
            conf = CONFIDENCE_PRIOR.get(cat, 0.50)
            atom_path = write_atom_file(atom, str(path), today, source_excerpt, domain=domain, privacy=privacy)
            cur = db.execute(
                "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, source_chunk, domain, privacy, category) "
                "VALUES (?, ?, ?, 'atom', ?, '', ?, 1, ?, ?, ?, ?)",
                [str(atom_path), today, atom["text"], atom["text"], conf, source_excerpt, domain, privacy, atom.get("category", "fact")],
            )
            db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
                       [cur.lastrowid, serialize(vec)])
            new_atom_ids.append((cur.lastrowid, atom["text"], vec))
            new_count += 1
            print(f"  new atom [{domain}]: {atom['text'][:70]}")

    db.commit()
    print(f"Extracted {new_count + corroborated_count} atoms ({new_count} new, {corroborated_count} corroborated)")

    # Phase 2: entity/relationship extraction + contradiction detection
    try:
        ent_rel = extract_entities_and_relations(content)
        entities = ent_rel.get("entities", [])
        relationships = ent_rel.get("relationships", [])

        entity_refs: list[tuple[str, str]] = []
        for ent in entities:
            if isinstance(ent, dict) and "name" in ent and "entity_type" in ent:
                domain = classify_domain(ent.get("summary", ent["name"]))
                upsert_entity(db, ent["name"], ent["entity_type"], domain, today)
                entity_refs.append((ent["name"], ent["entity_type"]))

        for rel in relationships:
            if isinstance(rel, dict) and "source" in rel and "target" in rel and "rel_type" in rel:
                src_row = db.execute(
                    "SELECT id FROM entities WHERE name = ?", [rel["source"].strip().lower()]
                ).fetchone()
                tgt_row = db.execute(
                    "SELECT id FROM entities WHERE name = ?", [rel["target"].strip().lower()]
                ).fetchone()
                if src_row and tgt_row:
                    upsert_relationship(db, src_row[0], tgt_row[0], rel["rel_type"],
                                        rel.get("confidence", 0.5), today)

        # Link new atoms to entities
        for atom_id, atom_text, _ in new_atom_ids:
            link_atom_entities(db, atom_id, entity_refs)

        db.commit()
        if entities:
            print(f"  graph: {len(entities)} entities, {len(relationships)} relationships")
    except Exception as e:
        print(f"  WARN: entity extraction failed: {e}", file=sys.stderr)

    # Contradiction detection for new atoms
    if not no_contradict and new_atom_ids:
        try:
            total_conflicts = 0
            for atom_id, atom_text, atom_vec in new_atom_ids:
                conflicts = detect_contradictions(db, atom_id, atom_text, atom_vec)
                total_conflicts += len(conflicts)
            if total_conflicts:
                print(f"  contradictions: {total_conflicts} conflict(s) logged for review (use --resolve-conflicts)")
        except Exception as e:
            print(f"  WARN: contradiction detection failed: {e}", file=sys.stderr)


def cmd_rebuild():
    if not VAULT_SESSION_LOGS.exists():
        print(f"ERROR: session logs not found at {VAULT_SESSION_LOGS}", file=sys.stderr)
        sys.exit(1)

    # Tables that CAN be rebuilt from disk (session logs + atom .md files):
    rebuildable_tables = ["entries", "embeddings", "entries_fts", "entities",
                          "relationships", "atom_entities"]
    # Tables with NO disk source — runtime data that must be preserved:
    # access_log, query_log, entity_articles, digests, synthesis_suggestions, pending_conflicts

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

        # Always back up before rebuild
        import shutil
        backup_path = DB_PATH.with_suffix(f".bak-{local_now().strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(DB_PATH, backup_path)
        print(f"Backed up to {backup_path}")

        # Soft-delete entries; clear derived tables (see ADR: no-db-deletion.md)
        db = open_db()
        now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
        db.execute(
            "UPDATE entries SET orphaned_at = ?, orphan_reason = ? WHERE orphaned_at IS NULL",
            [now, "rebuild"],
        )
        # Derived tables (no primary user data) — safe to clear during rebuild
        for table in ["embeddings", "entries_fts", "entities", "relationships", "atom_entities"]:
            try:
                # safe: `table` iterates the literal list above; SQLite cannot
                # parameterize identifiers in DELETE FROM. The `[...]` quoting
                # is belt-and-suspenders on an already-fixed schema-name set.
                db.execute(f"DELETE FROM [{table}]")
            except sqlite3.OperationalError:
                pass
        db.commit()
        db.close()

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
                    "SELECT id FROM entries WHERE path = ? AND orphaned_at IS NULL LIMIT 1", [str(af)]
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
                atom_category = "fact"
                atom_expired_at = None
                atom_expired_reason = None
                atom_privacy = "internal"
                atom_temperature = 1.0
                for line in raw.splitlines():
                    if line.startswith("domain:"):
                        atom_domain = line.split(":", 1)[1].strip()
                    elif line.startswith("category:"):
                        val = line.split(":", 1)[1].strip()
                        if val:
                            atom_category = val
                    elif line.startswith("expired_at:"):
                        val = line.split(":", 1)[1].strip()
                        if val and val != "null":
                            atom_expired_at = val
                    elif line.startswith("expired_reason:"):
                        atom_expired_reason = line.split(":", 1)[1].strip() or None
                    elif line.startswith("privacy:"):
                        val = line.split(":", 1)[1].strip()
                        if val in VALID_PRIVACY_LEVELS:
                            atom_privacy = val
                    elif line.startswith("temperature:"):
                        try:
                            atom_temperature = float(line.split(":", 1)[1].strip())
                        except (ValueError, TypeError):
                            pass
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
                    "INSERT INTO entries (path, date, chunk, type, tldr, topics, confidence, corroborations, source_chunk, domain, expired_at, expired_reason, privacy, temperature, category) "
                    "VALUES (?, ?, ?, 'atom', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [str(af), date_str, body, body, fm.get("tags", ""), conf, corr, stored_excerpt, atom_domain, atom_expired_at, atom_expired_reason, atom_privacy, atom_temperature, atom_category],
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
            "FROM entries WHERE type='atom' AND orphaned_at IS NULL"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = db.execute(
            "SELECT path, confidence, corroborations, NULL, NULL, 'general' FROM entries WHERE type='atom' AND orphaned_at IS NULL"
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
    # Phase 2: graph metrics
    try:
        snapshot["entities"] = db.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        rels = db.execute(
            "SELECT COUNT(*) FILTER (WHERE expired_at IS NULL), "
            "COUNT(*) FILTER (WHERE expired_at IS NOT NULL) FROM relationships"
        ).fetchall()
        snapshot["relationships_live"] = rels[0][0] if rels else 0
        snapshot["relationships_expired"] = rels[0][1] if rels else 0
    except sqlite3.OperationalError:
        snapshot.update({"entities": 0, "relationships_live": 0, "relationships_expired": 0})
    # Phase 3: article + digest metrics
    try:
        total_articles = db.execute("SELECT COUNT(*) FROM entity_articles").fetchone()[0]
        stale_articles = 0
        for row in db.execute("SELECT entity_id, source_hash FROM entity_articles").fetchall():
            if _compute_entity_source_hash(db, row[0]) != row[1]:
                stale_articles += 1
        snapshot["articles"] = total_articles
        snapshot["articles_stale"] = stale_articles
        digest_counts = db.execute(
            "SELECT level, COUNT(*) FROM digests GROUP BY level"
        ).fetchall()
        snapshot["digests_weekly"] = 0
        snapshot["digests_monthly"] = 0
        for level, cnt in digest_counts:
            snapshot[f"digests_{level}"] = cnt
    except sqlite3.OperationalError:
        snapshot.update({"articles": 0, "articles_stale": 0, "digests_weekly": 0, "digests_monthly": 0})
    # Phase 4: temperature, query success, privacy
    try:
        temp_rows = db.execute(
            "SELECT temperature FROM entries WHERE type = 'atom' AND (expired_at IS NULL OR expired_at > date('now')) AND orphaned_at IS NULL"
        ).fetchall()
        temps = [r[0] if r[0] is not None else 1.0 for r in temp_rows]
        if temps:
            snapshot["temp_hot"] = sum(1 for t in temps if t >= 0.5)
            snapshot["temp_warm"] = sum(1 for t in temps if 0.1 <= t < 0.5)
            snapshot["temp_cold"] = sum(1 for t in temps if t < 0.1)
        else:
            snapshot.update({"temp_hot": 0, "temp_warm": 0, "temp_cold": 0})
    except sqlite3.OperationalError:
        snapshot.update({"temp_hot": 0, "temp_warm": 0, "temp_cold": 0})
    try:
        total_queries = db.execute("SELECT COUNT(*) FROM query_log").fetchone()[0]
        queries_with_hits = db.execute("SELECT COUNT(*) FROM query_log WHERE atom_hit > 0").fetchone()[0]
        snapshot["query_total"] = total_queries
        snapshot["query_success_rate"] = round(queries_with_hits / max(total_queries, 1), 3)
    except sqlite3.OperationalError:
        snapshot.update({"query_total": 0, "query_success_rate": 0.0})
    try:
        privacy_rows = db.execute(
            "SELECT privacy, COUNT(*) FROM entries WHERE type = 'atom' "
            "AND (expired_at IS NULL OR expired_at > date('now')) AND orphaned_at IS NULL GROUP BY privacy"
        ).fetchall()
        snapshot["privacy"] = {p: c for p, c in privacy_rows} if privacy_rows else {}
    except sqlite3.OperationalError:
        snapshot["privacy"] = {}
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
    ent_count = current.get("entities", 0)
    rel_live = current.get("relationships_live", 0)
    rel_expired = current.get("relationships_expired", 0)
    if ent_count > 0 or rel_live > 0:
        out.append(f"Entities: {ent_count}  Relationships: {rel_live} live, {rel_expired} expired")
    articles = current.get("articles", 0)
    articles_stale = current.get("articles_stale", 0)
    if articles > 0:
        stale_pct = round(100 * articles_stale / articles) if articles else 0
        out.append(f"Articles: {articles} ({stale_pct}% stale)")
    dw = current.get("digests_weekly", 0)
    dm = current.get("digests_monthly", 0)
    if dw > 0 or dm > 0:
        out.append(f"Digests: {dw} weekly, {dm} monthly")
    t_hot = current.get("temp_hot", 0)
    t_warm = current.get("temp_warm", 0)
    t_cold = current.get("temp_cold", 0)
    if t_hot + t_warm + t_cold > 0:
        out.append(f"Temperature: {t_hot} hot, {t_warm} warm, {t_cold} cold")
    qt = current.get("query_total", 0)
    qsr = current.get("query_success_rate", 0.0)
    if qt > 0:
        out.append(f"Queries: {qt} total, {100*qsr:.0f}% hit rate")
    priv = current.get("privacy", {})
    if priv:
        out.append("Privacy: " + "  ".join(f"{k}:{v}" for k, v in sorted(priv.items(), key=lambda x: -x[1])))

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
    - DB rows whose atom file no longer exists get orphaned_at set (soft-delete).
    """
    from datetime import date as _date
    db = open_db()
    today = _date.today()

    # 1. TTL enforcement
    ttl_expired = 0
    rows = db.execute(
        "SELECT id, path FROM entries WHERE type = 'atom' AND expired_at IS NULL AND orphaned_at IS NULL"
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

    # 2. Orphan cleanup: DB rows whose file is gone — soft-delete (see ADR: no-db-deletion.md)
    orphans = 0
    now = utc_now().strftime("%Y-%m-%d %H:%M:%S")
    all_atom_rows = db.execute("SELECT id, path FROM entries WHERE type = 'atom' AND orphaned_at IS NULL").fetchall()
    for entry_id, path_str in all_atom_rows:
        if not Path(path_str).exists():
            if dry_run:
                print(f"  [dry-run] would orphan: {Path(path_str).name}")
            else:
                db.execute(
                    "UPDATE entries SET orphaned_at = ?, orphan_reason = ? WHERE id = ?",
                    [now, "file_deleted", entry_id],
                )
            orphans += 1

    if not dry_run:
        db.commit()

    prefix = "[dry-run] " if dry_run else ""
    print(f"{prefix}Prune complete: {ttl_expired} TTL-expired, {orphans} orphans soft-deleted")


def cmd_invalidate(path_str: str, reason: str):
    """Manually invalidate a specific atom by path."""
    db = open_db()
    path = Path(path_str).expanduser().resolve()
    row = db.execute("SELECT id FROM entries WHERE path = ? AND type = 'atom' AND orphaned_at IS NULL LIMIT 1", [str(path)]).fetchone()
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
    group.add_argument(
        "--add-dir",
        metavar="DIR",
        help="Batch-index every .md file under DIR in a single embedding pass "
             "(single HTTP call to Ollama). Used by benchmarks and bulk ingestion.",
    )
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
                       help="Return last N topic-diverse sessions (deduped by primary topic, no API call)")
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
    group.add_argument("--compile", nargs="?", const="__AUTO__", metavar="ENTITY",
                       help="Generate entity articles. No arg = auto mode (mention_count >= 3). "
                            "Arg = compile that specific entity.")
    group.add_argument("--compress-digests", nargs="?", const="weekly",
                       choices=["weekly", "monthly"], metavar="LEVEL",
                       help="Generate period digests (default: weekly)")
    group.add_argument("--decay", action="store_true",
                       help="Recompute forgetting curve temperatures for all atoms (no API call)")
    group.add_argument("--synthesize", action="store_true",
                       help="Cross-domain synthesis suggestions (uses Gemini Flash)")
    group.add_argument("--blind-spots", action="store_true",
                       help="Enhanced gap analysis: topic gaps + query misses + entity orphans (no API call)")
    group.add_argument("--resolve-conflicts", action="store_true",
                       help="Show pending contradictions for review (no auto-invalidation)")
    group.add_argument("--invalidate-conflict", type=int, metavar="ID",
                       help="Confirm and invalidate the older atom in conflict #ID")
    group.add_argument("--dismiss-conflict", type=int, metavar="ID",
                       help="Dismiss conflict #ID as false positive")
    group.add_argument("--export", metavar="PATH",
                       help="Export atoms filtered by --privacy to standalone markdown")
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
    parser.add_argument("--graph", action="store_true",
                        help="Use entity graph instead of topic co-occurrence for --wander")
    parser.add_argument("--no-contradict", action="store_true",
                        help="Skip contradiction detection during --extract")
    parser.add_argument("--as-of", metavar="DATE",
                        help="Temporal filter for --query (YYYY-MM-DD)")
    parser.add_argument("--intent",
                        choices=["factual", "exploratory", "temporal", "exhaustive"],
                        help="Override auto-detected query intent for --query")
    parser.add_argument("--privacy",
                        choices=["public", "internal", "private", "sensitive"],
                        help="Filter --query/--export by single privacy level. "
                             "Sensitive atoms are excluded from --query by default; "
                             "pass --privacy sensitive to see only sensitive atoms.")
    parser.add_argument("--allowed-privacy",
                        help="Comma-separated allowlist of privacy levels for --query "
                             "(e.g. public,internal,private). Overrides --privacy. "
                             "Also reads from DEUS_MEMORY_PRIVACY env var.")
    args = parser.parse_args()

    # Warm up embedding provider before batch workloads when requested.
    # Only fires when the caller sets DEUS_EMBED_WARMUP=1 to avoid adding
    # latency to single-file indexer invocations.
    if os.getenv("DEUS_EMBED_WARMUP") == "1":
        warmup_embedding_provider()

    global _client
    # Commands that need no API key
    if args.wander is not None:
        cmd_wander(args.wander or [], steps=args.steps, top_k=args.top or 10, graph=args.graph)
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
    if args.decay:
        cmd_decay(dry_run=args.dry_run)
        return
    if args.blind_spots:
        cmd_blind_spots(top=args.top)
        return
    if args.resolve_conflicts:
        cmd_resolve_conflicts()
        return
    if args.invalidate_conflict is not None:
        cmd_invalidate_conflict(args.invalidate_conflict)
        return
    if args.dismiss_conflict is not None:
        cmd_dismiss_conflict(args.dismiss_conflict)
        return
    if args.export:
        ap = _parse_allowed_privacy_arg(args.allowed_privacy)
        cmd_export(args.export, privacy_levels=ap or ([args.privacy] if args.privacy else None))
        return

    _client = genai.Client(api_key=load_api_key())

    if args.compile is not None:
        cmd_compile(None if args.compile == "__AUTO__" else args.compile)
        return
    if args.compress_digests is not None:
        cmd_compress_digests(args.compress_digests)
        return
    if args.synthesize:
        cmd_synthesize(top=args.top)
        return

    if args.add:
        cmd_add(args.add, extract=not args.no_extract)
        return
    if args.add_dir:
        cmd_add_dir(args.add_dir, extract=not args.no_extract)
        return
    elif args.query:
        ap = _parse_allowed_privacy_arg(args.allowed_privacy)
        cmd_query(args.query, top=args.top, recency_boost=args.recency_boost,
                  show_source=args.source, domain=args.domain,
                  intent=args.intent, as_of=args.as_of, privacy=args.privacy,
                  allowed_privacy=ap)
    elif args.rebuild:
        cmd_rebuild()
    elif args.extract:
        cmd_extract(args.extract, no_contradict=args.no_contradict)


if __name__ == "__main__":
    main()
