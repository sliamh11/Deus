"""
SQLite storage provider.

Wraps all database logic from the original evolution/db.py, including
schema migration, sqlite_vec extension loading, and vector operations.
"""
import sqlite3
import struct
from typing import Optional

import sqlite_vec

from ... import config as _config
from ..provider import StorageProvider


def _serialize_vec(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _deserialize_vec(buf: bytes) -> list[float]:
    n = len(buf) // 4
    return list(struct.unpack(f"{n}f", buf))


class SQLiteStorageProvider(StorageProvider):
    """SQLite-backed storage using sqlite-vec for vector operations."""

    def __init__(self, db_path=None):
        self._explicit_db_path = db_path

    @property
    def name(self) -> str:
        return "sqlite"

    @property
    def priority(self) -> int:
        return 10

    def is_available(self) -> bool:
        return True

    @property
    def _db_path(self):
        """Resolve DB path lazily so test monkeypatching works."""
        return self._explicit_db_path or _config.DB_PATH

    # ── Connection management ────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        """Open a connection and ensure schema is migrated."""
        db_path = self._db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        self._migrate(db)
        return db

    def _migrate(self, db: sqlite3.Connection) -> None:
        """Create or update all evolution tables. Safe to call multiple times."""
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
            CREATE INDEX IF NOT EXISTS ix_interactions_session
                ON interactions(session_id) WHERE session_id IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_interactions_group_ts
                ON interactions(group_folder, timestamp);

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
                USING vec0(embedding float[{_config.EMBED_DIM}])
            """)
        except Exception:
            pass  # Already exists or vec0 not available

        # Domain presets, user signal, parse_error columns (added in v1.3+)
        for col, coltype in [
            ("domain_presets", "TEXT"),
            ("user_signal", "TEXT"),
            ("parse_error", "INTEGER DEFAULT 0"),
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
        db.execute("""
            CREATE INDEX IF NOT EXISTS ix_reflections_archived
                ON reflections(archived_at) WHERE archived_at IS NULL
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

    # ── Interaction operations ───────────────────────────────────────────────

    def log_interaction(
        self,
        *,
        prompt: str,
        response: Optional[str],
        group_folder: str,
        timestamp: str,
        interaction_id: str,
        latency_ms: Optional[float] = None,
        tools_used: Optional[str] = None,
        session_id: Optional[str] = None,
        eval_suite: str = "runtime",
        domain_presets: Optional[str] = None,
        user_signal: Optional[str] = None,
    ) -> str:
        db = self._connect()
        db.execute(
            """
            INSERT OR REPLACE INTO interactions
                (id, timestamp, group_folder, prompt, response, tools_used,
                 latency_ms, eval_suite, session_id, domain_presets, user_signal)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction_id, timestamp, group_folder, prompt, response,
                tools_used, latency_ms, eval_suite, session_id,
                domain_presets, user_signal,
            ),
        )
        db.commit()
        db.close()
        return interaction_id

    def update_interaction(self, interaction_id: str, **fields) -> None:
        if not fields:
            return
        db = self._connect()
        set_clause = ", ".join(f"{k} = ?" for k in fields)
        db.execute(
            f"UPDATE interactions SET {set_clause} WHERE id = ?",
            list(fields.values()) + [interaction_id],
        )
        db.commit()
        db.close()

    def get_interaction(self, interaction_id: str) -> Optional[dict]:
        db = self._connect()
        row = db.execute(
            "SELECT * FROM interactions WHERE id = ?", [interaction_id],
        ).fetchone()
        db.close()
        return dict(row) if row else None

    def get_recent_interactions(
        self,
        *,
        limit: int = 50,
        group_folder: Optional[str] = None,
        min_score: Optional[float] = None,
        max_score: Optional[float] = None,
        eval_suite: Optional[str] = "runtime",
        domain: Optional[str] = None,
    ) -> list[dict]:
        db = self._connect()
        clauses: list[str] = []
        params: list = []

        if eval_suite is not None:
            clauses.append("eval_suite = ?")
            params.append(eval_suite)
        if group_folder:
            clauses.append("group_folder = ?")
            params.append(group_folder)
        if min_score is not None:
            clauses.append("judge_score >= ?")
            params.append(min_score)
        if max_score is not None:
            clauses.append("judge_score <= ?")
            params.append(max_score)
        if domain:
            clauses.append("domain_presets LIKE ?")
            params.append(f'%"{domain}"%')

        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = db.execute(
            f"SELECT * FROM interactions {where_clause} ORDER BY timestamp DESC LIMIT ?",
            params + [limit],
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]

    def get_previous_in_session(
        self, session_id: str, current_id: str,
    ) -> Optional[dict]:
        if not session_id:
            return None
        db = self._connect()
        row = db.execute(
            """
            SELECT * FROM interactions
            WHERE session_id = ? AND id != ?
            ORDER BY timestamp DESC LIMIT 1
            """,
            (session_id, current_id),
        ).fetchone()
        db.close()
        return dict(row) if row else None

    def count_interactions(self, **filters) -> int:
        db = self._connect()
        clauses: list[str] = []
        params: list = []
        for k, v in filters.items():
            if k == "eval_suite":
                clauses.append("eval_suite = ?")
                params.append(v)
            elif k == "scored":
                clauses.append("judge_score IS NOT NULL")
            elif k == "since_timestamp":
                clauses.append("timestamp > ?")
                params.append(v)
            elif k == "domain":
                clauses.append("domain_presets LIKE ?")
                params.append(f'%"{v}"%')
        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        count = db.execute(
            f"SELECT COUNT(*) FROM interactions {where_clause}", params,
        ).fetchone()[0]
        db.close()
        return count

    # ── Score trend ──────────────────────────────────────────────────────────

    def score_trend(
        self,
        *,
        group_folder: Optional[str] = None,
        days: int = 30,
        domain: Optional[str] = None,
    ) -> list[dict]:
        db = self._connect()
        params: list = []
        extra_clauses = ""
        if group_folder:
            extra_clauses += " AND group_folder = ?"
            params.append(group_folder)
        if domain:
            extra_clauses += " AND domain_presets LIKE ?"
            params.append(f'%"{domain}"%')
        rows = db.execute(
            f"""
            SELECT DATE(timestamp) AS day, AVG(judge_score) AS avg_score, COUNT(*) AS count
            FROM interactions
            WHERE judge_score IS NOT NULL
              AND timestamp >= DATETIME('now', '-{days} days')
              {extra_clauses}
            GROUP BY day
            ORDER BY day
            """,
            params,
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]

    # ── Reflection operations ────────────────────────────────────────────────

    def save_reflection(
        self,
        *,
        reflection_id: str,
        content: str,
        category: str,
        score_at_gen: float,
        timestamp: str,
        embedding: bytes,
        interaction_id: Optional[str] = None,
        group_folder: Optional[str] = None,
    ) -> str:
        db = self._connect()
        db.execute(
            """
            INSERT INTO reflections
                (id, interaction_id, timestamp, group_folder, content,
                 category, score_at_gen)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (reflection_id, interaction_id, timestamp, group_folder,
             content, category, score_at_gen),
        )
        row = db.execute(
            "SELECT rowid FROM reflections WHERE id = ?", [reflection_id],
        ).fetchone()
        rowid = row[0]
        db.execute(
            "INSERT INTO reflection_embeddings(rowid, embedding) VALUES (?, ?)",
            (rowid, embedding),
        )
        db.commit()
        db.close()
        return reflection_id

    def get_reflections_by_embedding(
        self,
        embedding: bytes,
        top_k: int,
        group_folder: Optional[str] = None,
        min_score: Optional[float] = None,
    ) -> list[dict]:
        db = self._connect()
        try:
            rows = db.execute(
                """
                SELECT r.id, r.content, r.category, r.score_at_gen,
                       r.times_helpful, r.times_retrieved,
                       re.distance
                FROM reflection_embeddings re
                JOIN reflections r ON r.rowid = re.rowid
                WHERE re.embedding MATCH ? AND k = ?
                  AND (r.group_folder = ? OR r.group_folder IS NULL)
                  AND r.archived_at IS NULL
                ORDER BY re.distance, r.times_helpful DESC
                """,
                [embedding, top_k * 2, group_folder],
            ).fetchall()
        except Exception:
            rows = []
        finally:
            db.close()

        results = [dict(zip(
            ["id", "content", "category", "score_at_gen",
             "times_helpful", "times_retrieved", "distance"],
            row,
        )) for row in rows[:top_k]]
        return results

    def check_reflection_duplicate(
        self,
        embedding: bytes,
        group_folder: Optional[str],
        threshold: float,
    ) -> bool:
        db = self._connect()
        try:
            if group_folder is None:
                row = db.execute(
                    """
                    SELECT re.distance
                    FROM reflection_embeddings re
                    JOIN reflections r ON r.rowid = re.rowid
                    WHERE re.embedding MATCH ? AND k = 1
                      AND r.archived_at IS NULL
                    """,
                    [embedding],
                ).fetchone()
            else:
                row = db.execute(
                    """
                    SELECT re.distance
                    FROM reflection_embeddings re
                    JOIN reflections r ON r.rowid = re.rowid
                    WHERE re.embedding MATCH ? AND k = 1
                      AND r.group_folder = ?
                      AND r.archived_at IS NULL
                    """,
                    [embedding, group_folder],
                ).fetchone()
            if row and row[0] < threshold:
                return True
        except Exception:
            pass  # vec0 table empty or unavailable -- allow insert
        finally:
            db.close()
        return False

    def increment_reflection_retrieved(self, reflection_id: str) -> None:
        db = self._connect()
        db.execute(
            "UPDATE reflections SET times_retrieved = times_retrieved + 1 WHERE id = ?",
            [reflection_id],
        )
        db.commit()
        db.close()

    def increment_reflection_helpful(self, reflection_id: str) -> None:
        db = self._connect()
        db.execute(
            "UPDATE reflections SET times_helpful = times_helpful + 1 WHERE id = ?",
            [reflection_id],
        )
        db.commit()
        db.close()

    def archive_stale_reflections(self, days: int) -> int:
        db = self._connect()
        rows = db.execute(
            """
            SELECT id FROM reflections
            WHERE times_retrieved = 0
              AND timestamp < datetime('now', ? || ' days')
              AND archived_at IS NULL
            """,
            [f"-{days}"],
        ).fetchall()
        count = len(rows)
        if count > 0:
            db.execute(
                """
                UPDATE reflections
                SET archived_at = datetime('now')
                WHERE times_retrieved = 0
                  AND timestamp < datetime('now', ? || ' days')
                  AND archived_at IS NULL
                """,
                [f"-{days}"],
            )
            db.commit()
        db.close()
        return count

    def count_stale_reflections(self, days: int) -> int:
        db = self._connect()
        count = db.execute(
            """
            SELECT COUNT(*) FROM reflections
            WHERE times_retrieved = 0
              AND timestamp < datetime('now', ? || ' days')
              AND archived_at IS NULL
            """,
            [f"-{days}"],
        ).fetchone()[0]
        db.close()
        return count

    def count_reflections(self) -> int:
        db = self._connect()
        count = db.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
        db.close()
        return count

    def count_helpful_reflections(self) -> int:
        db = self._connect()
        count = db.execute(
            "SELECT COUNT(*) FROM reflections WHERE times_helpful > 0"
        ).fetchone()[0]
        db.close()
        return count

    def reflections_by_category(self) -> list[dict]:
        db = self._connect()
        rows = db.execute(
            "SELECT category, COUNT(*) AS n FROM reflections GROUP BY category ORDER BY n DESC"
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]

    def get_reflections_for_interaction(self, interaction_id: str) -> list[dict]:
        db = self._connect()
        rows = db.execute(
            "SELECT id FROM reflections WHERE interaction_id = ?",
            [interaction_id],
        ).fetchall()
        db.close()
        return [dict(r) for r in rows]

    # ── Artifact operations ──────────────────────────────────────────────────

    def save_artifact(
        self,
        *,
        artifact_id: str,
        module: str,
        content: str,
        created_at: str,
        baseline_score: Optional[float] = None,
        optimized_score: Optional[float] = None,
        sample_count: Optional[int] = None,
    ) -> str:
        db = self._connect()
        db.execute(
            "UPDATE prompt_artifacts SET active = 0 WHERE module = ?", [module]
        )
        db.execute(
            """
            INSERT INTO prompt_artifacts
                (id, created_at, module, content, baseline_score,
                 optimized_score, sample_count, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (artifact_id, created_at, module, content,
             baseline_score, optimized_score, sample_count),
        )
        db.commit()
        db.close()
        return artifact_id

    def get_active_artifact(self, module: str) -> Optional[dict]:
        db = self._connect()
        row = db.execute(
            "SELECT * FROM prompt_artifacts WHERE module = ? AND active = 1",
            [module],
        ).fetchone()
        db.close()
        return dict(row) if row else None

    def list_artifacts(self, module: Optional[str] = None, limit: int = 10) -> list[dict]:
        db = self._connect()
        if module:
            rows = db.execute(
                "SELECT * FROM prompt_artifacts WHERE module = ? ORDER BY created_at DESC LIMIT ?",
                [module, limit],
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM prompt_artifacts ORDER BY created_at DESC LIMIT ?",
                [limit],
            ).fetchall()
        db.close()
        return [dict(r) for r in rows]

    def get_latest_artifact_timestamp(self) -> Optional[str]:
        db = self._connect()
        row = db.execute(
            "SELECT created_at FROM prompt_artifacts ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        db.close()
        return row["created_at"] if row else None

    # ── Principle extraction tracking ────────────────────────────────────────

    def get_last_extraction(self, domain: str) -> Optional[dict]:
        db = self._connect()
        row = db.execute(
            "SELECT extracted_at FROM principle_extractions "
            "WHERE domain = ? ORDER BY extracted_at DESC LIMIT 1",
            (domain,),
        ).fetchone()
        db.close()
        return dict(row) if row else None

    def record_extraction(
        self,
        *,
        extraction_id: str,
        domain: str,
        extracted_at: str,
        interaction_count: int,
        principles_count: int,
    ) -> None:
        db = self._connect()
        db.execute(
            "INSERT INTO principle_extractions (id, domain, extracted_at, interaction_count, principles_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (extraction_id, domain, extracted_at, interaction_count, principles_count),
        )
        db.commit()
        db.close()

    # ── Status / aggregate queries ───────────────────────────────────────────

    def interaction_stats(self, eval_suite: str) -> dict:
        db = self._connect()
        total = db.execute(
            "SELECT COUNT(*) FROM interactions WHERE eval_suite=?", [eval_suite]
        ).fetchone()[0]
        scored = db.execute(
            "SELECT COUNT(*) FROM interactions WHERE eval_suite=? AND judge_score IS NOT NULL",
            [eval_suite],
        ).fetchone()[0]
        avg = db.execute(
            "SELECT AVG(judge_score) FROM interactions WHERE eval_suite=? AND judge_score IS NOT NULL",
            [eval_suite],
        ).fetchone()[0]
        db.close()
        return {"total": total, "scored": scored, "avg_score": avg}

    def backfill_reflection_count(self) -> int:
        db = self._connect()
        count = db.execute(
            "SELECT COUNT(*) FROM reflections r "
            "JOIN interactions i ON r.interaction_id = i.id "
            "WHERE i.eval_suite = 'backfill'"
        ).fetchone()[0]
        db.close()
        return count

    def count_scored_since(self, since_timestamp: str) -> int:
        db = self._connect()
        count = db.execute(
            "SELECT COUNT(*) FROM interactions "
            "WHERE judge_score IS NOT NULL AND timestamp > ?",
            (since_timestamp,),
        ).fetchone()[0]
        db.close()
        return count

    def count_new_scored(
        self,
        *,
        since_timestamp: Optional[str] = None,
        domain: Optional[str] = None,
    ) -> int:
        db = self._connect()
        clauses = ["judge_score IS NOT NULL"]
        params: list = []
        if since_timestamp:
            clauses.append("timestamp > ?")
            params.append(since_timestamp)
        if domain:
            clauses.append("domain_presets LIKE ?")
            params.append(f'%"{domain}"%')
        count = db.execute(
            f"SELECT COUNT(*) FROM interactions WHERE {' AND '.join(clauses)}",
            params,
        ).fetchone()[0]
        db.close()
        return count

    def domain_comparison(self, domain: str) -> dict:
        db = self._connect()
        with_preset = db.execute(
            "SELECT AVG(judge_score) AS avg, COUNT(*) AS n FROM interactions "
            "WHERE judge_score IS NOT NULL AND domain_presets LIKE ?",
            (f'%"{domain}"%',),
        ).fetchone()
        without_preset = db.execute(
            "SELECT AVG(judge_score) AS avg, COUNT(*) AS n FROM interactions "
            "WHERE judge_score IS NOT NULL AND (domain_presets IS NULL OR domain_presets NOT LIKE ?)",
            (f'%"{domain}"%',),
        ).fetchone()
        db.close()
        return {
            "with_avg": with_preset["avg"] or 0,
            "with_n": with_preset["n"] or 0,
            "without_avg": without_preset["avg"] or 0,
            "without_n": without_preset["n"] or 0,
        }
