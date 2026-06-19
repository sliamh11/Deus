#!/usr/bin/env python3
"""code_search.py — semantic code search with sqlite-vec + Ollama + RRF fusion.

Follows the memory_tree.py pattern: sqlite-vec for vector storage, FTS5 for
BM25, Reciprocal Rank Fusion to combine both signals. Incremental indexing
via stat()+mtime and content SHA-256.

Subcommands: reindex | search | status | generate-fixture | benchmark

DB path: ~/.deus/code_search.db (override via DEUS_CODE_SEARCH_DB).
Embedding: reuses evolution.providers.embeddings (Ollama embeddinggemma 768d).

See docs/decisions/no-db-deletion.md (soft-delete only) and
docs/decisions/evolution-db-split.md (separate DB file per subsystem).
"""
from __future__ import annotations

import argparse
import ast
import bisect
import hashlib
import json
import os
import re
import secrets
import shutil
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
# Legacy shared DB (tier-3 fallback). Per-project resolution lives in
# _resolve_db_path(); DB_PATH stays a stable constant so the fallback is
# import-order-safe and patchable in tests (no import-time env read).
DB_PATH = Path("~/.deus/code_search.db").expanduser()


def _main_worktree_root(root: Path) -> Path:
    """Map a linked git worktree (``.git`` is a *file*) to its canonical main-repo
    root, so the code-search DB keys to a stable path instead of the ephemeral
    worktree that strands the index on cleanup (LIA-189). No-op for the main
    worktree, a non-git dir, a submodule, or any git failure.
    """
    if not (root / ".git").is_file():
        return root  # main worktree (.git dir) or non-git — nothing to normalize
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return root
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        return root
    common = Path(out)
    common = common.resolve() if common.is_absolute() else (root / common).resolve()
    # A worktree's common dir is "<main>/.git" → its parent is the canonical
    # root. Exclude submodules (common dir basename != ".git").
    if common.name != ".git":
        return root
    main_root = common.parent
    return main_root if main_root.is_dir() else root


def _project_root(start: Path | str | None = None) -> Path | None:
    """Nearest .git/.deus ancestor of ``start`` (default cwd); None if neither.

    A linked git worktree ancestor is normalized to its canonical main-repo root
    (:func:`_main_worktree_root`) so every consumer — :func:`_resolve_db_path`
    and :func:`_migrate_legacy_if_match` — keys to one stable per-project DB
    regardless of which worktree the call ran in (LIA-189)."""
    p = Path(start).resolve() if start is not None else Path.cwd().resolve()
    for candidate in (p, *p.parents):
        if (candidate / ".git").exists() or (candidate / ".deus").exists():
            return _main_worktree_root(candidate)
    return None


def _resolve_db_path(project_dir: Path | str | None = None) -> Path:
    """Resolve the code-search DB for a project (3-tier Strategy).

    1. ``DEUS_CODE_SEARCH_DB`` env override (tests / power users).
    2. Per-project, centralized:
       ``~/.config/deus/projects/<md5(realpath)>/code_search.db`` — md5 matches
       the deus-cmd.sh ``_project_config_path`` convention; non-security path
       keying, so collisions over a handful of project paths are negligible.
    3. Legacy shared DB (kept forever per docs/decisions/no-db-deletion.md).
    """
    override = os.environ.get("DEUS_CODE_SEARCH_DB")
    if override:
        return Path(override).expanduser()
    root = _project_root(project_dir)
    if root is not None:
        digest = hashlib.md5(str(root).encode()).hexdigest()
        return Path("~/.config/deus/projects").expanduser() / digest / "code_search.db"
    return DB_PATH


def _migrate_legacy_if_match(root: Path | None, dbp: Path) -> None:
    """Self-healing one-time copy of the legacy shared DB into a per-project DB.

    Fires only when ``dbp`` is absent, the legacy DB exists, and its stored
    ``indexed_directory`` equals ``root``. Copy (never move) — legacy is kept
    per no-db-deletion.md. Idempotent: once ``dbp`` exists it never refires. In
    practice this matches only the single project the shared DB currently holds.
    """
    if root is None or dbp == DB_PATH or dbp.exists() or not DB_PATH.exists():
        return
    try:
        legacy = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            row = legacy.execute(
                "SELECT value FROM index_meta WHERE key = 'indexed_directory'"
            ).fetchone()
        finally:
            legacy.close()
    except sqlite3.Error:
        return
    if not row or Path(row[0]).resolve() != root:
        return
    dbp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DB_PATH, dbp)

DEFAULT_TOP_K = 10
DEFAULT_RRF_K = int(os.environ.get("DEUS_CODE_SEARCH_RRF_K", "60"))

# Retrieval-confidence threshold below which a query is flagged as likely
# outside the indexed codebase (search() warning) and counted as a correct
# abstain (benchmark()). Re-tuned for the generic-NL calibration from #717: a
# sweep over scripts/tests/fixtures/code_search_bench.jsonl found 0.25 is the
# lowest threshold reaching the 0.85 abstain-accuracy plateau while minimizing
# in-domain false-flags (LIA-204).
CONFIDENCE_ABSTAIN_THRESHOLD = 0.25

# Calibration query set for retrieval_confidence (GH #717).
# Built from NL-shaped queries, not symbol-name self-lookups: querying
# `chunk_name.replace("_"," ")` matches its own chunk at artificially tight
# distances (~0.19-0.53), so real NL queries (~0.44-0.56 even for a correct
# match) were pinned to ~0 confidence. These domain-agnostic questions span the
# real NL-query regime while out-of-domain queries still rank far. English-only;
# the >=20-chunk guard at the call site bounds small-codebase over-confidence.
_GENERIC_NL_CALIBRATION_QUERIES: tuple[str, ...] = (
    "how is authentication handled",
    "where is configuration loaded",
    "what validates user input",
    "how are errors logged and handled",
    "how is the database connection initialized",
    "where are routes or endpoints defined",
    "how does caching work",
    "what handles retries and timeouts",
    "how is data serialized to json",
    "where is the main entry point",
    "how are background jobs scheduled",
    "what parses command line arguments",
    "how is logging configured",
    "where are environment variables read",
    "how is the http server started",
    "what sends notifications or messages",
    "how are files read and written",
    "how is application state persisted",
    "what manages user sessions",
    "how are tests structured",
    "how is the response formatted",
    "where is rate limiting enforced",
    "how are secrets and credentials managed",
    "what handles pagination",
    "how is the cache invalidated",
    "where is input sanitized",
    "how does the retry backoff work",
    "what computes the final score",
    "how are webhook events processed",
    "where is the schema migration logic",
    "how is concurrency controlled",
    "what dispatches incoming events",
    "how is the work queue consumed",
    "where are access permissions checked",
    "how is the embedding generated",
)

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
    # busy_timeout before WAL so create-time writes are covered: post-LIA-189 all
    # worktrees share one canonical DB, so concurrent post-commit reindexes can
    # collide — wait for the lock instead of failing (default 0 → SQLITE_BUSY).
    db.execute("PRAGMA busy_timeout=30000")
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

    global _cal_cache  # declared once here; reset at end of function (LIA-207)

    base = Path(directory).resolve()
    if not base.is_dir():
        return {"error": f"Not a directory: {directory}"}

    dbp = _resolve_db_path(base)
    _migrate_legacy_if_match(_project_root(base), dbp)
    db = _init_db(dbp)

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
    # Label with the canonical project root (LIA-189), not the literal walked
    # dir: it must agree with the DB key (also canonical) and never become a
    # dead worktree pointer. base stays the discovery/--diff source.
    db.execute(
        "INSERT OR REPLACE INTO index_meta (key, value) VALUES (?, ?)",
        ("indexed_directory", str(_project_root(base) or base)),
    )

    # Auto-populate calibration distribution if missing (needed for retrieval_confidence).
    # Built from generic natural-language queries (GH #717) rather than symbol-name
    # self-lookups, so the distribution reflects the real NL-query distance regime.
    # Guarded by `not cal_row` so a higher-quality fixture-generated calibration
    # (generate-fixture) is never overwritten here.
    cal_row = db.execute(
        "SELECT value FROM index_meta WHERE key = 'calibration_distances'"
    ).fetchone()
    calibrated = bool(cal_row)
    if not cal_row and embed and sqlite_vec is not None:
        chunk_count = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE orphaned_at IS NULL AND embedded_at IS NOT NULL"
        ).fetchone()[0]
        if chunk_count >= 20:
            cal_distances: list[float] = []
            for query in _GENERIC_NL_CALIBRATION_QUERIES:
                try:
                    emb = _embed_text(query)
                    vec_row = db.execute(
                        "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
                        (_serialize(emb),),
                    ).fetchall()
                    if vec_row:
                        cos = db.execute(
                            "SELECT vec_distance_cosine(embedding, ?) FROM chunks_vec WHERE rowid = ?",
                            (_serialize(emb), vec_row[0][0]),
                        ).fetchone()
                        if cos:
                            cal_distances.append(cos[0])
                except Exception:
                    pass
            if cal_distances:
                cal_distances.sort()
                db.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('calibration_distances', ?)",
                    [json.dumps(cal_distances)],
                )
                calibrated = True
                _cal_cache = None
                print(
                    f"Auto-calibrated: stored {len(cal_distances)} distances "
                    f"from {len(_GENERIC_NL_CALIBRATION_QUERIES)} NL queries",
                    file=sys.stderr,
                )
            else:
                # Every calibration query failed (e.g. Ollama cold-start race):
                # leave calibration empty so retrieval_confidence falls back to
                # the linear map rather than silently storing nothing unnoticed.
                print(
                    f"WARNING: auto-calibration skipped — 0 of "
                    f"{len(_GENERIC_NL_CALIBRATION_QUERIES)} NL queries returned a distance",
                    file=sys.stderr,
                )

    db.commit()
    db.close()

    # Bust the module-level calibration cache so a long-running server doesn't
    # keep serving stale retrieval_confidence after a DELETE+reindex cycle.
    # The next search() reloads calibration_distances from the DB (LIA-207).
    _cal_cache = None

    return {
        "files_scanned": len(files),
        "chunks_added": total_added,
        "chunks_unchanged": total_skipped,
        "embeddings": "yes" if embed else "no (Ollama unavailable)",
        "calibrated": calibrated,
    }


# ── Search ────────────────────────────────────────────────────────────────────

_cal_cache: list[float] | None = None


def _retrieval_confidence(
    top_vec_distance: float,
    cal_distances: list[float] | None,
    has_fts_ranked: bool,
) -> float:
    """Query-level confidence (0-1) from the top vector distance.

    Percentile rank against the calibration distribution: a small distance
    (closer than most calibration queries) → high confidence; a distance beyond
    the calibration range → ~0. Falls back to a linear map when no calibration
    exists. When the result has no FTS support, a sub-0.5 confidence is halved
    (vector-only weak matches are less trustworthy). Pure function — the
    calibration I/O and caching stay in the caller so this is unit-testable
    without an embedding backend (GH #717).
    """
    if cal_distances:
        percentile = bisect.bisect_left(cal_distances, top_vec_distance) / len(cal_distances)
        confidence = round(1.0 - percentile, 3)
    else:
        confidence = round(max(0.0, 1.0 - (top_vec_distance / 2.0)), 3)
    if not has_fts_ranked and confidence < 0.5:
        confidence = round(confidence * 0.5, 3)
    return confidence


def search(
    query: str,
    k: int = DEFAULT_TOP_K,
    gap_threshold: float = 0.0,
    min_confidence: float = 0.0,
    rrf_k: int = DEFAULT_RRF_K,
) -> list[dict[str, Any]]:
    """Semantic + BM25 search with weighted RRF fusion.

    Returns results with retrieval_confidence (0-1): query-level signal
    indicating how well the query matches the indexed codebase. Computed
    via percentile rank against a stored calibration distribution. Below
    0.25 = likely out-of-domain.
    """
    dbp = _resolve_db_path()
    _migrate_legacy_if_match(_project_root(), dbp)
    if not dbp.exists():
        return [{"error": f"No index at {dbp}. Run: code_search.py reindex <directory>"}]

    db = _init_db(dbp)
    results: list[dict[str, Any]] = []
    top_vec_distance: float = 2.0

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
            if rows:
                top_rid = rows[0][0]
                cos_row = db.execute(
                    "SELECT vec_distance_cosine(embedding, ?) FROM chunks_vec WHERE rowid = ?",
                    (_serialize(qvec), top_rid),
                ).fetchone()
                if cos_row:
                    top_vec_distance = cos_row[0]
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
        fused = _rrf_fuse(vec_ranked, fts_ranked, k_rrf=rrf_k, top=k, chunk_meta=chunk_meta)
    else:
        fused = []

    # Compute retrieval confidence via percentile rank against calibration distribution
    global _cal_cache
    if _cal_cache is None:
        cal_row = db.execute(
            "SELECT value FROM index_meta WHERE key = 'calibration_distances'"
        ).fetchone()
        if cal_row:
            _cal_cache = json.loads(cal_row[0])
    if not _cal_cache:
        print("WARNING: no calibration data — run generate-fixture first", file=sys.stderr)
    # bool(fts_ranked): a non-empty list means FTS produced results (load-bearing —
    # vector-only matches with sub-0.5 confidence are down-weighted in the helper).
    retrieval_confidence = _retrieval_confidence(
        top_vec_distance, _cal_cache, bool(fts_ranked)
    )

    # Gap-threshold truncation: cut after a large score cliff (keep high-confidence cluster)
    if gap_threshold > 0.0 and len(fused) > 1:
        cutoff = len(fused)
        for i in range(len(fused) - 1):
            if fused[i][1] - fused[i + 1][1] > gap_threshold:
                cutoff = i + 1
                break
        fused = fused[:cutoff]

    # Compute confidence: score normalized to theoretical max (rank-1 in both lists)
    max_score = 2.0 / (rrf_k + 1)

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
            entry: dict[str, Any] = {
                "file": row[0],
                "type": row[1],
                "name": row[2] or "(anonymous)",
                "snippet": snippet,
                "score": round(score, 6),
                "confidence": round(confidence, 3),
                "retrieval_confidence": retrieval_confidence,
            }
            if top_vec_distance >= 2.0:
                entry["low_confidence_warning"] = "embedding unavailable — confidence unreliable"
            elif retrieval_confidence < CONFIDENCE_ABSTAIN_THRESHOLD:
                entry["low_confidence_warning"] = "query may be outside indexed codebase"
            results.append(entry)

    db.close()
    return results


# ── Status ────────────────────────────────────────────────────────────────────

def status() -> dict[str, Any]:
    dbp = _resolve_db_path()
    _migrate_legacy_if_match(_project_root(), dbp)
    if not dbp.exists():
        return {"indexed": False, "message": "No index found"}

    db = _init_db(dbp)
    total = db.execute("SELECT COUNT(*) FROM chunks WHERE orphaned_at IS NULL").fetchone()[0]
    embedded = db.execute(
        "SELECT COUNT(*) FROM chunks WHERE orphaned_at IS NULL AND embedded_at IS NOT NULL"
    ).fetchone()[0]
    files = db.execute(
        "SELECT COUNT(DISTINCT file_path) FROM chunks WHERE orphaned_at IS NULL"
    ).fetchone()[0]
    last = db.execute("SELECT value FROM index_meta WHERE key = 'last_indexed_at'").fetchone()
    directory = db.execute("SELECT value FROM index_meta WHERE key = 'indexed_directory'").fetchone()
    db_size = dbp.stat().st_size
    db.close()

    directory_val = directory[0] if directory else None
    # A stored indexed_directory that no longer exists signals a stale/broken
    # index (e.g. a pre-fix index keyed to a since-deleted worktree, LIA-189) —
    # surface it loudly instead of silently degrading callers to grep.
    stale = bool(directory_val) and not Path(directory_val).expanduser().exists()

    result: dict[str, Any] = {
        "indexed": True,
        "total_chunks": total,
        "embedded_chunks": embedded,
        "files": files,
        "last_indexed": last[0] if last else "never",
        "directory": directory_val or "unknown",
        "db_size_mb": round(db_size / 1024 / 1024, 2),
        "stale": stale,
    }
    if stale:
        result["message"] = (
            f"indexed_directory {directory_val!r} no longer exists — "
            "index may be stale; re-run reindex"
        )
    return result


# ── Fixture Generation ────────────────────────────────────────────────────────

ABSTAIN_QUERIES_FAR = [
    "kubernetes pod scheduling",
    "React Native navigation stack",
    "Flutter widget lifecycle",
    "Django ORM migrations",
    "AWS Lambda cold start optimization",
    "GraphQL schema stitching",
    "Redis cluster failover",
    "Terraform state locking",
    "iOS SwiftUI animations",
    "Kafka consumer group rebalancing",
]

ABSTAIN_QUERIES_NEAR = [
    "pgvector HNSW index tuning parameters",
    "Weaviate hybrid search configuration",
    "Pinecone serverless index namespacing",
    "cosine similarity threshold selection theory",
    "GGUF quantization format internals",
    "Qdrant collection snapshots for backup",
    "chromadb collection sharding strategy",
    "LangChain output parser retry strategies",
    "Haystack pipeline node custom connectors",
    "MLflow experiment tracking with nested runs",
]


def generate_fixture(
    repo_dir: str | Path,
    output: str | Path | None = None,
    seed: int = 42,
) -> list[dict[str, Any]]:
    """Mine docstrings + git history + manual abstain queries into a benchmark fixture."""
    import random

    repo = Path(repo_dir).resolve()
    rng = random.Random(seed)
    items: list[dict[str, Any]] = []

    # 1a. Docstring mining
    try:
        ls = subprocess.run(
            ["git", "ls-files", "--", "*.py"],
            cwd=str(repo), capture_output=True, text=True,
        )
        py_files = [f.strip() for f in ls.stdout.strip().split("\n") if f.strip()]
    except Exception:
        py_files = []

    seen_queries: set[str] = set()
    docstring_candidates: list[dict[str, Any]] = []

    for rel in py_files:
        if "/tests/" in rel or rel.startswith("test_") or "/test_" in rel:
            continue
        fpath = repo / rel
        if not fpath.exists():
            continue
        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(source, filename=str(fpath))
        except Exception:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_") and node.name != "__init__":
                continue
            if node.name == "__init__":
                continue
            ds = ast.get_docstring(node)
            if not ds or len(ds) < 20:
                continue
            first_sentence = ds.split(".")[0].strip()
            if len(first_sentence) < 15:
                continue
            if first_sentence in seen_queries:
                continue
            seen_queries.add(first_sentence)
            docstring_candidates.append({
                "query": first_sentence,
                "expected_file": rel,
                "expected_name": node.name,
                "tag": "docstring",
            })

    if len(docstring_candidates) > 100:
        docstring_candidates = rng.sample(docstring_candidates, 100)
    items.extend(docstring_candidates)

    # 1b. Git history mining
    try:
        log = subprocess.run(
            ["git", "log", "--no-merges", "--format=%H %s", "-200"],
            cwd=str(repo), capture_output=True, text=True,
        )
        commits = [ln.strip() for ln in log.stdout.strip().split("\n") if ln.strip()]
    except Exception:
        commits = []

    _skip_re = re.compile(r"^(chore|docs)(\([^)]*\))?:|^(Merge |release-please)")
    git_items: list[dict[str, Any]] = []

    for entry in commits:
        parts = entry.split(" ", 1)
        if len(parts) < 2:
            continue
        sha, msg = parts
        if len(msg) < 15:
            continue
        if _skip_re.match(msg):
            continue

        files_out = subprocess.run(
            ["git", "log", "--format=", "--name-only", "-1", sha],
            cwd=str(repo), capture_output=True, text=True,
        )
        changed = [
            f.strip() for f in files_out.stdout.strip().split("\n")
            if f.strip() and Path(f.strip()).suffix.lower() in SUPPORTED_EXTENSIONS
        ]
        if not changed:
            continue
        if all(Path(f).suffix.lower() in DOC_EXTENSIONS for f in changed):
            continue

        query = re.sub(r"^(feat|fix|refactor|perf|test|ci|build)\([^)]*\):\s*", "", msg)
        query = re.sub(r"^(feat|fix|refactor|perf|test|ci|build):\s*", "", query)
        query = re.sub(r"\s*\(#\d+\)", "", query)
        if query in seen_queries:
            continue
        seen_queries.add(query)
        git_items.append({
            "query": query,
            "expected_files": changed[:5],
            "tag": "git-history",
        })

    if len(git_items) > 50:
        git_items = rng.sample(git_items, 50)
    items.extend(git_items)

    # 1c. Manual abstain queries (far-OOD + near-OOD)
    for q in ABSTAIN_QUERIES_FAR:
        items.append({"query": q, "abstain": True, "tag": "abstain-far"})
    for q in ABSTAIN_QUERIES_NEAR:
        items.append({"query": q, "abstain": True, "tag": "abstain-near"})

    # Write JSONL
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        print(f"Wrote {len(items)} fixture items to {out_path}", file=sys.stderr)

    # Store calibration distribution (in-domain query distances) for percentile confidence
    dbp = _resolve_db_path()
    if dbp.exists():
        db = _init_db(dbp)
        cal_distances: list[float] = []
        for item in items:
            if item.get("abstain"):
                continue
            emb = _embed_text(item["query"])
            vec_rows = db.execute(
                "SELECT rowid FROM chunks_vec WHERE embedding MATCH ? ORDER BY distance LIMIT 1",
                (_serialize(emb),),
            ).fetchall()
            if vec_rows:
                cos_row = db.execute(
                    "SELECT vec_distance_cosine(embedding, ?) FROM chunks_vec WHERE rowid = ?",
                    (_serialize(emb), vec_rows[0][0]),
                ).fetchone()
                if cos_row:
                    cal_distances.append(cos_row[0])
        cal_distances.sort()
        db.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('calibration_distances', ?)",
            [json.dumps(cal_distances)],
        )
        db.commit()
        db.close()
        global _cal_cache
        _cal_cache = None
        print(f"Stored {len(cal_distances)} calibration distances in index_meta", file=sys.stderr)

    return items


# ── Benchmark ────────────────────────────────────────────────────────────────

def benchmark(
    dataset: list[dict[str, Any]],
    *,
    k: int = 5,
    gap_threshold: float = 0.0,
    rrf_k: int = DEFAULT_RRF_K,
) -> dict[str, Any]:
    """Run dataset queries, compute recall@k, MRR@k, abstain accuracy, latency."""
    n = len(dataset)
    if n == 0:
        return {"error": "empty dataset"}

    by_tag: dict[str, dict[str, Any]] = {}
    recall_hits = 0
    mrr_sum = 0.0
    abstain_correct = 0
    abstain_total = 0
    non_abstain_count = 0
    latencies: list[float] = []
    confidence_by_tag: dict[str, list[float]] = {}

    for item in dataset:
        q = item["query"]
        expected_files = item.get("expected_files") or (
            [item["expected_file"]] if item.get("expected_file") else []
        )
        expected_name = item.get("expected_name")
        tag = item.get("tag", "abstain" if item.get("abstain") else "single")
        expect_abstain = bool(item.get("abstain"))

        bucket = by_tag.setdefault(tag, {"n": 0, "hits": 0, "mrr": 0.0})
        bucket["n"] += 1

        t0 = time.monotonic()
        results = search(q, k=k, gap_threshold=gap_threshold, rrf_k=rrf_k)
        latencies.append(time.monotonic() - t0)

        ret_conf = results[0].get("retrieval_confidence", 0.0) if results else 0.0
        confidence_by_tag.setdefault(tag, []).append(ret_conf)

        if expect_abstain:
            abstain_total += 1
            if ret_conf < CONFIDENCE_ABSTAIN_THRESHOLD:
                abstain_correct += 1
                bucket.setdefault("abstain_correct", 0)
                bucket["abstain_correct"] += 1
            continue

        non_abstain_count += 1

        empty = len(results) == 0 or (len(results) == 1 and "error" in results[0])
        if empty:
            continue

        returned_files = [r["file"] for r in results if "file" in r]
        returned_names = [r["name"] for r in results if "name" in r]

        hit = any(ef in returned_files for ef in expected_files)
        if expected_name and not hit:
            hit = expected_name in returned_names

        if hit:
            recall_hits += 1
            bucket["hits"] += 1
            for idx, r in enumerate(results):
                match = (r.get("file") in expected_files or
                         (expected_name and r.get("name") == expected_name))
                if match:
                    reciprocal = 1.0 / (idx + 1)
                    mrr_sum += reciprocal
                    bucket["mrr"] += reciprocal
                    break

    latencies.sort()
    p50 = latencies[len(latencies) // 2] if latencies else 0
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))] if latencies else 0

    tag_report: dict[str, dict[str, Any]] = {}
    for tag, s in by_tag.items():
        confs = confidence_by_tag.get(tag, [])
        mean_conf = round(sum(confs) / len(confs), 3) if confs else 0.0
        entry: dict[str, Any] = {"n": s["n"], "mean_confidence": mean_conf}
        if tag.startswith("abstain"):
            entry["abstain_accuracy"] = round(s.get("abstain_correct", 0) / s["n"], 3) if s["n"] else None
        else:
            entry["recall_at_k"] = round(s["hits"] / s["n"], 3) if s["n"] else 0
            entry["mrr_at_k"] = round(s["mrr"] / s["n"], 3) if s["n"] else 0
        tag_report[tag] = entry

    return {
        "n": n,
        "recall_at_k": round(recall_hits / non_abstain_count, 3) if non_abstain_count else 0,
        "mrr_at_k": round(mrr_sum / non_abstain_count, 3) if non_abstain_count else 0,
        "abstain_accuracy": round(abstain_correct / abstain_total, 3) if abstain_total else None,
        "latency_p50_ms": round(p50 * 1000, 1),
        "latency_p95_ms": round(p95 * 1000, 1),
        "by_tag": tag_report,
        "config": {"k": k, "gap_threshold": gap_threshold, "rrf_k": rrf_k},
    }


# ── Calibration Sweep ────────────────────────────────────────────────────────



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

    p_fixture = sub.add_parser("generate-fixture", help="Generate benchmark fixture from codebase")
    p_fixture.add_argument("directory", nargs="?", default=".")
    p_fixture.add_argument("-o", "--output", default="scripts/tests/fixtures/code_search_bench.jsonl")

    p_bench = sub.add_parser("benchmark", help="Run benchmark against fixture")
    p_bench.add_argument("fixture", help="Path to JSONL fixture")
    p_bench.add_argument("-k", type=int, default=5)

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
    elif args.command == "generate-fixture":
        items = generate_fixture(args.directory, output=args.output)
        print(json.dumps({"count": len(items), "output": args.output}, indent=2))
    elif args.command == "benchmark":
        with open(args.fixture) as f:
            dataset = [json.loads(line) for line in f if line.strip()]
        result = benchmark(dataset, k=args.k)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
