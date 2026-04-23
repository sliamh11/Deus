#!/usr/bin/env python3
"""memory_tree.py — hierarchical memory navigation with collapsed-flat retrieval.

Design: the tree is a human/agent-readable map; retrieval is flat-over-all-nodes
plus 1-hop graph expansion via see_also/alias_of edges. Storage is sqlite-vec at
~/.deus/memory_tree.db (override via DEUS_MEMORY_TREE_DB). Embeddings reuse the
evolution provider (Ollama embeddinggemma by default, Gemini fallback).

Subcommands: build | query | reembed | reindex-external | check | graph | calibrate | benchmark

See docs/decisions/no-db-deletion.md (soft-delete only) and
docs/decisions/evolution-db-split.md (separate DB file per subsystem).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import struct
import sys
import time
from datetime import datetime, timezone  # noqa: F401 -- timezone re-exported through _time
from pathlib import Path

# Local helpers — _time.py lives next to this script.
_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
from _time import local_now, utc_now  # noqa: E402


def _utc_iso() -> str:
    """Naive-looking UTC timestamp — tz-aware internally, stripped on write."""
    return utc_now().replace(tzinfo=None).isoformat(timespec="seconds")

from typing import Any

# Reuse embedding infra from the evolution provider (Ollama default).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    import sqlite_vec
except ImportError:
    sqlite_vec = None


# ── Config ────────────────────────────────────────────────────────────────────

EMBED_DIM = 768
DB_PATH = Path(os.environ.get(
    "DEUS_MEMORY_TREE_DB", "~/.deus/memory_tree.db"
)).expanduser()
VAULT_PATH_ENV = "DEUS_VAULT_PATH"

# Thresholds are provider-specific: Gemini embeddings produce higher absolute
# cosine scores (~0.55-0.73 in-domain, ~0.45-0.53 OOD) vs Ollama embeddinggemma
# (~0.30-0.66 in-domain, ~0.25-0.39 OOD). Env vars always override.
_EMBED_PROVIDER = os.environ.get("EMBEDDING_PROVIDER", "auto").lower()
_IS_GEMINI = _EMBED_PROVIDER == "gemini" or (
    _EMBED_PROVIDER == "auto" and not os.environ.get("OLLAMA_HOST")
    and os.environ.get("GEMINI_API_KEY")
)

_THRESHOLD_DEFAULTS = {
    "gemini": {"low": 0.55, "abstain": 0.54, "gap": 0.02},
    "ollama": {"low": 0.55, "abstain": 0.30, "gap": 0.04},
}
_DEFAULTS = _THRESHOLD_DEFAULTS["gemini" if _IS_GEMINI else "ollama"]

DEFAULT_LOW_THRESHOLD = float(os.environ.get("DEUS_TREE_LOW", str(_DEFAULTS["low"])))
DEFAULT_ABSTAIN_THRESHOLD = float(os.environ.get("DEUS_TREE_ABSTAIN", str(_DEFAULTS["abstain"])))
DEFAULT_TOP_K = 5
NEIGHBOR_HOPS = 1
ROOT_TOKEN_BUDGET = 800  # MEMORY_TREE.md cold-start cap

NODE_TYPES_TRACKED = {"memory-tree-root", "persona-index", "persona-node", "project-node", "infra-node",
                      "feedback", "project", "reference", "user"}

EXTERNAL_NAMESPACE = "auto-memory/"
EXTERNAL_DIR_ENV = "DEUS_AUTO_MEMORY_DIR"

DEFAULT_SCORE_GAP_THRESHOLD = float(os.environ.get("DEUS_TREE_GAP", str(_DEFAULTS["gap"])))

# Hybrid retrieval: FTS5 BM25 + vector cosine, fused via RRF.
DEFAULT_USE_FTS = os.environ.get("DEUS_TREE_FTS", "1") == "1"
DEFAULT_RRF_K = int(os.environ.get("DEUS_TREE_RRF_K", "60"))

_LOG_PATH = Path(os.environ.get(
    "DEUS_TREE_LOG", "~/.deus/memory_tree_queries.jsonl"
)).expanduser()

_AUDIT_PATH = Path(os.environ.get(
    "DEUS_TREE_AUDIT", "~/.deus/memory_tree_audit.jsonl"
)).expanduser()

# Rebuild safety: abort if vault walk would produce fewer than this fraction
# of the current active node count (protects against DEUS_VAULT_PATH misconfig
# silently wiping live data — see 2026-04-15 incident). `--force` bypasses.
REBUILD_MIN_RETENTION = 0.5


def is_external_namespace(path: str) -> bool:
    """True if path belongs to an external population (e.g. auto-memory/)."""
    return path.startswith(EXTERNAL_NAMESPACE)


# ── Retrieval policy (ported from ~/deus-memory-evo exp_0006) ─────────────────
# Evo round (2026-04-16) lifted end-to-end retrieval from 0.7172 → 0.9931 by
# adding persona-keyword gating on below-threshold hits, focused retries on
# abstain, supplementary hints for ambiguous triggers, and adaptive top-K.
# Tune these sets carefully: adding a noisy trigger re-introduces false
# Persona leaves for abstain-near queries (salary, car, etc.).

PERSONA_TRIGGERS = {
    # life / household
    "roommate", "roommates", "flatmate", "flatmates", "housemate", "housemates",
    "apartment", "flat", "household", "live", "lives", "living", "moving",
    "shani", "omer", "eden", "rent", "home",
    # taste
    "movie", "movies", "film", "films", "cinema", "director", "directors",
    "music", "genre", "genres", "instrument", "song", "songs", "album",
    "taste", "media", "watch", "watching", "shows",
    # work style / communication
    "communicat", "communicates", "communication", "learn", "learning", "learns",
    "explanation", "explanations", "tone", "style", "prefer", "preference", "preferences",
    "phrase", "address", "pick",
    # identity / background
    "persona", "background", "engineering", "education", "history", "employment",
    "enroll", "university", "college", "program", "prior", "came",
    # study vault files (non-Persona but tree-indexed)
    "lecture", "tutor", "study",
    # broad personal descriptors
    "enjoy", "enjoys", "enjoyed", "describe", "described", "culture", "cultural",
    "world", "person", "identity", "interest", "interests", "care",
}

# On fell_back, retry with a focused alias when the query contains these words.
TRIGGER_RETRY_TERMS = {
    "director": "directors", "directors": "directors",
    "film": "films", "films": "films", "cinema": "cinema",
    "movie": "movies", "movies": "movies",
    "music": "music", "genre": "music genres", "genres": "music genres",
    "song": "music", "songs": "music", "album": "music",
    "roommate": "roommates", "roommates": "roommates",
    "flatmate": "roommates", "housemate": "roommates", "housemates": "roommates",
    "household": "household",
    "lecture": "lecture strategies", "tutor": "study tutor",
}

# Parallel focused hints merged into the primary results when the trigger is
# too ambiguous to route alone. Only below-threshold Persona queries use these.
SUPPLEMENTARY_HINTS = {
    "home": "roommates",  # "home stuff" → household.md via "roommates"
}

# Adaptive K: shrink supplementary results when the top hit is high-confidence
# (query strongly matched one file → noisy extras hurt). Low-confidence Persona
# queries keep top-K for multi-path coverage.
POLICY_K_HIGH_CONF = 2
POLICY_K_LOW_CONF = 4

_POLICY_TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{2,}")


def _query_persona_triggers(query: str) -> tuple[bool, list[str]]:
    """Return (has_trigger, matched_trigger_stems)."""
    matched: list[str] = []
    for raw in _POLICY_TOKEN_RE.findall(query.lower()):
        if raw in PERSONA_TRIGGERS:
            matched.append(raw)
            continue
        for suf in ("ies", "es", "ed", "ing", "s"):
            if len(raw) > len(suf) + 2 and raw.endswith(suf):
                stem = raw[: -len(suf)]
                if stem in PERSONA_TRIGGERS:
                    matched.append(stem)
                    break
                if raw[:-1] in PERSONA_TRIGGERS:
                    matched.append(raw[:-1])
                    break
    return bool(matched), matched


# ── ID + hashing ──────────────────────────────────────────────────────────────

def make_id() -> str:
    """Lexicographically sortable 32-char hex ID. 48-bit timestamp + 80-bit random."""
    ts_ms = int(time.time() * 1000).to_bytes(6, "big")
    rand = secrets.token_bytes(10)
    return (ts_ms + rand).hex()


def content_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── Embedding / vector math ───────────────────────────────────────────────────

def serialize(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def deserialize(buf: bytes) -> list[float]:
    n = len(buf) // 4
    return list(struct.unpack(f"{n}f", buf))


def cosine(u: list[float], v: list[float]) -> float:
    """Cosine similarity. gemini-embedding and embeddinggemma both return
    approximately L2-normalized vectors, but we normalize defensively."""
    if not u or not v:
        return 0.0
    du = sum(x * x for x in u) ** 0.5 or 1.0
    dv = sum(x * x for x in v) ** 0.5 or 1.0
    return sum(a * b for a, b in zip(u, v)) / (du * dv)


def embed_text(text: str) -> list[float]:
    """Embed via the evolution provider. Monkey-patched in tests."""
    from evolution.providers.embeddings import embed as _embed
    return _embed(text)


# ── FTS5 helpers ─────────────────────────────────────────────────────────────

def _fts_available(db: sqlite3.Connection) -> bool:
    """Check if the nodes_fts table exists."""
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes_fts'"
    ).fetchone()
    return row is not None


_FTS_STOP_WORDS = frozenset({
    "the", "is", "at", "which", "on", "in", "to", "for", "of", "an",
    "it", "be", "as", "do", "by", "or", "if", "up", "so", "no", "we",
    "my", "me", "am", "are", "was", "has", "had", "how", "its", "can",
    "did", "but", "our", "you", "what", "when", "where", "who", "does",
    "should", "would", "could", "this", "that", "with", "from", "have",
    "there", "been", "were", "they", "them", "will", "about",
})


def _fts_escape(query: str) -> str:
    """Strip FTS5 syntax + stop words, join remaining terms with OR.

    FTS5 defaults to implicit AND, which is too strict for natural-language
    queries against a knowledge base. Stop-word removal prevents common
    words like 'what', 'is', 'my' from drowning out the content terms.
    """
    cleaned = re.sub(r'["()*\-/\\?!@#$%^&+=<>{}[\]|~`:;,.\']', " ", query)
    cleaned = re.sub(r"\b(AND|OR|NOT|NEAR)\b", " ", cleaned, flags=re.IGNORECASE)
    tokens = [t for t in cleaned.split() if len(t) >= 2 and t.lower() not in _FTS_STOP_WORDS]
    if not tokens:
        return ""
    return " OR ".join(tokens)


def _body_from_content(content: str) -> str:
    """Strip frontmatter, return body text."""
    m = _FM_RE.match(content)
    if m:
        return content[m.end():].strip()
    return content.strip()


def _fts_upsert(
    db: sqlite3.Connection, node_id: str,
    title: str, description: str, body: str,
) -> None:
    """Insert or replace a node's FTS5 entry. Silent no-op if FTS5 unavailable."""
    try:
        rowid = _rowid_for(node_id)
        db.execute("DELETE FROM nodes_fts WHERE rowid = ?", (rowid,))
        db.execute(
            "INSERT INTO nodes_fts(rowid, title, description, body) VALUES (?, ?, ?, ?)",
            (rowid, title or "", description or "", body or ""),
        )
    except sqlite3.OperationalError:
        pass


def _fts_delete(db: sqlite3.Connection, node_id: str) -> None:
    """Remove a node from the FTS5 index. Silent no-op if FTS5 unavailable."""
    try:
        db.execute("DELETE FROM nodes_fts WHERE rowid = ?", (_rowid_for(node_id),))
    except sqlite3.OperationalError:
        pass


def _fts_query(
    db: sqlite3.Connection, query: str, k: int,
    _rowid_to_id: dict[int, str] | None = None,
) -> list[tuple[str, int]]:
    """FTS5 BM25-ranked search. Returns [(node_id, 1-based rank)].

    Returns [] if FTS5 is unavailable or the query matches nothing.
    _rowid_to_id is built once per retrieve() call and passed in.
    """
    escaped = _fts_escape(query)
    if not escaped.strip():
        return []
    if _rowid_to_id is None:
        _rowid_to_id = {
            _rowid_for(nid): nid
            for (nid,) in db.execute(
                "SELECT id FROM nodes WHERE orphaned_at IS NULL"
            ).fetchall()
        }
    try:
        rows = db.execute(
            """
            SELECT rowid FROM nodes_fts
            WHERE nodes_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            [escaped, k * 3],
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    result: list[tuple[str, int]] = []
    for (fts_rowid,) in rows:
        nid = _rowid_to_id.get(fts_rowid)
        if nid is not None:
            result.append((nid, len(result) + 1))
    return result[:k]


def _rrf_fuse(
    vec_ranked: list[tuple[str, int]],
    fts_ranked: list[tuple[str, int]],
    k_rrf: int = 60,
    top: int = 10,
) -> list[str]:
    """Reciprocal Rank Fusion. Returns node IDs sorted by fused score."""
    scores: dict[str, float] = {}
    for nid, rank in vec_ranked:
        scores[nid] = scores.get(nid, 0.0) + 1.0 / (k_rrf + rank)
    for nid, rank in fts_ranked:
        scores[nid] = scores.get(nid, 0.0) + 1.0 / (k_rrf + rank)
    return [nid for nid, _ in sorted(scores.items(), key=lambda x: -x[1])[:top]]


# ── Frontmatter parsing ───────────────────────────────────────────────────────

_FM_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)


def parse_frontmatter(content: str) -> dict[str, Any]:
    """Minimal YAML-ish parser for the fields we care about.

    Recognizes: id, description, children (list), see_also (list), alias_of,
    level (int), title, type, orphaned_at. Quoted and unquoted strings both work.
    Not a full YAML parser — deliberately small, no deps.
    """
    m = _FM_RE.match(content)
    if not m:
        return {}
    fm = m.group(1)
    out: dict[str, Any] = {}

    for scalar in ("id", "name", "description", "summary", "alias_of", "title", "type", "orphaned_at"):
        sm = re.search(
            rf"^{scalar}:\s*>?\s*\n?\s*(.+?)(?=\n\S|\n---|\Z)",
            fm,
            re.MULTILINE | re.DOTALL,
        )
        if sm:
            val = re.sub(r"\n\s+", " ", sm.group(1)).strip().strip('"').strip("'")
            out[scalar] = val

    # Existing vault files use `summary:`; treat as fallback description so we
    # don't duplicate prose.
    if "description" not in out and "summary" in out:
        out["description"] = out["summary"]

    lm = re.search(r"^level:\s*(\d+)\s*$", fm, re.MULTILINE)
    if lm:
        out["level"] = int(lm.group(1))

    for listkey in ("children", "see_also"):
        lm_block = re.search(
            rf"^{listkey}:\s*\n((?:\s+-\s+.+\n?)+)", fm, re.MULTILINE
        )
        if lm_block:
            items = re.findall(r"^\s+-\s+(.+?)\s*$", lm_block.group(1), re.MULTILINE)
            out[listkey] = [i.strip().strip('"').strip("'") for i in items if i.strip()]
        else:
            inline = re.search(rf"^{listkey}:\s*\[(.*?)\]\s*$", fm, re.MULTILINE)
            if inline:
                items = [s.strip().strip('"').strip("'") for s in inline.group(1).split(",")]
                out[listkey] = [i for i in items if i]

    return out


EMBED_BODY_WORDS = 200


def embedding_source(description: str, content: str) -> str:
    """Build richer embedding input from description + body excerpt.

    The description alone (~15 words) can't discriminate 130+ nodes.
    Appending the first ~200 words of the body (after frontmatter) gives
    the embedding model the semantic specifics it needs.
    """
    body = _FM_RE.sub("", content, count=1).strip()
    body_words = body.split()[:EMBED_BODY_WORDS]
    if body_words:
        return description + " — " + " ".join(body_words)
    return description


def token_estimate(text: str) -> int:
    """Rough token count — words × 1.3 for English prose."""
    return int(len(text.split()) * 1.3)


# ── DB ────────────────────────────────────────────────────────────────────────

def open_db(db_path: Path = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    if sqlite_vec is not None:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    db.execute("""
        CREATE TABLE IF NOT EXISTS nodes (
            id           TEXT PRIMARY KEY,
            path         TEXT NOT NULL,
            title        TEXT,
            description  TEXT NOT NULL,
            level        INTEGER NOT NULL DEFAULT 0,
            type         TEXT,
            updated_at   INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            orphaned_at  TEXT DEFAULT NULL,
            orphan_reason TEXT DEFAULT NULL
        )
    """)
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_path ON nodes(path) WHERE orphaned_at IS NULL")
    db.execute("""
        CREATE TABLE IF NOT EXISTS edges (
            src_id     TEXT NOT NULL,
            dst_id     TEXT NOT NULL,
            kind       TEXT NOT NULL,
            weight     REAL NOT NULL DEFAULT 1.0,
            created_at INTEGER NOT NULL,
            expired_at TEXT DEFAULT NULL,
            PRIMARY KEY (src_id, dst_id, kind),
            CHECK (src_id != dst_id)
        )
    """)
    db.execute("CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id, kind)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id, kind)")
    if sqlite_vec is not None:
        # safe: EMBED_DIM is a module-level int constant defined at line 49
        # of this file. SQLite cannot parameterize vec0 dimensions.
        # See PR #9 in docs/decisions/error-discipline.md.
        db.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS embeddings
            USING vec0(embedding float[{EMBED_DIM}])
        """)
    try:
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts
            USING fts5(title, description, body, tokenize='porter unicode61')
        """)
    except sqlite3.OperationalError:
        pass  # FTS5 unavailable — hybrid degrades to vector-only

    db.execute("""
        CREATE TABLE IF NOT EXISTS queries_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               TEXT NOT NULL,
            query            TEXT NOT NULL,
            trace            TEXT NOT NULL,
            final_confidence REAL NOT NULL,
            route            TEXT NOT NULL,
            fell_back        INTEGER NOT NULL DEFAULT 0
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS calibration (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            calibrated_at    TEXT NOT NULL,
            low_threshold    REAL NOT NULL,
            abstain_threshold REAL NOT NULL,
            sample_count     INTEGER NOT NULL,
            notes            TEXT
        )
    """)
    db.commit()
    return db


def _rowid_for(node_id: str) -> int:
    """Stable 63-bit rowid from the node's ULID, for vec0's INTEGER rowid."""
    h = hashlib.sha1(node_id.encode()).digest()
    return int.from_bytes(h[:8], "big") & 0x7FFFFFFFFFFFFFFF


def upsert_node(
    db: sqlite3.Connection,
    *,
    node_id: str,
    path: str,
    title: str,
    description: str,
    level: int,
    node_type: str,
    embedding: list[float] | None,
    content_hash_val: str,
    body_text: str | None = None,
) -> None:
    """Insert or update a node + its embedding + FTS5 index. Soft-deletes
    prior versions by path if the ID has changed."""
    now = int(time.time())
    old_rows = db.execute(
        "SELECT id FROM nodes WHERE path = ? AND id != ? AND orphaned_at IS NULL",
        (path, node_id),
    ).fetchall()
    for (old_id,) in old_rows:
        _fts_delete(db, old_id)
    db.execute(
        """
        UPDATE nodes SET orphaned_at = ?, orphan_reason = 'superseded'
        WHERE path = ? AND id != ? AND orphaned_at IS NULL
        """,
        (_utc_iso(), path, node_id),
    )
    db.execute(
        """
        INSERT INTO nodes (id, path, title, description, level, type, updated_at, content_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            path = excluded.path,
            title = excluded.title,
            description = excluded.description,
            level = excluded.level,
            type = excluded.type,
            updated_at = excluded.updated_at,
            content_hash = excluded.content_hash,
            orphaned_at = NULL,
            orphan_reason = NULL
        """,
        (node_id, path, title, description, level, node_type, now, content_hash_val),
    )
    if embedding is not None and sqlite_vec is not None:
        rowid = _rowid_for(node_id)
        db.execute("DELETE FROM embeddings WHERE rowid = ?", (rowid,))
        db.execute(
            "INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
            (rowid, serialize(embedding)),
        )
    _fts_upsert(db, node_id, title, description, body_text or "")


def upsert_edge(
    db: sqlite3.Connection, *, src: str, dst: str, kind: str, weight: float = 1.0
) -> None:
    now = int(time.time())
    db.execute(
        """
        INSERT INTO edges (src_id, dst_id, kind, weight, created_at, expired_at)
        VALUES (?, ?, ?, ?, ?, NULL)
        ON CONFLICT(src_id, dst_id, kind) DO UPDATE SET
            weight = excluded.weight,
            expired_at = NULL
        """,
        (src, dst, kind, weight, now),
    )


def expire_edges_missing(
    db: sqlite3.Connection, *, src: str, kind: str, keep_dst: set[str]
) -> None:
    """Soft-expire edges from src of the given kind that are not in keep_dst."""
    now_iso = _utc_iso()
    rows = db.execute(
        "SELECT dst_id FROM edges WHERE src_id = ? AND kind = ? AND expired_at IS NULL",
        (src, kind),
    ).fetchall()
    for (dst,) in rows:
        if dst not in keep_dst:
            db.execute(
                "UPDATE edges SET expired_at = ? WHERE src_id = ? AND dst_id = ? AND kind = ?",
                (now_iso, src, dst, kind),
            )


# ── Vault walking ─────────────────────────────────────────────────────────────

def resolve_vault_path() -> Path:
    vault = os.environ.get(VAULT_PATH_ENV)
    if vault:
        return Path(vault).expanduser()
    # Fallback: the Deus default used elsewhere.
    default = Path("~/Desktop/אישי/Brain Dump/Second Brain/Deus").expanduser()
    return default


def iter_tree_files(vault: Path) -> list[Path]:
    """Yield markdown files that participate in the tree (have `id:` or are
    the root). We skip Session-Logs, Checkpoints, Atoms — those are owned by
    the session indexer and would pollute the tree."""
    root = vault / "MEMORY_TREE.md"
    files: list[Path] = []
    if root.exists():
        files.append(root)
    skip_dirs = {"Session-Logs", "Checkpoints", "Atoms", "ARCHIVE", ".git", ".obsidian"}
    for p in vault.rglob("*.md"):
        if any(part in skip_dirs for part in p.relative_to(vault).parts):
            continue
        if p == root:
            continue
        try:
            head = p.read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            continue
        fm = parse_frontmatter(head)
        if fm.get("id"):
            files.append(p)
    return files


# ── Build ─────────────────────────────────────────────────────────────────────

def build_tree(
    vault: Path,
    db: sqlite3.Connection,
    *,
    rebuild: bool = False,
    skip_embed: bool = False,
    force: bool = False,
) -> dict[str, int]:
    """Walk the vault, upsert nodes and edges.

    `rebuild=True` orphans all existing active nodes before re-upserting. A
    safety abort fires if the vault walk would produce fewer than
    REBUILD_MIN_RETENTION (50%) of the current active count unless
    `force=True` — protects against DEUS_VAULT_PATH misconfig silently
    wiping live data (2026-04-15 incident).

    Returns counts: {nodes: N, embedded: M, edges: E, skipped: S, orphaned: O}.
    Raises ValueError if the safety abort fires.
    """
    counts = {"nodes": 0, "embedded": 0, "skipped": 0, "edges": 0, "orphaned": 0}

    # Walk the vault FIRST so the safety check can compare walk size to the
    # current active row count before any orphaning happens.
    path_to_id: dict[str, str] = {}
    node_inputs: list[dict[str, Any]] = []
    for p in iter_tree_files(vault):
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        fm = parse_frontmatter(content)
        node_id = fm.get("id") or make_id()
        rel_path = str(p.relative_to(vault))
        path_to_id[rel_path] = node_id
        node_inputs.append(
            {
                "id": node_id,
                "path": rel_path,
                "abs": p,
                "fm": fm,
                "content": content,
            }
        )

    if rebuild:
        current_active = db.execute(
            "SELECT COUNT(*) FROM nodes WHERE orphaned_at IS NULL AND path NOT LIKE ? || '%'",
            (EXTERNAL_NAMESPACE,),
        ).fetchone()[0]
        threshold = max(1, int(current_active * REBUILD_MIN_RETENTION))
        if current_active > 0 and len(node_inputs) < threshold and not force:
            _emit_audit({
                "action": "rebuild_aborted",
                "vault": str(vault),
                "current_active": current_active,
                "walked": len(node_inputs),
                "threshold_fraction": REBUILD_MIN_RETENTION,
                "reason": "walk_too_small",
            })
            raise ValueError(
                f"Refusing rebuild: vault walk found {len(node_inputs)} files "
                f"but DB has {current_active} active nodes (would retain <"
                f"{int(REBUILD_MIN_RETENTION * 100)}%). "
                f"Check DEUS_VAULT_PATH={vault!s}. Pass force=True to override."
            )
        _backup_db()
        now_iso = _utc_iso()
        db.execute(
            "UPDATE nodes SET orphaned_at = ?, orphan_reason = 'rebuild' "
            "WHERE orphaned_at IS NULL AND path NOT LIKE ? || '%'",
            (now_iso, EXTERNAL_NAMESPACE),
        )
        db.execute(
            "UPDATE edges SET expired_at = ? WHERE expired_at IS NULL", (now_iso,)
        )
        if sqlite_vec is not None:
            db.execute("DELETE FROM embeddings")
        try:
            db.execute("DELETE FROM nodes_fts")
        except sqlite3.OperationalError:
            pass
        db.commit()
        _emit_audit({
            "action": "rebuild",
            "vault": str(vault),
            "orphaned": current_active,
            "walked": len(node_inputs),
            "forced": force,
        })

    # Upsert nodes + embeddings.
    for entry in node_inputs:
        fm = entry["fm"]
        description = fm.get("description", "").strip()
        if not description:
            counts["skipped"] += 1
            continue
        title = fm.get("title") or entry["abs"].stem
        level = int(fm.get("level", 0))
        node_type = fm.get("type", "persona-node")
        # Vault files have long dense bodies that dilute the description signal.
        ch = content_hash(description)
        existing = db.execute(
            "SELECT content_hash FROM nodes WHERE id = ?", (entry["id"],)
        ).fetchone()
        # On rebuild, embeddings were just DELETEd — re-embed even if the
        # content_hash is unchanged, otherwise the vec table stays empty.
        need_embed = rebuild or existing is None or existing[0] != ch
        vec = None
        if need_embed and not skip_embed:
            try:
                vec = embed_text(description)
                counts["embedded"] += 1
            except Exception as exc:
                print(f"WARN: embed failed for {entry['path']}: {exc}", file=sys.stderr)
                vec = None
        upsert_node(
            db,
            node_id=entry["id"],
            path=entry["path"],
            title=title,
            description=description,
            level=level,
            node_type=node_type,
            embedding=vec,
            content_hash_val=ch,
            body_text=_body_from_content(entry["content"]),
        )
        counts["nodes"] += 1

    # Walk children + see_also and write edges (resolve paths to IDs).
    for entry in node_inputs:
        fm = entry["fm"]
        src = entry["id"]
        for kind, key in (("child", "children"), ("see_also", "see_also")):
            targets = fm.get(key, [])
            keep: set[str] = set()
            for tgt_path in targets:
                dst = path_to_id.get(tgt_path)
                if dst is None:
                    print(
                        f"WARN: {entry['path']}: unresolved {kind} → {tgt_path}",
                        file=sys.stderr,
                    )
                    continue
                if dst == src:
                    continue
                upsert_edge(db, src=src, dst=dst, kind=kind)
                keep.add(dst)
                counts["edges"] += 1
            expire_edges_missing(db, src=src, kind=kind, keep_dst=keep)

    # Orphan: any active vault node in DB whose path didn't show up in this walk.
    # External-namespace nodes (auto-memory/*) are managed by reindex_external.
    active = db.execute(
        "SELECT id, path FROM nodes WHERE orphaned_at IS NULL"
    ).fetchall()
    now_iso = _utc_iso()
    for (nid, npath) in active:
        if is_external_namespace(npath):
            continue
        if npath not in path_to_id:
            db.execute(
                "UPDATE nodes SET orphaned_at = ?, orphan_reason = 'missing_file' WHERE id = ?",
                (now_iso, nid),
            )
            _fts_delete(db, nid)
            counts["orphaned"] += 1
    db.commit()
    return counts


def _backup_db() -> None:
    if not DB_PATH.exists():
        return
    ts = local_now().strftime("%Y%m%d-%H%M%S")
    bak = DB_PATH.with_suffix(f".{ts}.bak")
    bak.write_bytes(DB_PATH.read_bytes())


# ── Reembed ────────────────────────────────────────────────────────────────────

def reembed_file(vault: Path, rel_path: str, db: sqlite3.Connection) -> str:
    """Re-embed a single node; no-op if content hash unchanged."""
    p = vault / rel_path
    if not p.exists():
        # External-namespace paths aren't under vault; resolve via env.
        if is_external_namespace(rel_path):
            ext_dir = os.environ.get(EXTERNAL_DIR_ENV)
            if ext_dir:
                filename = rel_path[len(EXTERNAL_NAMESPACE):]
                base = Path(ext_dir).expanduser().resolve()
                p = (base / filename).resolve()
                if not p.is_relative_to(base):
                    return "missing"
                if not p.exists():
                    return "missing"
            else:
                return "missing"
        else:
            return "missing"
    content = p.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(content)
    desc = fm.get("description", "").strip()
    if not desc:
        return "no_description"
    embed_src = embedding_source(desc, content) if is_external_namespace(rel_path) else desc
    ch = content_hash(embed_src)
    row = db.execute(
        "SELECT id, content_hash FROM nodes WHERE path = ? AND orphaned_at IS NULL",
        (rel_path,),
    ).fetchone()
    if row is None:
        return "not_in_tree"
    node_id, old_hash = row
    if old_hash == ch:
        return "unchanged"
    try:
        vec = embed_text(embed_src)
    except Exception as exc:
        print(f"ERROR: embed failed: {exc}", file=sys.stderr)
        return "embed_failed"
    db.execute(
        "UPDATE nodes SET description = ?, content_hash = ?, updated_at = ? WHERE id = ?",
        (desc, ch, int(time.time()), node_id),
    )
    if sqlite_vec is not None:
        rowid = _rowid_for(node_id)
        db.execute("DELETE FROM embeddings WHERE rowid = ?", (rowid,))
        db.execute(
            "INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
            (rowid, serialize(vec)),
        )
    title = fm.get("title") or fm.get("name") or Path(rel_path).stem
    _fts_upsert(db, node_id, title, desc, _body_from_content(content))
    db.commit()
    return "reembedded"


TREE_SKIP_DIRS = frozenset(
    {"Session-Logs", "Checkpoints", "Atoms", "ARCHIVE", ".git", ".obsidian"}
)


def discover_node(vault: Path, rel_path: str, db: sqlite3.Connection) -> str:
    """Add a new vault markdown file to the tree if it has `id:` + `description:`.

    Mirrors the node-creation path of build_tree but for a single file, so
    PostToolUse / stop-hook drift scan can add files without a full rebuild.

    Edges to siblings that aren't in the tree yet are skipped silently — they
    will link next time the sibling is discovered or on the next build_tree.

    Statuses: discovered | no_id | no_description | missing | skipped_dir |
              already_tracked | embed_failed
    """
    parts = Path(rel_path).parts
    if any(part in TREE_SKIP_DIRS for part in parts):
        return "skipped_dir"
    p = vault / rel_path
    if not p.exists():
        return "missing"
    content = p.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(content)
    node_id = fm.get("id")
    if not node_id:
        return "no_id"
    desc = fm.get("description", "").strip()
    if not desc:
        return "no_description"
    existing = db.execute(
        "SELECT 1 FROM nodes WHERE path = ? AND orphaned_at IS NULL",
        (rel_path,),
    ).fetchone()
    if existing is not None:
        return "already_tracked"
    try:
        vec = embed_text(desc)
    except Exception as exc:
        print(f"ERROR: embed failed: {exc}", file=sys.stderr)
        return "embed_failed"
    title = fm.get("title") or p.stem
    level = int(fm.get("level", 0))
    node_type = fm.get("type", "persona-node")
    ch = content_hash(desc)
    upsert_node(
        db,
        node_id=node_id,
        path=rel_path,
        title=title,
        description=desc,
        level=level,
        node_type=node_type,
        embedding=vec,
        content_hash_val=ch,
        body_text=_body_from_content(content),
    )
    for kind, key in (("child", "children"), ("see_also", "see_also")):
        for tgt_path in fm.get(key, []):
            dst_row = db.execute(
                "SELECT id FROM nodes WHERE path = ? AND orphaned_at IS NULL",
                (tgt_path,),
            ).fetchone()
            if dst_row is None or dst_row[0] == node_id:
                continue
            upsert_edge(db, src=node_id, dst=dst_row[0], kind=kind)
    db.commit()
    return "discovered"


# ── Query (3-phase) ───────────────────────────────────────────────────────────

def retrieve(
    db: sqlite3.Connection,
    query: str,
    *,
    k: int = DEFAULT_TOP_K,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    abstain_threshold: float = DEFAULT_ABSTAIN_THRESHOLD,
    query_vec: list[float] | None = None,
    use_see_also: bool = True,
    use_abstain: bool = True,
    use_fts: bool = DEFAULT_USE_FTS,
    rrf_k: int = DEFAULT_RRF_K,
) -> dict[str, Any]:
    """4-phase retrieval: flat cosine → FTS5 BM25 → graph expansion → abstain.

    Phase 1 (vector) and Phase 1b (FTS5 BM25) run independently, then
    Reciprocal Rank Fusion reorders the union. Abstain still gates on raw
    cosine confidence so calibrated thresholds are preserved.

    Returns {results: [{id, path, score, route}], confidence, fell_back, trace}.
    """
    qv = query_vec if query_vec is not None else embed_text(query)

    # Phase 1: flat cosine over all active nodes.
    all_nodes = db.execute(
        "SELECT id, path, title FROM nodes WHERE orphaned_at IS NULL"
    ).fetchall()
    node_lookup: dict[str, tuple[str, str]] = {}  # nid → (path, title)
    cosine_scores: dict[str, float] = {}
    scored: list[tuple[str, str, str, float, str]] = []
    for (nid, npath, ntitle) in all_nodes:
        node_lookup[nid] = (npath, ntitle)
        rowid = _rowid_for(nid)
        erow = db.execute(
            "SELECT embedding FROM embeddings WHERE rowid = ?", (rowid,)
        ).fetchone()
        if erow is None:
            continue
        vec = deserialize(erow[0])
        score = cosine(qv, vec)
        cosine_scores[nid] = score
        scored.append((nid, npath, ntitle, score, "flat"))

    scored.sort(key=lambda r: r[3], reverse=True)
    best = scored[0][3] if scored else 0.0

    trace = [f"flat_top={scored[0][1]}:{best:.3f}" if scored else "flat_empty"]
    fell_back = False

    # Phase 1b: FTS5 BM25 keyword search + RRF fusion.
    fts_hits: list[tuple[str, int]] = []
    if use_fts and _fts_available(db):
        rowid_to_id = {_rowid_for(nid): nid for nid in node_lookup}
        fts_hits = _fts_query(db, query, k=k, _rowid_to_id=rowid_to_id)
        if fts_hits:
            trace.append(f"fts_hits={len(fts_hits)}")
            vec_ranked = [(r[0], rank + 1) for rank, r in enumerate(scored[:k * 2])]
            fused_ids = _rrf_fuse(vec_ranked, fts_hits, k_rrf=rrf_k, top=k * 2)
            reordered: list[tuple[str, str, str, float, str]] = []
            for nid in fused_ids:
                npath, ntitle = node_lookup.get(nid, ("?", "?"))
                cs = cosine_scores.get(nid, 0.0)
                route = "rrf" if nid in dict(fts_hits) else "flat"
                reordered.append((nid, npath, ntitle, cs, route))
            scored = reordered
        else:
            trace.append("fts_hits=0")
    elif not use_fts:
        trace.append("fts_off")

    top = scored[:k]

    # Phase 2: graph expansion when the seed is confident (best ≥ LOW).
    if use_see_also and best >= low_threshold:
        expanded: dict[str, tuple[str, str, str, float, str]] = {r[0]: r for r in top}
        for (nid, npath, ntitle, _score, _route) in top[:3]:
            forward = db.execute(
                """
                SELECT dst_id FROM edges
                WHERE src_id = ? AND kind IN ('see_also', 'alias_of') AND expired_at IS NULL
                """,
                (nid,),
            ).fetchall()
            backward = db.execute(
                """
                SELECT src_id FROM edges
                WHERE dst_id = ? AND kind IN ('see_also', 'alias_of') AND expired_at IS NULL
                """,
                (nid,),
            ).fetchall()
            for (other_id,) in forward + backward:
                if other_id in expanded:
                    continue
                nrow = db.execute(
                    "SELECT path, title FROM nodes WHERE id = ? AND orphaned_at IS NULL",
                    (other_id,),
                ).fetchone()
                if nrow is None:
                    continue
                rowid = _rowid_for(other_id)
                erow = db.execute(
                    "SELECT embedding FROM embeddings WHERE rowid = ?", (rowid,)
                ).fetchone()
                if erow is None:
                    continue
                score = cosine(qv, deserialize(erow[0]))
                expanded[other_id] = (
                    other_id,
                    nrow[0],
                    nrow[1],
                    score,
                    f"neighbor-of:{npath}",
                )
        merged = sorted(expanded.values(), key=lambda r: r[3], reverse=True)[:k]
        top = merged
        trace.append(f"expanded→{len(expanded)}")

    # Phase 3: abstain gates on raw cosine scores (not RRF order).
    # Compute gap from cosine-sorted scores so RRF reordering can't
    # accidentally trigger abstain on queries that vector-only would surface.
    cosine_sorted = sorted(cosine_scores.values(), reverse=True)
    if use_abstain and best < abstain_threshold:
        fell_back = True
        trace.append("abstain:threshold")
        top = []
    elif use_abstain and len(cosine_sorted) >= 2:
        others_avg = sum(cosine_sorted[1:k + 1]) / min(len(cosine_sorted) - 1, k)
        gap = best - others_avg
        gap_threshold = DEFAULT_SCORE_GAP_THRESHOLD
        if gap < gap_threshold and best < low_threshold:
            fell_back = True
            trace.append(f"abstain:gap={gap:.3f}<{gap_threshold}")
            top = []
        else:
            trace.append(f"gap={gap:.3f}")

    result = {
        "results": [
            {"id": r[0], "path": r[1], "title": r[2], "score": r[3], "route": r[4]}
            for r in top
        ],
        "confidence": best,
        "fell_back": fell_back,
        "trace": trace,
    }
    _log_query(db, query, result)
    return result


def retrieve_with_policy(
    db: sqlite3.Connection,
    query: str,
    *,
    k: int = DEFAULT_TOP_K,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    abstain_threshold: float = DEFAULT_ABSTAIN_THRESHOLD,
) -> dict[str, Any]:
    """Retrieval with persona-trigger gating, focused retries, supplementary
    hints, and adaptive K. Ported from ~/deus-memory-evo exp_0006 (round 1
    winner, 0.7172 → 0.9931 on 145-query retrieval benchmark).

    Gating rules:
    - fell_back=True: abstain unless the query contains a PERSONA_TRIGGER;
      then retry once with the TRIGGER_RETRY_TERMS alias and accept that.
    - fell_back=False, confidence >= low_threshold: accept unconditionally.
    - fell_back=False, confidence <  low_threshold: accept only if the query
      contains a PERSONA_TRIGGER (otherwise abstain to protect abstain-near
      gates like "what car do I drive").

    Post-acceptance:
    - Adaptive K: top_score >= low_threshold → cap at POLICY_K_HIGH_CONF;
      else POLICY_K_LOW_CONF.
    - Supplementary hints: for below-threshold Persona queries whose primary
      result might miss a key path, merge the top-1 of a focused hint query.
    """
    has_trigger, trigger_words = _query_persona_triggers(query)
    policy_trace: list[str] = []

    primary = retrieve(
        db, query, k=max(k, POLICY_K_LOW_CONF),
        low_threshold=low_threshold, abstain_threshold=abstain_threshold,
    )

    fell_back = primary["fell_back"]
    confidence = primary["confidence"]
    results = list(primary["results"])

    if fell_back:
        # Try a focused retry when we have a trigger word.
        retry_alias = None
        for tw in trigger_words:
            alias = TRIGGER_RETRY_TERMS.get(tw)
            if alias:
                retry_alias = alias
                break
        if retry_alias:
            policy_trace.append(f"retry:{retry_alias}")
            retry = retrieve(
                db, retry_alias, k=max(k, POLICY_K_LOW_CONF),
                low_threshold=low_threshold, abstain_threshold=abstain_threshold,
            )
            if not retry["fell_back"] and retry["results"]:
                fell_back = False
                confidence = retry["confidence"]
                results = list(retry["results"])
                primary = retry
        if fell_back:
            return {
                "results": [],
                "confidence": confidence,
                "fell_back": True,
                "trace": primary["trace"],
                "policy_trace": policy_trace + ["abstain:fell_back"],
            }

    low_confidence = confidence < low_threshold
    if low_confidence and not has_trigger:
        # Below-threshold and nothing in the query signals Persona coverage →
        # abstain rather than surface a noisy leaf.
        policy_trace.append("abstain:low_conf_no_trigger")
        return {
            "results": [],
            "confidence": confidence,
            "fell_back": True,
            "trace": primary["trace"],
            "policy_trace": policy_trace,
        }

    # Adaptive K cap.
    effective_k = POLICY_K_HIGH_CONF if not low_confidence else POLICY_K_LOW_CONF
    effective_k = min(effective_k, k) if k < POLICY_K_LOW_CONF else effective_k
    results = results[:effective_k]
    policy_trace.append(f"k={effective_k}")

    # Supplementary hints for ambiguous triggers on below-threshold queries.
    if has_trigger and low_confidence:
        seen_paths = {r.get("path") for r in results}
        seen_hints: set[str] = set()
        for tw in trigger_words:
            hint = SUPPLEMENTARY_HINTS.get(tw)
            if not hint or hint in seen_hints:
                continue
            seen_hints.add(hint)
            hint_res = retrieve(
                db, hint, k=1,
                low_threshold=low_threshold, abstain_threshold=abstain_threshold,
            )
            if hint_res["fell_back"] or not hint_res["results"]:
                continue
            top = hint_res["results"][0]
            if top.get("path") in seen_paths:
                continue
            results.append(top)
            seen_paths.add(top.get("path"))
            policy_trace.append(f"hint:{hint}")

    return {
        "results": results,
        "confidence": confidence,
        "fell_back": False,
        "trace": primary["trace"],
        "policy_trace": policy_trace,
    }


def _log_query(db: sqlite3.Connection, query: str, result: dict[str, Any]) -> None:
    try:
        db.execute(
            """
            INSERT INTO queries_log (ts, query, trace, final_confidence, route, fell_back)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_iso(),
                query,
                json.dumps(result["trace"]),
                result["confidence"],
                result["results"][0]["route"] if result["results"] else "none",
                int(result["fell_back"]),
            ),
        )
        db.commit()
    except sqlite3.OperationalError:
        pass
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a") as f:
            f.write(json.dumps({
                "ts": _utc_iso(),
                "query": query,
                "trace": result["trace"],
                "final_confidence": result["confidence"],
                "results": [r["path"] for r in result["results"][:3]],
                "fell_back": result["fell_back"],
            }) + "\n")
    except OSError:
        pass


def _emit_audit(record: dict[str, Any]) -> None:
    """Append a structured audit line for rebuild + orphan operations.

    Silent on write failures — auditing must never block the caller. The
    audit trail lives at ~/.deus/memory_tree_audit.jsonl (override via
    DEUS_TREE_AUDIT). Forensic record for incidents like the 2026-04-15
    wipe, where a rebuild with a misconfigured vault path left 13 active
    rows orphaned.
    """
    payload = {"ts": _utc_iso(), "argv": list(sys.argv), **record}
    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a") as f:
            f.write(json.dumps(payload) + "\n")
    except OSError:
        pass


# ── Check / coverage ──────────────────────────────────────────────────────────

def check_tree(db: sqlite3.Connection, vault: Path) -> dict[str, Any]:
    """Report coverage gaps, orphans, cycles, token budget violations."""
    report: dict[str, Any] = {"ok": True, "issues": []}

    total = db.execute(
        "SELECT COUNT(*) FROM nodes WHERE orphaned_at IS NULL"
    ).fetchone()[0]
    with_emb = 0
    if sqlite_vec is not None:
        with_emb = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
    report["nodes_active"] = total
    report["nodes_with_embedding"] = with_emb

    # Root token budget.
    root_file = vault / "MEMORY_TREE.md"
    if root_file.exists():
        tokens = token_estimate(root_file.read_text(encoding="utf-8", errors="replace"))
        report["root_tokens"] = tokens
        if tokens > ROOT_TOKEN_BUDGET:
            report["ok"] = False
            report["issues"].append(
                f"MEMORY_TREE.md = {tokens} tokens > budget {ROOT_TOKEN_BUDGET}"
            )

    # Every active vault node should be reachable from root via child edges.
    # External-namespace nodes (auto-memory/*) have no parent in the vault tree.
    root_row = db.execute(
        "SELECT id FROM nodes WHERE path = 'MEMORY_TREE.md' AND orphaned_at IS NULL"
    ).fetchone()
    unreachable: list[str] = []
    if root_row:
        reachable = _reachable_via_child(db, root_row[0])
        active_ids = set()
        for r in db.execute(
            "SELECT id, path FROM nodes WHERE orphaned_at IS NULL"
        ).fetchall():
            if not is_external_namespace(r[1]):
                active_ids.add(r[0])
        unreachable = sorted(active_ids - reachable)
        if unreachable:
            report["ok"] = False
            paths = [
                db.execute("SELECT path FROM nodes WHERE id = ?", (u,)).fetchone()[0]
                for u in unreachable
            ]
            report["issues"].append(f"unreachable from root: {paths}")
    else:
        report["issues"].append("MEMORY_TREE.md not found as a node")
        report["ok"] = False

    # Missing descriptions (coverage gaps).
    missing_desc: list[str] = []
    for p in iter_tree_files(vault):
        fm = parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        if not fm.get("description", "").strip():
            missing_desc.append(str(p.relative_to(vault)))
    if missing_desc:
        report["ok"] = False
        report["issues"].append(f"missing description: {missing_desc}")
    report["coverage_gaps"] = missing_desc

    # Cycles in child edges (polyhierarchy allowed only for see_also/alias_of).
    cycles = _detect_child_cycles(db)
    if cycles:
        report["ok"] = False
        report["issues"].append(f"cycles in child edges: {cycles}")

    return report


def autofix_tree(db: sqlite3.Connection, vault: Path) -> dict[str, int]:
    """Idempotent maintenance pass: discover new, orphan missing, reembed stale.

    Does NOT drop embeddings or orphan everything — each action is targeted
    and preserves the no-db-deletion invariant. Safe for daily launchd use.

    Returns counts: {discovered, orphaned, reembedded, skipped}.
    """
    counts = {"discovered": 0, "orphaned": 0, "reembedded": 0, "skipped": 0}
    now_iso = _utc_iso()

    active = db.execute(
        "SELECT id, path, updated_at FROM nodes WHERE orphaned_at IS NULL"
    ).fetchall()
    tracked: set[str] = {row[1] for row in active}

    # 1) Orphan vault nodes whose files are missing.
    # External-namespace nodes are managed by reindex_external.
    for (nid, npath, _) in active:
        if is_external_namespace(npath):
            continue
        if not (vault / npath).exists():
            db.execute(
                "UPDATE nodes SET orphaned_at = ?, orphan_reason = 'missing_file' WHERE id = ?",
                (now_iso, nid),
            )
            _fts_delete(db, nid)
            counts["orphaned"] += 1

    # 2) Discover new .md files under the vault (honors iter_tree_files' skip set).
    for p in iter_tree_files(vault):
        try:
            rel = str(p.relative_to(vault))
        except ValueError:
            continue
        if rel in tracked:
            continue
        status = discover_node(vault, rel, db)
        if status == "discovered":
            counts["discovered"] += 1
        else:
            counts["skipped"] += 1

    # 3) Re-embed stale tracked files (mtime > updated_at, hash-gated inside).
    for (_, npath, updated_at) in active:
        full = vault / npath
        if not full.exists():
            continue
        try:
            mtime = int(full.stat().st_mtime)
        except OSError:
            continue
        if mtime > (updated_at or 0):
            status = reembed_file(vault, npath, db)
            if status == "reembedded":
                counts["reembedded"] += 1

    db.commit()
    _emit_audit({"action": "autofix", "vault": str(vault), **counts})
    return counts


# ── External population (auto-memory) ────────────────────────────────────────

def _write_id_to_frontmatter(path: Path, new_id: str) -> None:
    """Inject `id: <new_id>` into an existing YAML frontmatter block. No-op if id already present."""
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = parse_frontmatter(text)
    if fm.get("id"):
        return
    m = _FM_RE.match(text)
    if not m:
        return
    fm_end = m.end(1)
    updated = text[:fm_end] + f"\nid: {new_id}" + text[fm_end:]
    path.write_text(updated, encoding="utf-8")


def reindex_external(
    db: sqlite3.Connection,
    external_dir: Path,
    *,
    skip_embed: bool = False,
) -> dict[str, int]:
    """Index auto-memory files from an external directory into the tree.

    Files are stored with the `auto-memory/<filename>` namespace prefix to
    avoid vault path collisions. Frontmatter mapping: `name` -> title,
    `description` -> embedding source, `type` -> kept as-is.

    ULID write-back is idempotent: files already carrying `id:` are not
    modified. Bypasses REBUILD_MIN_RETENTION (separate population).
    """
    counts = {"indexed": 0, "embedded": 0, "skipped": 0, "orphaned": 0, "id_written": 0}
    _backup_db()

    external_dir = external_dir.expanduser().resolve()
    if not external_dir.is_dir():
        raise FileNotFoundError(f"DEUS_AUTO_MEMORY_DIR not found: {external_dir}")

    skip_dirs = {"ARCHIVE", ".git"}
    walked_paths: set[str] = set()

    for p in sorted(external_dir.rglob("*.md")):
        try:
            rel_to_ext = p.relative_to(external_dir)
        except ValueError:
            continue
        if any(part in skip_dirs for part in rel_to_ext.parts):
            continue
        if p.name == "MEMORY.md":
            continue

        ns_path = EXTERNAL_NAMESPACE + str(rel_to_ext)
        walked_paths.add(ns_path)

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            counts["skipped"] += 1
            continue

        fm = parse_frontmatter(content)
        description = fm.get("description", "").strip()
        if not description:
            counts["skipped"] += 1
            continue

        node_id = fm.get("id")
        if not node_id:
            node_id = make_id()
            _write_id_to_frontmatter(p, node_id)
            counts["id_written"] += 1

        title = fm.get("name") or p.stem
        node_type = fm.get("type", "feedback")
        embed_src = embedding_source(description, content)
        ch = content_hash(embed_src)

        existing = db.execute(
            "SELECT content_hash FROM nodes WHERE id = ?", (node_id,)
        ).fetchone()
        need_embed = existing is None or existing[0] != ch

        vec = None
        if need_embed and not skip_embed:
            try:
                vec = embed_text(embed_src)
                counts["embedded"] += 1
            except Exception as exc:
                print(f"WARN: embed failed for {ns_path}: {exc}", file=sys.stderr)
                vec = None

        upsert_node(
            db,
            node_id=node_id,
            path=ns_path,
            title=title,
            description=description,
            level=0,
            node_type=node_type,
            embedding=vec,
            content_hash_val=ch,
            body_text=_body_from_content(content),
        )
        counts["indexed"] += 1

    # Orphan external nodes no longer on disk.
    now_iso = _utc_iso()
    ext_active = db.execute(
        "SELECT id, path FROM nodes WHERE orphaned_at IS NULL AND path LIKE ? || '%'",
        (EXTERNAL_NAMESPACE,),
    ).fetchall()
    for (nid, npath) in ext_active:
        if npath not in walked_paths:
            db.execute(
                "UPDATE nodes SET orphaned_at = ?, orphan_reason = 'missing_file' WHERE id = ?",
                (now_iso, nid),
            )
            _fts_delete(db, nid)
            counts["orphaned"] += 1

    db.commit()
    _emit_audit({"action": "reindex_external", "dir": str(external_dir), **counts})
    return counts


# ── Manifest ─────────────────────────────────────────────────────────────────

# Grouping map: node type → human-readable category for the manifest.
_MANIFEST_CATEGORIES: dict[str, str] = {
    "feedback": "Behavioral rules",
    "project": "Project state",
    "reference": "References",
    "user": "Identity & persona",
    "persona-node": "Persona",
    "persona-index": "Persona",
    "infra-node": "Infrastructure",
    "project-node": "Project docs",
    "memory-tree-root": "System",
    "permanent-memory": "Vault core",
    "permanent-reference": "Vault references",
}


def generate_manifest(db: sqlite3.Connection) -> str:
    """Generate a ~200-token manifest from all active nodes, grouped by type."""
    rows = db.execute(
        "SELECT type, title, path FROM nodes WHERE orphaned_at IS NULL ORDER BY type, title"
    ).fetchall()

    groups: dict[str, list[str]] = {}
    for (ntype, title, path) in rows:
        cat = _MANIFEST_CATEGORIES.get(ntype or "", ntype or "other")
        groups.setdefault(cat, []).append(title or path)

    lines = [
        "# Memory Manifest",
        "",
        "Memories are auto-retrieved per prompt by a semantic hook.",
        'Manual retrieval: `memory_tree.py query "<topic>"`',
        "",
        "## Available Knowledge",
    ]
    for cat in sorted(groups.keys()):
        items = groups[cat]
        summary = ", ".join(items[:8])
        if len(items) > 8:
            summary += f", ... (+{len(items) - 8} more)"
        lines.append(f"- **{cat}** ({len(items)}): {summary}")

    return "\n".join(lines) + "\n"


def _reachable_via_child(db: sqlite3.Connection, root_id: str) -> set[str]:
    visited = {root_id}
    frontier = [root_id]
    while frontier:
        nxt = []
        for nid in frontier:
            for (dst,) in db.execute(
                "SELECT dst_id FROM edges WHERE src_id = ? AND kind = 'child' AND expired_at IS NULL",
                (nid,),
            ).fetchall():
                if dst not in visited:
                    visited.add(dst)
                    nxt.append(dst)
        frontier = nxt
    return visited


def _detect_child_cycles(db: sqlite3.Connection) -> list[list[str]]:
    nodes = [
        r[0]
        for r in db.execute(
            "SELECT id FROM nodes WHERE orphaned_at IS NULL"
        ).fetchall()
    ]
    cycles: list[list[str]] = []
    for start in nodes:
        stack = [(start, [start])]
        visited = set()
        while stack:
            nid, path = stack.pop()
            if nid in visited:
                continue
            visited.add(nid)
            for (dst,) in db.execute(
                "SELECT dst_id FROM edges WHERE src_id = ? AND kind = 'child' AND expired_at IS NULL",
                (nid,),
            ).fetchall():
                if dst == start:
                    cycles.append(path + [dst])
                    return cycles
                if dst not in visited:
                    stack.append((dst, path + [dst]))
    return cycles


# ── Graph debug view ──────────────────────────────────────────────────────────

def render_graph(db: sqlite3.Connection, highlight: str | None = None) -> str:
    """Emit a GraphViz dot string of the tree + see_also edges."""
    lines = ["digraph memory_tree {", '  rankdir=LR;', '  node [shape=box, style="rounded,filled"];']
    nodes = db.execute(
        "SELECT id, path, title, level FROM nodes WHERE orphaned_at IS NULL"
    ).fetchall()
    palette = ["#e3f2fd", "#bbdefb", "#90caf9", "#64b5f6", "#42a5f5"]
    focus_id: str | None = None
    if highlight:
        row = db.execute(
            "SELECT id FROM nodes WHERE path = ? AND orphaned_at IS NULL", (highlight,)
        ).fetchone()
        if row:
            focus_id = row[0]
    focus_set: set[str] = set()
    if focus_id:
        focus_set.add(focus_id)
        for (dst,) in db.execute(
            "SELECT dst_id FROM edges WHERE src_id = ? AND expired_at IS NULL",
            (focus_id,),
        ).fetchall():
            focus_set.add(dst)
        for (src,) in db.execute(
            "SELECT src_id FROM edges WHERE dst_id = ? AND expired_at IS NULL",
            (focus_id,),
        ).fetchall():
            focus_set.add(src)
    for (nid, path, title, level) in nodes:
        color = palette[min(level, len(palette) - 1)]
        label = (title or path).replace('"', "'")
        dim = focus_id is not None and nid not in focus_set
        opts = f'fillcolor="{color}"'
        if dim:
            opts += ', fontcolor="#bbbbbb", color="#dddddd"'
        if nid == focus_id:
            opts += ', penwidth=3, color="#d81b60"'
        lines.append(f'  "{nid}" [label="{label}", {opts}];')
    for (src, dst, kind) in db.execute(
        "SELECT src_id, dst_id, kind FROM edges WHERE expired_at IS NULL"
    ).fetchall():
        style = "solid" if kind == "child" else "dashed"
        color = {"child": "#555555", "see_also": "#1e88e5", "alias_of": "#43a047"}.get(
            kind, "#999999"
        )
        dim = focus_id is not None and (src not in focus_set or dst not in focus_set)
        if dim:
            color = "#dddddd"
        lines.append(f'  "{src}" -> "{dst}" [style={style}, color="{color}"];')
    lines.append("}")
    return "\n".join(lines)


# ── Calibrate ─────────────────────────────────────────────────────────────────

def calibrate(db: sqlite3.Connection, labeled: list[dict[str, Any]]) -> dict[str, Any]:
    """Fit LOW/ABSTAIN thresholds from a labeled dataset.

    Inputs: labeled = [{query, expected_path, ...}, ..., {query, abstain: true}].
    ABSTAIN is fit from the gap between max out-of-distribution score and min
    correct score. LOW is fit by descending threshold sweep: first threshold
    where precision (top-hit = expected_path) hits 0.9 with a minimum sample
    population. Separating the two fits avoids the single-sample fluke that
    pins LOW too strictly at high thresholds.
    """
    real_samples: list[tuple[float, bool]] = []
    ood_scores: list[float] = []
    for item in labeled:
        q = item["query"]
        result = retrieve(db, q, k=3, abstain_threshold=0.0)
        confidence = float(result.get("confidence", 0.0))
        if item.get("abstain"):
            ood_scores.append(confidence)
            continue
        if "expected_path" not in item:
            continue
        if result["results"]:
            top = result["results"][0]
            is_correct = top["path"] == item["expected_path"]
            real_samples.append((confidence, is_correct))

    if not real_samples:
        return {"ok": False, "reason": "no samples"}

    # ABSTAIN — floor. Just above max OOD (so all OOD queries abstain) and
    # safely below min correct (so real queries pass). If no OOD provided,
    # leave at the default.
    if ood_scores:
        max_ood = max(ood_scores)
        correct_scores = [c for c, ok in real_samples if ok]
        min_correct = min(correct_scores) if correct_scores else max_ood + 0.1
        abstain = min(
            round(min_correct - 0.01, 2),
            round(max_ood + 0.03, 2),
        )
        abstain = max(0.0, min(abstain, 0.9))
    else:
        abstain = DEFAULT_ABSTAIN_THRESHOLD

    # LOW — ceiling. Descending sweep for the first threshold with precision
    # ≥ 0.9 and enough samples to be meaningful.
    real_samples.sort(key=lambda s: s[0], reverse=True)
    min_samples_for_threshold = max(3, len(real_samples) // 10)
    low = DEFAULT_LOW_THRESHOLD
    lower_bound = int(round((abstain + 0.05) * 100))
    for thresh in [i / 100.0 for i in range(75, lower_bound, -5)]:
        kept = [s for s in real_samples if s[0] >= thresh]
        if len(kept) < min_samples_for_threshold:
            continue
        precision = sum(1 for _, c in kept if c) / len(kept)
        if precision >= 0.9:
            low = thresh
            break

    db.execute(
        """
        INSERT INTO calibration (calibrated_at, low_threshold, abstain_threshold, sample_count, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            _utc_iso(),
            low,
            abstain,
            len(real_samples) + len(ood_scores),
            f"precision-sweep; real={len(real_samples)} ood={len(ood_scores)}",
        ),
    )
    db.commit()
    return {
        "ok": True,
        "low_threshold": low,
        "abstain_threshold": abstain,
        "samples": len(real_samples),
        "ood_samples": len(ood_scores),
    }


# ── Benchmark ─────────────────────────────────────────────────────────────────

def benchmark(
    db: sqlite3.Connection,
    dataset: list[dict[str, Any]],
    *,
    k: int = 5,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    abstain_threshold: float = DEFAULT_ABSTAIN_THRESHOLD,
    use_see_also: bool = True,
    use_abstain: bool = True,
    use_fts: bool = DEFAULT_USE_FTS,
    wrong_confident_score: float = 0.65,
) -> dict[str, Any]:
    """Run dataset queries, compute recall@k, MRR@k, per-tag breakdown, latency.

    Every non-abstain result feeds per-tag buckets so single/multi/cross-branch/
    adversarial/ambiguous can be read independently. Abstain queries split into
    abstain-far vs abstain-near depending on item tag. Threshold + ablation
    params flow through to retrieve() so the same dataset can be re-scored
    under different configurations.
    """
    import time as _time

    n = len(dataset)
    if n == 0:
        return {"error": "empty dataset"}

    by_tag: dict[str, dict[str, Any]] = {}
    recall_at_k = 0
    mrr_sum = 0.0
    abstain_correct = 0
    abstain_total = 0
    wrong_confident = 0
    latencies: list[float] = []

    for item in dataset:
        q = item["query"]
        expected = item.get("expected_paths") or ([item["expected_path"]] if item.get("expected_path") else [])
        tag = item.get("tag", "abstain" if item.get("abstain") else "single")
        expect_abstain = bool(item.get("abstain"))

        bucket = by_tag.setdefault(tag, {"n": 0, "hits": 0, "mrr": 0.0,
                                         "abstain_correct": 0, "wrong_confident": 0})
        bucket["n"] += 1

        t0 = _time.monotonic()
        result = retrieve(
            db, q, k=k,
            low_threshold=low_threshold,
            abstain_threshold=abstain_threshold,
            use_see_also=use_see_also,
            use_abstain=use_abstain,
            use_fts=use_fts,
        )
        latencies.append(_time.monotonic() - t0)

        returned = [r["path"] for r in result["results"]]
        hit = any(p in returned for p in expected) if expected else False
        if hit:
            recall_at_k += 1
            bucket["hits"] += 1
            for idx, r in enumerate(result["results"]):
                if r["path"] in expected:
                    reciprocal = 1.0 / (idx + 1)
                    mrr_sum += reciprocal
                    bucket["mrr"] += reciprocal
                    break
        if expect_abstain:
            abstain_total += 1
            if result["fell_back"]:
                abstain_correct += 1
                bucket["abstain_correct"] += 1
        elif result["results"] and result["confidence"] >= wrong_confident_score and not hit:
            wrong_confident += 1
            bucket["wrong_confident"] += 1

    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0

    tag_report = {}
    for tag, s in by_tag.items():
        entry = {"n": s["n"]}
        if s["n"] > 0 and not tag.startswith("abstain"):
            entry["recall_at_k"] = round(s["hits"] / s["n"], 3)
            entry["mrr_at_k"] = round(s["mrr"] / s["n"], 3)
            entry["wrong_confident"] = s["wrong_confident"]
        elif tag.startswith("abstain"):
            entry["abstain_accuracy"] = round(s["abstain_correct"] / s["n"], 3) if s["n"] else None
        tag_report[tag] = entry

    return {
        "n": n,
        "recall_at_k": round(recall_at_k / n, 3),
        "mrr_at_k": round(mrr_sum / n, 3),
        "abstain_accuracy": round(abstain_correct / abstain_total, 3) if abstain_total else None,
        "wrong_confident_rate": round(wrong_confident / n, 3),
        "latency_p50_ms": round(p50 * 1000, 1),
        "latency_p95_ms": round(p95 * 1000, 1),
        "by_tag": tag_report,
        "config": {
            "k": k,
            "low_threshold": low_threshold,
            "abstain_threshold": abstain_threshold,
            "use_see_also": use_see_also,
            "use_abstain": use_abstain,
            "use_fts": use_fts,
        },
    }


def benchmark_ablation(
    db: sqlite3.Connection,
    dataset: list[dict[str, Any]],
    *,
    k: int = 5,
    low_threshold: float = DEFAULT_LOW_THRESHOLD,
    abstain_threshold: float = DEFAULT_ABSTAIN_THRESHOLD,
) -> dict[str, Any]:
    """Run the same dataset under four variants so we can read the marginal
    value of each retrieval phase:

        V0  — flat only, no see_also, no abstain (baseline)
        V1  — flat + abstain only
        V2  — flat + see_also only
        V3  — full (current production behavior)
    """
    variants = [
        ("V0_flat_only", False, False),
        ("V1_flat_abstain", False, True),
        ("V2_flat_seealso", True, False),
        ("V3_full", True, True),
    ]
    out = {}
    for name, use_sa, use_ab in variants:
        out[name] = benchmark(
            db, dataset,
            k=k,
            low_threshold=low_threshold,
            abstain_threshold=abstain_threshold,
            use_see_also=use_sa,
            use_abstain=use_ab,
        )
    return out


def benchmark_loo(
    db: sqlite3.Connection,
    dataset: list[dict[str, Any]],
    *,
    k: int = 5,
) -> dict[str, Any]:
    """Leave-one-out CV with precomputed retrieval cache.

    Retrieval results depend only on (query, db state) — not on which other
    items are in the dataset. So we cache each query's retrieve() output once,
    then LOO becomes pure arithmetic: re-fit thresholds from N-1 cached
    (confidence, correct) pairs, evaluate the held-out one with those
    thresholds.

    Thresholds never see the item they're scoring — the honest generalization
    estimate. O(N × sweep_size) instead of O(N²) retrievals.
    """
    n = len(dataset)
    if n == 0:
        return {"error": "empty dataset"}

    # Precompute retrieve() once per query. abstain_threshold=0 so we get
    # full results regardless of confidence (we'll apply thresholds in-loop).
    cache: list[dict[str, Any]] = []
    for item in dataset:
        r = retrieve(db, item["query"], k=k, abstain_threshold=0.0)
        expected = item.get("expected_paths") or ([item["expected_path"]] if item.get("expected_path") else [])
        returned = [res["path"] for res in r["results"]]
        hit = any(p in returned for p in expected) if expected else False
        first_rank = next((idx + 1 for idx, res in enumerate(r["results"]) if res["path"] in expected), None)
        top_correct = (r["results"][0]["path"] in expected) if (r["results"] and expected) else False
        cache.append({
            "confidence": r["confidence"],
            "top_correct": top_correct,
            "hit_at_k": hit,
            "first_rank": first_rank,
            "top_path": r["results"][0]["path"] if r["results"] else None,
            "expect_abstain": bool(item.get("abstain")),
            "has_expected": "expected_path" in item or "expected_paths" in item,
        })

    # For each i: fit thresholds from cache minus i, then evaluate cache[i].
    hits = 0
    non_abstain = 0
    abstain_correct = 0
    abstain_total = 0
    wrong_confident = 0
    mrr_sum = 0.0
    low_fits: list[float] = []
    abstain_fits: list[float] = []

    for i in range(n):
        # Build (confidence, top_correct) samples and OOD scores from the
        # cache, skipping index i.
        real_samples = [
            (c["confidence"], c["top_correct"])
            for j, c in enumerate(cache)
            if j != i and c["has_expected"] and not c["expect_abstain"]
        ]
        ood_scores = [
            c["confidence"]
            for j, c in enumerate(cache)
            if j != i and c["expect_abstain"]
        ]
        if not real_samples:
            continue

        # Same logic as calibrate() but pure-arithmetic over the cache.
        if ood_scores:
            max_ood = max(ood_scores)
            correct_scores = [s for s, ok in real_samples if ok]
            min_correct = min(correct_scores) if correct_scores else max_ood + 0.1
            abstain_fit = min(round(min_correct - 0.01, 2), round(max_ood + 0.03, 2))
            abstain_fit = max(0.0, min(abstain_fit, 0.9))
        else:
            abstain_fit = DEFAULT_ABSTAIN_THRESHOLD

        real_sorted = sorted(real_samples, key=lambda s: s[0], reverse=True)
        min_samples = max(3, len(real_sorted) // 10)
        low_fit = DEFAULT_LOW_THRESHOLD
        lower_bound = int(round((abstain_fit + 0.05) * 100))
        for thresh in [t / 100.0 for t in range(75, lower_bound, -5)]:
            kept = [s for s in real_sorted if s[0] >= thresh]
            if len(kept) < min_samples:
                continue
            prec = sum(1 for _, ok in kept if ok) / len(kept)
            if prec >= 0.9:
                low_fit = thresh
                break

        low_fits.append(low_fit)
        abstain_fits.append(abstain_fit)

        # Evaluate cache[i] under these fitted thresholds.
        c = cache[i]
        if c["expect_abstain"]:
            abstain_total += 1
            if c["confidence"] < abstain_fit:
                abstain_correct += 1
            continue
        non_abstain += 1
        # Abstain gate kills recall; the item only counts as a hit if the
        # gate lets it through AND it was in top-k.
        if c["confidence"] >= abstain_fit and c["hit_at_k"]:
            hits += 1
            if c["first_rank"]:
                mrr_sum += 1.0 / c["first_rank"]
        elif c["confidence"] >= 0.65 and not c["hit_at_k"]:
            wrong_confident += 1

    mean_low = sum(low_fits) / len(low_fits) if low_fits else 0.0
    mean_ab = sum(abstain_fits) / len(abstain_fits) if abstain_fits else 0.0
    return {
        "n": n,
        "non_abstain_evaluated": non_abstain,
        "recall_at_k_loo": round(hits / non_abstain, 3) if non_abstain else None,
        "mrr_at_k_loo": round(mrr_sum / non_abstain, 3) if non_abstain else None,
        "abstain_accuracy_loo": round(abstain_correct / abstain_total, 3) if abstain_total else None,
        "wrong_confident_rate_loo": round(wrong_confident / non_abstain, 3) if non_abstain else None,
        "low_threshold_fit_mean": round(mean_low, 3),
        "low_threshold_fit_stddev": round(
            (sum((x - mean_low) ** 2 for x in low_fits) / len(low_fits)) ** 0.5, 3
        ) if low_fits else 0.0,
        "abstain_threshold_fit_mean": round(mean_ab, 3),
        "abstain_threshold_fit_stddev": round(
            (sum((x - mean_ab) ** 2 for x in abstain_fits) / len(abstain_fits)) ** 0.5, 3
        ) if abstain_fits else 0.0,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="memory_tree", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="Walk vault, upsert nodes + edges")
    p_build.add_argument(
        "--force-resync", "--rebuild",
        action="store_true",
        dest="force_resync",
        help="EMERGENCY: orphan all active nodes + drop embeddings, then re-upsert from walk. Normal flow should never invoke this — use `check --auto-fix` or reembed instead. The 50%% safety abort gate still applies unless --force is passed.",
    )
    p_build.add_argument("--skip-embed", action="store_true", help="Skip embedding API calls (structure only)")
    p_build.add_argument("--force", action="store_true", help="Bypass force-resync safety abort when walk retains under half of current active rows")

    p_query = sub.add_parser("query", help="Retrieve top nodes for a query")
    p_query.add_argument("text", help="Query text")
    p_query.add_argument("-k", type=int, default=DEFAULT_TOP_K)
    p_query.add_argument("--json", action="store_true")
    p_query.add_argument("--low", type=float, default=DEFAULT_LOW_THRESHOLD)
    p_query.add_argument("--abstain", type=float, default=DEFAULT_ABSTAIN_THRESHOLD)
    p_query.add_argument(
        "--policy", action="store_true",
        help="Opt into retrieve_with_policy (persona gating / retry / hints / adaptive-K). "
             "Raw retrieve is the default as of 2026-04-18 after bench showed --no-policy "
             "at 0.944 vs --policy at 0.844 on the 90-item fixture.",
    )
    p_query.add_argument(
        "--raw", action="store_true",
        help="[deprecated] No-op kept for backward compatibility — raw retrieve is now the default.",
    )
    p_query.add_argument("--no-fts", action="store_true", help="Disable FTS5 hybrid search")

    p_reembed = sub.add_parser("reembed", help="Re-embed a single file")
    p_reembed.add_argument("path", help="Relative path from vault root")

    p_check = sub.add_parser("check", help="Report coverage gaps + graph issues")
    p_check.add_argument("--coverage", action="store_true")
    p_check.add_argument("--json", action="store_true")
    p_check.add_argument(
        "--auto-fix",
        action="store_true",
        help="Discover new files, orphan missing files, re-embed stale files (idempotent)",
    )

    p_graph = sub.add_parser("graph", help="Emit GraphViz dot of the tree")
    p_graph.add_argument("--highlight", help="Relative path to highlight")
    p_graph.add_argument("-o", "--output", help="Write to file instead of stdout")

    p_calib = sub.add_parser("calibrate", help="Fit thresholds from labeled data")
    p_calib.add_argument("labeled_jsonl", help="Path to labeled dataset (JSONL)")

    sub.add_parser("manifest", help="Generate thin manifest from all indexed nodes")

    p_ext = sub.add_parser("reindex-external", help="Index auto-memory files from DEUS_AUTO_MEMORY_DIR")
    p_ext.add_argument("--skip-embed", action="store_true", help="Skip embedding API calls")
    p_ext.add_argument("--json", action="store_true")

    p_bench = sub.add_parser("benchmark", help="Run benchmark on labeled dataset")
    p_bench.add_argument("dataset_jsonl", help="Path to JSONL benchmark dataset")
    p_bench.add_argument("-k", type=int, default=5)
    p_bench.add_argument("--json", action="store_true")
    p_bench.add_argument("--low", type=float, default=DEFAULT_LOW_THRESHOLD)
    p_bench.add_argument("--abstain", type=float, default=DEFAULT_ABSTAIN_THRESHOLD)
    p_bench.add_argument("--ablation", action="store_true", help="Run V0/V1/V2/V3/V4 variants side by side")
    p_bench.add_argument("--loo", action="store_true", help="Leave-one-out CV (honest generalization estimate)")
    p_bench.add_argument("--no-fts", action="store_true", help="Disable FTS5 hybrid search")

    args = parser.parse_args(argv)
    db = open_db()
    vault = resolve_vault_path()

    if args.cmd == "build":
        if "--rebuild" in (argv if argv is not None else sys.argv):
            print(
                "WARN: --rebuild is deprecated; use --force-resync. "
                "Intent is unchanged (emergency full resync).",
                file=sys.stderr,
            )
        try:
            counts = build_tree(
                vault, db,
                rebuild=args.force_resync,
                skip_embed=args.skip_embed,
                force=args.force,
            )
        except ValueError as exc:
            print(f"ABORT: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(counts, indent=2))
        return 0

    if args.cmd == "query":
        if args.policy:
            result = retrieve_with_policy(
                db, args.text, k=args.k,
                low_threshold=args.low, abstain_threshold=args.abstain,
            )
        else:
            result = retrieve(
                db, args.text, k=args.k,
                low_threshold=args.low, abstain_threshold=args.abstain,
                use_fts=not args.no_fts,
            )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            for r in result["results"]:
                route = r.get("route") or "policy"
                print(f"{r['score']:.3f}  {route:<20}  {r['path']}")
            extra = ""
            if "policy_trace" in result and result["policy_trace"]:
                extra = f" policy={','.join(result['policy_trace'])}"
            print(f"— confidence={result['confidence']:.3f} fell_back={result['fell_back']}{extra}")
        # Exit code: 0 when a usable result was surfaced, 1 on abstain.
        return 0 if not result["fell_back"] else 1

    if args.cmd == "reembed":
        status = reembed_file(vault, args.path, db)
        print(status)
        return 0

    if args.cmd == "check":
        if args.auto_fix:
            fix_counts = autofix_tree(db, vault)
            if args.json:
                print(json.dumps(fix_counts, indent=2))
            else:
                print(
                    f"autofix: discovered={fix_counts['discovered']} "
                    f"orphaned={fix_counts['orphaned']} "
                    f"reembedded={fix_counts['reembedded']} "
                    f"skipped={fix_counts['skipped']}"
                )
        report = check_tree(db, vault)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"ok={report['ok']} nodes={report.get('nodes_active', 0)}")
            for issue in report["issues"]:
                print(f"  - {issue}")
        return 0 if report["ok"] else 1

    if args.cmd == "graph":
        dot = render_graph(db, highlight=args.highlight)
        if args.output:
            Path(args.output).write_text(dot)
        else:
            print(dot)
        return 0

    if args.cmd == "manifest":
        print(generate_manifest(db))
        return 0

    if args.cmd == "reindex-external":
        ext_dir = os.environ.get(EXTERNAL_DIR_ENV)
        if not ext_dir:
            print(f"ABORT: {EXTERNAL_DIR_ENV} not set", file=sys.stderr)
            return 2
        try:
            counts = reindex_external(db, Path(ext_dir), skip_embed=args.skip_embed)
        except (FileNotFoundError, ValueError) as exc:
            print(f"ABORT: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(counts, indent=2))
        else:
            print(
                f"reindex-external: indexed={counts['indexed']} "
                f"embedded={counts['embedded']} "
                f"orphaned={counts['orphaned']} "
                f"id_written={counts['id_written']} "
                f"skipped={counts['skipped']}"
            )
        return 0

    if args.cmd == "calibrate":
        data = [json.loads(l) for l in Path(args.labeled_jsonl).read_text().splitlines() if l.strip()]
        print(json.dumps(calibrate(db, data), indent=2))
        return 0

    if args.cmd == "benchmark":
        data = [json.loads(l) for l in Path(args.dataset_jsonl).read_text().splitlines() if l.strip()]
        if args.ablation:
            report = benchmark_ablation(db, data, k=args.k, low_threshold=args.low, abstain_threshold=args.abstain)
        elif args.loo:
            report = benchmark_loo(db, data, k=args.k)
        else:
            report = benchmark(db, data, k=args.k, low_threshold=args.low, abstain_threshold=args.abstain, use_fts=not args.no_fts)
        print(json.dumps(report, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
