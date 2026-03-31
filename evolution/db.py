"""
Database access for the Evolution loop.
Opens the shared memory database and migrates in the evolution-specific tables
alongside the memory indexer's existing schema.
"""
import sqlite3
import struct
from pathlib import Path
from typing import Optional

import sqlite_vec

from .config import DB_PATH, EMBED_DIM


def open_db() -> sqlite3.Connection:
    """
    Open (or create) the shared Deus SQLite database and ensure all
    evolution tables exist.  Safe to call multiple times; uses IF NOT EXISTS.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.enable_load_extension(True)
    sqlite_vec.load(db)
    db.enable_load_extension(False)
    _migrate(db)
    return db


def _migrate(db: sqlite3.Connection) -> None:
    db.executescript(f"""
        -- Interaction log: one row per agent call
        CREATE TABLE IF NOT EXISTS interactions (
            id            TEXT PRIMARY KEY,
            timestamp     TEXT NOT NULL,
            group_folder  TEXT NOT NULL,
            prompt        TEXT NOT NULL,
            response      TEXT,
            tools_used    TEXT,          -- JSON array of tool names
            latency_ms    REAL,
            judge_score   REAL,          -- null until evaluated
            judge_dims    TEXT,          -- JSON: quality/safety/tool_use/personalization
            eval_suite    TEXT DEFAULT 'runtime',
            session_id    TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_interactions_ts
            ON interactions(timestamp);
        CREATE INDEX IF NOT EXISTS ix_interactions_group
            ON interactions(group_folder);
        CREATE INDEX IF NOT EXISTS ix_interactions_score
            ON interactions(judge_score) WHERE judge_score IS NOT NULL;

        -- Reflexion memory: lessons generated from low-score interactions
        CREATE TABLE IF NOT EXISTS reflections (
            id                  TEXT PRIMARY KEY,
            interaction_id      TEXT REFERENCES interactions(id) ON DELETE CASCADE,
            timestamp           TEXT NOT NULL,
            group_folder        TEXT,     -- NULL = applies cross-group
            content             TEXT NOT NULL,
            category            TEXT,     -- tool_use|reasoning|style|safety
            score_at_gen        REAL,
            times_retrieved     INTEGER DEFAULT 0,
            times_helpful       INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_reflections_group
            ON reflections(group_folder);

        -- Prompt artifacts from DSPy optimizer (versioned)
        CREATE TABLE IF NOT EXISTS prompt_artifacts (
            id              TEXT PRIMARY KEY,
            created_at      TEXT NOT NULL,
            module          TEXT NOT NULL,  -- system_prompt|tool_selection|summarization
            content         TEXT NOT NULL,
            baseline_score  REAL,
            optimized_score REAL,
            sample_count    INTEGER,
            active          INTEGER DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_artifacts_module
            ON prompt_artifacts(module, active);
    """)

    # Reflection embeddings (vec0 virtual table)
    try:
        db.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS reflection_embeddings
            USING vec0(embedding float[{EMBED_DIM}])
        """)
    except Exception:
        pass  # Already exists or vec0 not available

    # Domain presets and user signal columns (added in v1.3)
    for col, coltype in [
        ("domain_presets", "TEXT"),    # JSON array: '["marketing","writing"]'
        ("user_signal", "TEXT"),       # "positive"|"negative"|null
    ]:
        try:
            db.execute(f"ALTER TABLE interactions ADD COLUMN {col} {coltype}")
        except sqlite3.OperationalError:
            pass  # Column already exists

    db.execute("""
        CREATE INDEX IF NOT EXISTS ix_interactions_domain
            ON interactions(domain_presets) WHERE domain_presets IS NOT NULL
    """)

    # Reflection lifecycle: soft-delete archival column (added in v1.5)
    try:
        db.execute("ALTER TABLE reflections ADD COLUMN archived_at TEXT")
    except sqlite3.OperationalError:
        pass  # Column already exists

    db.execute("""
        CREATE INDEX IF NOT EXISTS ix_reflections_stale
            ON reflections(times_retrieved, timestamp)
    """)

    # Principle extraction tracking (added in v1.4)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS principle_extractions (
            id              TEXT PRIMARY KEY,
            domain          TEXT NOT NULL,
            extracted_at    TEXT NOT NULL,
            interaction_count INTEGER,
            principles_count  INTEGER
        );
        CREATE INDEX IF NOT EXISTS ix_principle_extractions_domain_time
            ON principle_extractions(domain, extracted_at);
    """)

    db.commit()


# ── Vector helpers ────────────────────────────────────────────────────────────

def serialize_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def deserialize_vec(buf: bytes) -> list[float]:
    n = len(buf) // 4
    return list(struct.unpack(f"{n}f", buf))
