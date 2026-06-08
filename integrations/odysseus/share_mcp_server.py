#!/usr/bin/env python3
"""Odysseus curated-memory MCP sidecar (read-only).

Exposes ONE tool, ``recall(query, k)``, over MCP streamable-http. It queries a
self-contained sqlite-vec DB (built by ``build_share_index.py``) that contains
ONLY the curated ``Shareable/`` subset of Deus memory. The sidecar has no vault
filesystem access and imports no Deus package — it can only read this one DB.

Runs inside a Docker container on Odysseus's network. Binds 0.0.0.0:8200; the
compose file publishes no host port, so it is reachable only as the
``deus-memory`` service on the ``odysseus_default`` network.

Fail-fast contract: if the DB is missing or Ollama is unreachable, ``recall``
returns a clear ``{"error": ...}`` payload immediately rather than hanging, so
the Odysseus agent degrades gracefully.

Security note: anything in the curated DB is, by design, visible to the
(prompt-injectable, unsandboxed) Odysseus agent. Keep ``Shareable/``
conservative. Every query is appended to the audit log for after-the-fact review.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import sqlite_vec
from mcp.server.fastmcp import FastMCP

import _embed

# Sidecar config flags (runtime config, not feature gates; tracked in PR #715).
DB_PATH = Path(os.environ.get("DEUS_SHARE_DB_PATH", "/data/share.db"))
RECALL_LOG = Path(os.environ.get("DEUS_RECALL_LOG", "/data/recall.log"))
# Default to loopback for safety: if someone runs this directly on the host
# (outside Docker), it won't bind to all interfaces. The compose file sets
# DEUS_MCP_HOST=0.0.0.0 explicitly so the container is reachable on its network.
BIND_HOST = os.environ.get("DEUS_MCP_HOST", "127.0.0.1")
BIND_PORT = int(os.environ.get("DEUS_MCP_PORT", "8200"))
# Clamp to >=1 so a stray 0/negative value can't blank every result silently. (PR #715)
MAX_CHARS = max(1, int(os.environ.get("DEUS_RECALL_MAX_CHARS", "4000")))
# Optional L2-distance cutoff. Unset (default) = return all top-k, matching the
# approved plan's behavior. When set, off-topic queries that only match weakly
# return nothing instead of the least-bad curated note. See .env.example for
# calibration guidance (the canonical home for the threshold values). (PR #715)
_max_dist_env = os.environ.get("DEUS_RECALL_MAX_DISTANCE", "").strip()
MAX_DISTANCE = float(_max_dist_env) if _max_dist_env else None

mcp = FastMCP("deus-memory", host=BIND_HOST, port=BIND_PORT)


def _audit(query: str, k: int, status: str, n: int) -> None:
    """Append a one-line JSON audit record. Best-effort; never raises."""
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "k": k,
            "status": status,
            "results": n,
        }
        RECALL_LOG.parent.mkdir(parents=True, exist_ok=True)
        with RECALL_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # auditing must never break recall


def _open_ro() -> sqlite3.Connection:
    """Open the share DB read-only with the sqlite-vec extension loaded."""
    uri = f"file:{DB_PATH}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _index_model(conn: sqlite3.Connection) -> str | None:
    """Embedding model the index was BUILT with, or None if unknown (older index)."""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'model'"
        ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None  # meta table absent (index built before this column existed)


@mcp.tool()
def recall(query: str, k: int = 3) -> dict:
    """Recall curated Deus memory relevant to ``query``.

    Searches only the user's curated "Shareable" memory subset. Returns the
    top-``k`` matching notes as ``{"results": [{"path", "text", "distance"}]}``.
    Read-only: this tool cannot write, delete, or reach anything outside the
    curated set.

    Args:
        query: Natural-language query.
        k:     Max number of notes to return (default 3).
    """
    k = max(1, min(int(k), 20))

    if not DB_PATH.exists():
        _audit(query, k, "no_db", 0)
        return {
            "error": "Curated memory index not built yet. Run "
            "build_share_index.py on the host.",
            "results": [],
        }

    try:
        qvec = _embed.embed(query)
    except Exception as exc:
        _audit(query, k, "embed_error", 0)
        return {"error": f"Embedding failed (Ollama unreachable?): {exc}", "results": []}

    try:
        conn = _open_ro()
    except Exception as exc:
        _audit(query, k, "db_error", 0)
        return {"error": f"Could not open curated index: {exc}", "results": []}

    try:
        built = _index_model(conn)
        if built and built != _embed.OLLAMA_EMBED_MODEL:
            _audit(query, k, "model_mismatch", 0)
            return {
                "error": (
                    f"Index built with embedding model '{built}' but this server "
                    f"is configured for '{_embed.OLLAMA_EMBED_MODEL}'. Rebuild with "
                    "build_share_index.py so vectors are comparable."
                ),
                "results": [],
            }
        # vec0's `k = ?` is the canonical KNN limit (returns <= k rows); no LIMIT needed.
        rows = conn.execute(
            """SELECT e.path, e.text, v.distance
               FROM vec_entries v JOIN entries e ON e.id = v.rowid
               WHERE v.embedding MATCH ? AND k = ?
               ORDER BY v.distance""",
            (sqlite_vec.serialize_float32(qvec), k),
        ).fetchall()
    except Exception as exc:
        _audit(query, k, "query_error", 0)
        return {"error": f"Recall query failed: {exc}", "results": []}
    finally:
        conn.close()

    results = [
        {"path": path, "text": (text or "")[:MAX_CHARS], "distance": round(dist, 4)}
        for path, text, dist in rows
        if MAX_DISTANCE is None or dist <= MAX_DISTANCE
    ]
    _audit(query, k, "ok", len(results))
    return {"results": results}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
