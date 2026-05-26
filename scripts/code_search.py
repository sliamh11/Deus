#!/usr/bin/env python3
"""code_search.py — semantic code search with sqlite-vec + Ollama + RRF fusion.

Follows the memory_tree.py pattern: sqlite-vec for vector storage, FTS5 for
BM25, Reciprocal Rank Fusion to combine both signals. Incremental indexing
via stat()+mtime and content SHA-256.

Subcommands: reindex | search | status

DB path: ~/.deus/code_search.db (override via DEUS_CODE_SEARCH_DB).
Embedding: reuses evolution.providers.embeddings (Ollama embeddinggemma 768d).

See docs/decisions/no-db-deletion.md (soft-delete only) and
docs/decisions/evolution-db-split.md (separate DB file per subsystem).
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import re
import secrets
import sqlite3
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    import sqlite_vec
except ImportError:
    sqlite_vec = None

EMBED_DIM = 768
DB_PATH = Path(os.environ.get(
    "DEUS_CODE_SEARCH_DB", "~/.deus/code_search.db"
)).expanduser()

DEFAULT_TOP_K = 10
DEFAULT_RRF_K = int(os.environ.get("DEUS_CODE_SEARCH_RRF_K", "60"))

TYPE_WEIGHT: dict[str, float] = {
    "function": 1.0, "method": 1.0, "class": 0.9,
    "interface": 0.85, "impl": 0.85, "block": 0.6, "module": 0.4,
}
DOC_EXTENSIONS = frozenset({".md", ".yaml", ".yml", ".toml", ".html", ".css"})
DOC_PENALTY = float(os.environ.get("DEUS_CODE_SEARCH_DOC_PENALTY", "0.7"))

SUPPORTED_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".java", ".c", ".cpp",
    ".h", ".hpp", ".cs", ".rb", ".sh", ".bash", ".zsh", ".lua", ".swift",
    ".kt", ".scala", ".r", ".sql", ".md", ".yaml", ".yml", ".toml",
    ".css", ".scss", ".html", ".vue", ".svelte", ".astro", ".mjs", ".cjs",
})

IGNORE_DIRS = frozenset({
    "node_modules", ".git", "dist", "build", ".next", "__pycache__",
    ".mypy_cache", ".pytest_cache", "coverage", ".nyc_output",
    "vendor", ".venv", "venv", ".tox", "target", ".gradle",
    ".idea", ".vscode", "worktrees", ".claude",
    "container", "groups", ".husky",
})

IGNORE_FILES = frozenset({
    ".DS_Store", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
})

EMBED_WORKERS = int(os.environ.get("DEUS_CODE_SEARCH_WORKERS", "4"))

SLIDING_WINDOW_LINES = 200
SLIDING_OVERLAP_LINES = 50


# ── ID + hashing ──────────────────────────────────────────────────────────────

def _make_id() -> str:
    ts_ms = int(time.time() * 1000).to_bytes(6, "big")
    rand = secrets.token_bytes(10)
    return (ts_ms + rand).hex()


def _content_hash(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ── Vector helpers ────────────────────────────────────────────────────────────

def _serialize(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _embed_text(text: str) -> list[float]:
    from evolution.providers.embeddings import embed as _embed
    return _embed(text)


def _embed_batch(texts: list[str]) -> list[list[float]]:
    # Thread-safe: each call creates its own HTTP request to Ollama (no shared session state)
    from evolution.providers.embeddings import embed_batch as _eb
    return _eb(texts)


def _embed_available() -> bool:
    try:
        _embed_text("test")
        return True
    except Exception:
        return False


# ── RRF fusion ────────────────────────────────────────────────────────────────

def _rrf_fuse(
    vec_ranked: list[tuple[int, int]],
    fts_ranked: list[tuple[int, int]],
    k_rrf: int = 60,
    top: int = 10,
    chunk_meta: dict[int, tuple[str, str]] | None = None,
) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for rid, rank in vec_ranked:
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (k_rrf + rank)
    for rid, rank in fts_ranked:
        scores[rid] = scores.get(rid, 0.0) + 1.0 / (k_rrf + rank)
    if chunk_meta:
        for rid in scores:
            ctype, ext = chunk_meta.get(rid, ("block", ""))
            scores[rid] *= TYPE_WEIGHT.get(ctype, 0.6) * (DOC_PENALTY if ext in DOC_EXTENSIONS else 1.0)
    return sorted(scores.items(), key=lambda x: -x[1])[:top]


# ── FTS helpers ───────────────────────────────────────────────────────────────

FTS_STOP_WORDS = frozenset({
    "the", "is", "at", "which", "on", "in", "to", "for", "of", "an",
    "it", "be", "as", "do", "by", "or", "if", "up", "so", "no", "we",
    "my", "me", "am", "are", "was", "has", "had", "how", "its", "can",
    "did", "but", "our", "you", "what", "when", "where", "who", "does",
    "should", "would", "could", "this", "that", "with", "from", "have",
    "there", "been", "were", "they", "them", "will", "about",
    "def", "class", "import", "return", "function", "const", "let", "var",
})


def _fts_escape(query: str) -> str:
    tokens = re.findall(r"[a-zA-Z_][a-zA-Z0-9_]*", query.lower())
    kept = [t for t in tokens if t not in FTS_STOP_WORDS and len(t) >= 2]
    if not kept:
        return query.lower()
    return " AND ".join(f'"{t}"' for t in kept)


def _fts_available(db: sqlite3.Connection) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='chunks_fts'"
    ).fetchone()
    return row is not None


# ── Chunking ──────────────────────────────────────────────────────────────────

def _chunk_python(content: str, file_path: str) -> list[dict[str, Any]]:
    """Extract functions and classes from Python files using stdlib ast."""
    chunks: list[dict[str, Any]] = []
    lines = content.split("\n")
    try:
        tree = ast.parse(content, filename=file_path)
    except SyntaxError:
        return _chunk_sliding_window(content, file_path)

    parent_map: dict[int, ast.AST] = {
        id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            chunk_type = "function"
            name = node.name
            p = parent_map.get(id(node))
            if isinstance(p, ast.ClassDef):
                name = f"{p.name}.{node.name}"
                chunk_type = "method"
            start = node.lineno - 1
            end = node.end_lineno or (start + 1)
            body = "\n".join(lines[start:end])
            chunks.append({
                "chunk_type": chunk_type,
                "chunk_name": name,
                "content": body,
                "chunk_index": len(chunks),
            })
        elif isinstance(node, ast.ClassDef):
            start = node.lineno - 1
            # Just the class signature + docstring, not the full body
            end = start + 1
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.Expr) and isinstance(child.value, ast.Constant):
                    end = child.end_lineno or end
                    break
            body = "\n".join(lines[start:end])
            chunks.append({
                "chunk_type": "class",
                "chunk_name": node.name,
                "content": body,
                "chunk_index": len(chunks),
            })

    if not chunks:
        return _chunk_sliding_window(content, file_path)
    return chunks


# Regex patterns for TypeScript/JavaScript/Rust extraction
_TS_FUNC_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\()",
    re.MULTILINE,
)
_TS_CLASS_RE = re.compile(
    r"^(?:export\s+)?(?:abstract\s+)?class\s+(\w+)",
    re.MULTILINE,
)
_TS_INTERFACE_RE = re.compile(
    r"^(?:export\s+)?(?:interface|type)\s+(\w+)",
    re.MULTILINE,
)
_RUST_FN_RE = re.compile(
    r"^(?:pub(?:\(crate\))?\s+)?(?:async\s+)?fn\s+(\w+)",
    re.MULTILINE,
)
_RUST_IMPL_RE = re.compile(
    r"^impl(?:<[^>]+>)?\s+(\w+)",
    re.MULTILINE,
)


def _find_brace_end(content: str, start: int) -> int:
    """Find the matching closing brace from `start` (which should be at '{')."""
    depth = 0
    i = start
    while i < len(content):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(content)


def _chunk_braced(content: str, file_path: str, patterns: list[tuple[re.Pattern, str]]) -> list[dict[str, Any]]:
    """Extract chunks from brace-delimited languages using regex + brace matching."""
    chunks: list[dict[str, Any]] = []
    for pattern, chunk_type in patterns:
        for m in pattern.finditer(content):
            name = m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else None)
            if not name:
                continue
            # Find the opening brace
            rest = content[m.start():]
            brace_pos = rest.find("{")
            if brace_pos == -1:
                # No brace (type alias, interface without body, etc.)
                end_pos = rest.find(";")
                if end_pos == -1:
                    end_pos = rest.find("\n")
                if end_pos == -1:
                    end_pos = len(rest)
                body = rest[:end_pos + 1]
            else:
                end = _find_brace_end(rest, brace_pos)
                body = rest[:end]
            chunks.append({
                "chunk_type": chunk_type,
                "chunk_name": name,
                "content": body,
                "chunk_index": len(chunks),
            })
    return chunks


def _chunk_typescript(content: str, file_path: str) -> list[dict[str, Any]]:
    patterns = [
        (_TS_FUNC_RE, "function"),
        (_TS_CLASS_RE, "class"),
        (_TS_INTERFACE_RE, "interface"),
    ]
    chunks = _chunk_braced(content, file_path, patterns)
    if not chunks:
        return _chunk_sliding_window(content, file_path)
    return chunks


def _chunk_rust(content: str, file_path: str) -> list[dict[str, Any]]:
    patterns = [
        (_RUST_FN_RE, "function"),
        (_RUST_IMPL_RE, "impl"),
    ]
    chunks = _chunk_braced(content, file_path, patterns)
    if not chunks:
        return _chunk_sliding_window(content, file_path)
    return chunks


def _chunk_sliding_window(content: str, file_path: str) -> list[dict[str, Any]]:
    lines = content.split("\n")
    chunks: list[dict[str, Any]] = []
    step = SLIDING_WINDOW_LINES - SLIDING_OVERLAP_LINES
    for i in range(0, len(lines), step):
        window = lines[i:i + SLIDING_WINDOW_LINES]
        body = "\n".join(window)
        if body.strip():
            chunks.append({
                "chunk_type": "block",
                "chunk_name": f"block_{i}",
                "content": body,
                "chunk_index": len(chunks),
            })
    return chunks or [{"chunk_type": "module", "chunk_name": "module", "content": content, "chunk_index": 0}]


def chunk_file(content: str, file_path: str) -> list[dict[str, Any]]:
    ext = Path(file_path).suffix.lower()
    if ext == ".py":
        chunks = _chunk_python(content, file_path)
    elif ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".vue", ".svelte"}:
        chunks = _chunk_typescript(content, file_path)
    elif ext == ".rs":
        chunks = _chunk_rust(content, file_path)
    else:
        chunks = _chunk_sliding_window(content, file_path)
    return [c for c in chunks if c.get("content", "").strip()]


# ── File discovery ────────────────────────────────────────────────────────────

def _should_ignore(rel: Path) -> bool:
    if rel.name in IGNORE_FILES:
        return True
    return any(p in IGNORE_DIRS for p in rel.parts)


def discover_files(directory: Path) -> list[Path]:
    """Discover source files to index. Uses git ls-files if available."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=str(directory), capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0:
            files = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                p = directory / line
                if not p.is_file():
                    continue
                rel = Path(line)
                if _should_ignore(rel):
                    continue
                if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                    files.append(p)
            return sorted(files)
    except Exception:
        pass

    # Fallback: rglob
    files: list[Path] = []
    for p in directory.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(directory)
        if _should_ignore(rel):
            continue
        if p.suffix.lower() in SUPPORTED_EXTENSIONS:
            files.append(p)
    return sorted(files)


# ── DB ────────────────────────────────────────────────────────────────────────

def _init_db(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    if sqlite_vec is not None:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)

    db.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            id TEXT NOT NULL UNIQUE,
            file_path TEXT NOT NULL,
            chunk_type TEXT NOT NULL,
            chunk_name TEXT,
            chunk_index INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            mtime REAL NOT NULL,
            embedded_at TEXT,
            orphaned_at TEXT DEFAULT NULL
        )
    """)
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_chunks_active ON chunks(file_path, chunk_type, chunk_index) WHERE orphaned_at IS NULL"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path) WHERE orphaned_at IS NULL")
    db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_hash ON chunks(content_hash)")

    if sqlite_vec is not None:
        db.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec
            USING vec0(embedding float[{EMBED_DIM}])
        """)

    try:
        db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
            USING fts5(chunk_name, content, tokenize='porter unicode61')
        """)
    except sqlite3.OperationalError:
        pass  # FTS5 unavailable

    db.execute("""
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    db.commit()
    return db


# ── Indexing ──────────────────────────────────────────────────────────────────

def _index_file(
    db: sqlite3.Connection,
    file_path: Path,
    base_dir: Path,
    embed: bool = True,
) -> tuple[int, int]:
    """Index a single file. Returns (chunks_added, chunks_skipped)."""
    rel_path = str(file_path.relative_to(base_dir))
    stat = file_path.stat()
    mtime = stat.st_mtime

    # Check if file content has changed
    existing = db.execute(
        "SELECT content_hash FROM chunks WHERE file_path = ? AND orphaned_at IS NULL LIMIT 1",
        (rel_path,),
    ).fetchone()

    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0, 0

    file_hash = _content_hash(content)
    if existing and existing[0] == file_hash:
        return 0, 1  # unchanged

    # Soft-delete old chunks for this file
    db.execute(
        "UPDATE chunks SET orphaned_at = ? WHERE file_path = ? AND orphaned_at IS NULL",
        (time.strftime("%Y-%m-%dT%H:%M:%S"), rel_path),
    )

    chunks = chunk_file(content, rel_path)
    if not chunks:
        return 0, 0

    rowids: list[int] = []
    texts: list[str] = []

    for chunk in chunks:
        cid = _make_id()
        cur = db.execute(
            """INSERT INTO chunks (id, file_path, chunk_type, chunk_name, chunk_index,
               content, content_hash, mtime, embedded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)""",
            (cid, rel_path, chunk["chunk_type"], chunk.get("chunk_name"),
             chunk["chunk_index"], chunk["content"], file_hash, mtime),
        )
        rid = cur.lastrowid
        rowids.append(rid)
        texts.append(chunk["content"][:4000])  # cap embedding input

        if _fts_available(db):
            db.execute(
                "INSERT INTO chunks_fts (rowid, chunk_name, content) VALUES (?, ?, ?)",
                (rid, chunk.get("chunk_name", ""), chunk["content"][:8000]),
            )

    # Batch embed if Ollama is available
    if embed and sqlite_vec is not None and texts:
        try:
            vectors = _embed_batch(texts)
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            for rid, vec in zip(rowids, vectors):
                db.execute(
                    "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                    (rid, _serialize(vec)),
                )
                db.execute(
                    "UPDATE chunks SET embedded_at = ? WHERE rowid = ?",
                    (now, rid),
                )
        except Exception as exc:
            print(f"WARNING: embedding failed for {rel_path}: {exc}", file=sys.stderr)

    return len(chunks), 0


def _embed_worker(batch: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Called from thread pool."""
    return _embed_batch(batch)


def reindex(directory: str, diff_ref: str | None = None) -> dict[str, Any]:
    """Full or incremental reindex with parallel embedding."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    base = Path(directory).resolve()
    if not base.is_dir():
        return {"error": f"Not a directory: {directory}"}

    db = _init_db()

    if diff_ref:
        if not re.match(r'^[a-zA-Z0-9_.^~/-]+$', diff_ref):
            return {"error": f"Invalid git ref: {diff_ref}"}
        result = subprocess.run(
            ["git", "diff", "--name-only", diff_ref],
            cwd=str(base), capture_output=True, text=True,
        )
        changed = [base / f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        files = [f for f in changed if f.exists() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
    else:
        files = discover_files(base)

    total_added = 0
    total_skipped = 0

    # Phase 1: chunk all files and insert into DB (no embedding yet)
    pending_embeds: list[tuple[int, str]] = []  # (rowid, text)

    for f in files:
        try:
            added, skipped = _index_file(db, f, base, embed=False)
            total_added += added
            total_skipped += skipped
        except Exception as exc:
            print(f"WARNING: failed to index {f}: {exc}", file=sys.stderr)

    # Collect un-embedded chunks
    if sqlite_vec is not None:
        rows = db.execute(
            "SELECT rowid, content FROM chunks WHERE embedded_at IS NULL AND orphaned_at IS NULL"
        ).fetchall()
        pending_embeds = [(rid, content[:4000]) for rid, content in rows]

    # Phase 2: parallel embedding
    embed = _embed_available()
    if not embed:
        print("WARNING: Ollama unavailable, indexing without embeddings", file=sys.stderr)
    elif pending_embeds:
        batch_size = 8
        batches: list[list[tuple[int, str]]] = []
        for i in range(0, len(pending_embeds), batch_size):
            batches.append(pending_embeds[i:i + batch_size])

        embedded_count = 0
        with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as pool:
            futures = {
                pool.submit(_embed_worker, [text for _, text in batch]): batch
                for batch in batches
            }
            for future in as_completed(futures):
                batch = futures[future]
                try:
                    vectors = future.result()
                    now = time.strftime("%Y-%m-%dT%H:%M:%S")
                    for (rid, _text), vec in zip(batch, vectors):
                        db.execute(
                            "INSERT INTO chunks_vec (rowid, embedding) VALUES (?, ?)",
                            (rid, _serialize(vec)),
                        )
                        db.execute(
                            "UPDATE chunks SET embedded_at = ? WHERE rowid = ?",
                            (now, rid),
                        )
                    embedded_count += len(batch)
                    if embedded_count % 100 == 0:
                        print(f"  embedded {embedded_count}/{len(pending_embeds)} chunks...", file=sys.stderr)
                        db.commit()  # periodic commit for crash safety
                except Exception as exc:
                    print(f"WARNING: embedding batch failed: {exc}", file=sys.stderr)

    # Store metadata
    db.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        ("last_indexed_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
    )
    db.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        ("indexed_directory", str(base)),
    )
    db.commit()
    db.close()

    return {
        "files_scanned": len(files),
        "chunks_added": total_added,
        "chunks_unchanged": total_skipped,
        "embeddings": "yes" if embed else "no (Ollama unavailable)",
    }


# ── Search ────────────────────────────────────────────────────────────────────

def search(
    query: str,
    k: int = DEFAULT_TOP_K,
    abstain_threshold: float = 0.0,
    gap_threshold: float = 0.0,
    min_confidence: float = 0.0,
) -> list[dict[str, Any]]:
    """Semantic + BM25 search with weighted RRF fusion."""
    if not DB_PATH.exists():
        return [{"error": "No index. Run: code_search.py reindex <directory>"}]

    db = _init_db()
    results: list[dict[str, Any]] = []

    # Vector search (rowid-based)
    vec_candidates: list[tuple[int, int]] = []
    try:
        qvec = _embed_text(query)
        if sqlite_vec is not None:
            rows = db.execute(
                """SELECT rowid, distance
                    FROM chunks_vec
                    WHERE embedding MATCH ?
                    ORDER BY distance
                    LIMIT ?""",
                (_serialize(qvec), k * 3),
            ).fetchall()
            vec_candidates = [(rowid, rank + 1) for rank, (rowid, _dist) in enumerate(rows)]
    except Exception as exc:
        print(f"WARNING: vector search failed: {exc}", file=sys.stderr)
        vec_candidates = []

    # FTS search (rowid-based)
    fts_candidates: list[tuple[int, int]] = []
    if _fts_available(db):
        fts_query = _fts_escape(query)
        try:
            rows = db.execute(
                """SELECT rowid, rank FROM chunks_fts
                   WHERE chunks_fts MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, k * 3),
            ).fetchall()
            fts_candidates = [(rowid, rank + 1) for rank, (rowid, _score) in enumerate(rows)]
        except Exception:
            pass

    # Batch-filter orphaned chunks and fetch metadata for type-weighting
    all_candidate_rowids = {rid for rid, _ in vec_candidates} | {rid for rid, _ in fts_candidates}
    active_set: set[int] = set()
    chunk_meta: dict[int, tuple[str, str]] = {}
    if all_candidate_rowids:
        placeholders = ",".join("?" * len(all_candidate_rowids))
        active_rows = db.execute(
            f"SELECT rowid, chunk_type, file_path FROM chunks WHERE rowid IN ({placeholders}) AND orphaned_at IS NULL",
            list(all_candidate_rowids),
        ).fetchall()
        for rid, ctype, fpath in active_rows:
            active_set.add(rid)
            chunk_meta[rid] = (ctype, Path(fpath).suffix.lower())

    vec_ranked = [(rid, rank) for rid, rank in vec_candidates if rid in active_set]
    fts_ranked = [(rid, rank) for rid, rank in fts_candidates if rid in active_set]

    # Fuse with type/extension weighting
    if vec_ranked or fts_ranked:
        fused = _rrf_fuse(vec_ranked, fts_ranked, k_rrf=DEFAULT_RRF_K, top=k, chunk_meta=chunk_meta)
    else:
        fused = []

    # Abstain gate
    if fused and abstain_threshold > 0.0 and fused[0][1] < abstain_threshold:
        db.close()
        return []

    # Gap-threshold truncation: cut after a large score cliff (keep high-confidence cluster)
    if gap_threshold > 0.0 and len(fused) > 1:
        cutoff = len(fused)
        for i in range(len(fused) - 1):
            if fused[i][1] - fused[i + 1][1] > gap_threshold:
                cutoff = i + 1
                break
        fused = fused[:cutoff]

    # Compute confidence: score normalized to theoretical max (rank-1 in both lists)
    max_score = 2.0 / (DEFAULT_RRF_K + 1)

    # Fetch chunk details
    for rid, score in fused:
        confidence = score / max_score if max_score > 0 else 0.0
        if confidence < min_confidence:
            continue
        row = db.execute(
            """SELECT file_path, chunk_type, chunk_name, content
               FROM chunks WHERE rowid = ? AND orphaned_at IS NULL""",
            (rid,),
        ).fetchone()
        if row:
            content = row[3]
            snippet = content[:500] + ("..." if len(content) > 500 else "")
            results.append({
                "file": row[0],
                "type": row[1],
                "name": row[2] or "(anonymous)",
                "snippet": snippet,
                "score": round(score, 6),
                "confidence": round(confidence, 3),
            })

    db.close()
    return results


# ── Status ────────────────────────────────────────────────────────────────────

def status() -> dict[str, Any]:
    if not DB_PATH.exists():
        return {"indexed": False, "message": "No index found"}

    db = _init_db()
    total = db.execute("SELECT COUNT(*) FROM chunks WHERE orphaned_at IS NULL").fetchone()[0]
    embedded = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE orphaned_at IS NULL AND embedded_at IS NOT NULL"
    ).fetchone()[0]
    files = db.execute(
        "SELECT COUNT(DISTINCT file_path) FROM chunks WHERE orphaned_at IS NULL"
    ).fetchone()[0]
    last = db.execute("SELECT value FROM index_meta WHERE key = 'last_indexed_at'").fetchone()
    directory = db.execute("SELECT value FROM index_meta WHERE key = 'indexed_directory'").fetchone()
    db_size = DB_PATH.stat().st_size
    db.close()

    return {
        "indexed": True,
        "total_chunks": total,
        "embedded_chunks": embedded,
        "files": files,
        "last_indexed": last[0] if last else "never",
        "directory": directory[0] if directory else "unknown",
        "db_size_mb": round(db_size / 1024 / 1024, 2),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic code search")
    sub = parser.add_subparsers(dest="command")

    p_reindex = sub.add_parser("reindex", help="Index or re-index a codebase")
    p_reindex.add_argument("directory", nargs="?", default=".")
    p_reindex.add_argument("--diff", help="Git ref for incremental reindex (e.g. HEAD~1)")

    p_search = sub.add_parser("search", help="Search indexed code")
    p_search.add_argument("query", nargs="+")
    p_search.add_argument("-k", type=int, default=DEFAULT_TOP_K)

    sub.add_parser("status", help="Show index status")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    import json

    if args.command == "reindex":
        result = reindex(args.directory, diff_ref=args.diff)
        print(json.dumps(result, indent=2))
    elif args.command == "search":
        query = " ".join(args.query)
        results = search(query, k=args.k)
        print(json.dumps(results, indent=2))
    elif args.command == "status":
        print(json.dumps(status(), indent=2))


if __name__ == "__main__":
    main()
