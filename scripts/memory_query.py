#!/usr/bin/env python3
"""memory_query.py — reusable memory retrieval for all Deus interfaces.

Wraps memory_tree.retrieve() with file-reading, context formatting, and
unified logging. Designed as the shared foundation for MCP server (Phase 2),
HTTP bridge (Phase 3), container hooks (Phase 4/5), and Aider wrapper (Phase 6).

Platform: Linux/macOS only (depends on sqlite_vec C extension + Ollama).
Fails fast on Windows with a clear error rather than an opaque import failure.

Log schema: appends to ~/.deus/memory_retrieval_log.jsonl with a `source` field.
Existing host-hook entries (pre-Phase 1) lack this field; per-interface analytics
cover only entries written by this module until the hook is updated separately.

Usage:
    # As a module
    from memory_query import recall
    result = recall("what is Liam's timezone?", source="mcp")

    # As CLI
    python3 scripts/memory_query.py "query text" --source bridge -k 3
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

if sys.platform == "win32":
    print("memory_query.py requires Linux or macOS (sqlite_vec + Ollama).", file=sys.stderr)
    sys.exit(1)

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import memory_tree as mt  # noqa: E402

LOG_FILE = Path(os.environ.get(
    "DEUS_RETRIEVAL_LOG",
    "~/.deus/memory_retrieval_log.jsonl",
)).expanduser()

AUTO_MEM_DIR = Path(os.environ.get(
    mt.EXTERNAL_DIR_ENV,
    os.environ.get(
        "DEUS_AUTO_MEMORY_DIR",
        "~/.claude/projects/-Users-liam10play-deus/memory",
    ),
)).expanduser()


def _read_node_file(path: str) -> str | None:
    """Resolve a tree node path to file content, mirroring the host hook logic."""
    if path.startswith(mt.EXTERNAL_NAMESPACE):
        filename = path[len(mt.EXTERNAL_NAMESPACE):]
        full = AUTO_MEM_DIR / filename
    else:
        vault = mt.resolve_vault_path()
        full = vault / path
    try:
        return full.read_text(encoding="utf-8", errors="replace") if full.is_file() else None
    except OSError:
        return None


def _format_context(results: list[dict], fell_back: bool) -> str:
    """Build the context string matching the host hook output format."""
    if fell_back or not results:
        return ""
    lines = ["=== Auto-retrieved memory (may not be relevant to your task) ==="]
    for r in results:
        content = _read_node_file(r["path"])
        if content:
            lines.append(f"--- {r['path']} (score: {r['score']:.4f}) ---")
            lines.append(content)
    lines.append("=== End auto-retrieved memory ===")
    return "\n".join(lines)


def _log_retrieval(
    query: str,
    result: dict,
    source: str,
) -> None:
    """Append to the unified retrieval log (same file as host hook)."""
    prompt_hash = hashlib.sha256(query.encode()).hexdigest()[:16]
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "prompt_hash": prompt_hash,
        "confidence": result["confidence"],
        "fell_back": result["fell_back"],
        "paths": [r["path"] for r in result["results"]],
        "source": source,
    }
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def recall(
    query: str,
    *,
    k: int = 3,
    abstain_threshold: float | None = None,
    source: str = "unknown",
) -> dict:
    """Retrieve memory context for a query.

    Returns:
        {
            "context": str,       # formatted text block (empty on abstain)
            "paths": [str, ...],  # matched file paths
            "confidence": float,
            "fell_back": bool,
        }
    """
    threshold = abstain_threshold if abstain_threshold is not None else mt.DEFAULT_ABSTAIN_THRESHOLD

    db = mt.open_db()
    try:
        raw = mt.retrieve(db, query, k=k, abstain_threshold=threshold)
    finally:
        db.close()

    context = _format_context(raw["results"], raw["fell_back"])
    paths = [r["path"] for r in raw["results"]] if not raw["fell_back"] else []

    out = {
        "context": context,
        "paths": paths,
        "confidence": raw["confidence"],
        "fell_back": raw["fell_back"],
    }

    _log_retrieval(query, raw, source)

    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="memory_query",
        description="Retrieve memory context from the Deus memory tree.",
    )
    parser.add_argument("query", help="Query text")
    parser.add_argument("-k", type=int, default=3, help="Top-K results")
    parser.add_argument(
        "--abstain", type=float, default=None,
        help=f"Abstain threshold (default: {mt.DEFAULT_ABSTAIN_THRESHOLD})",
    )
    parser.add_argument("--source", default="cli", help="Source identifier for logging")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--context-only", action="store_true", help="Output only the context block")

    args = parser.parse_args(argv)
    result = recall(args.query, k=args.k, abstain_threshold=args.abstain, source=args.source)

    if args.context_only:
        print(result["context"])
    elif args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        if result["fell_back"]:
            print(f"Abstained (confidence={result['confidence']:.3f})")
        else:
            for p in result["paths"]:
                print(f"  {p}")
            print(f"— confidence={result['confidence']:.3f}")
            if result["context"]:
                print()
                print(result["context"])
    return 0 if not result["fell_back"] else 1


if __name__ == "__main__":
    sys.exit(main())
