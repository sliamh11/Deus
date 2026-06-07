#!/usr/bin/env python3
"""Build the curated Odysseus share index (one-shot ETL, run on the HOST).

Walks the ``Shareable/`` subfolder of the Deus vault, embeds each markdown file
via the shared :mod:`_embed` module, and writes a SELF-CONTAINED sqlite-vec DB
at ``DEUS_SHARE_DB_PATH`` (default ``~/.deus/odysseus-share.db``).

Why a dedicated DB with its own schema (not a reuse of memory.db /
memory_tree.db): the Odysseus sidecar mounts ONLY this file. Baking just the
curated text in here means a compromised Odysseus agent physically cannot reach
any non-shareable memory — the data isolation is structural, not policy.

Re-run this whenever you change what's in ``Shareable/``. There is no watcher;
the index is a point-in-time snapshot (stale until you re-run).

Usage (from the Deus repo, in its venv so `mcp`/`sqlite_vec` are importable):
    python integrations/odysseus/build_share_index.py
    python integrations/odysseus/build_share_index.py --share-dir /path --db /path
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import sqlite_vec

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _embed  # noqa: E402  (shared embedder; same dir)


def _default_share_dir() -> Path | None:
    """Resolve the vault Shareable/ folder.

    Prefer an explicit env var; otherwise ask Deus's memory_tree for the vault
    root. Returns None if neither is available (caller errors out with help).
    """
    env = os.environ.get("DEUS_VAULT_SHARE_PATH")  # config path, tracked in PR #715
    if env:
        return Path(env).expanduser()
    try:
        repo_scripts = Path(__file__).resolve().parents[2] / "scripts"
        sys.path.insert(0, str(repo_scripts))
        import memory_tree as mt  # type: ignore

        return Path(mt.resolve_vault_path()) / "Shareable"
    except Exception:
        return None


def _open_db(db_path: Path) -> sqlite3.Connection:
    """Open a sqlite-vec connection. enable_load_extension MUST precede load."""
    conn = sqlite3.connect(str(db_path))
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS entries")
    conn.execute("DROP TABLE IF EXISTS vec_entries")
    conn.execute("DROP TABLE IF EXISTS meta")
    conn.execute(
        "CREATE TABLE entries (id INTEGER PRIMARY KEY, path TEXT, text TEXT)"
    )
    conn.execute(
        f"CREATE VIRTUAL TABLE vec_entries USING vec0("
        f"embedding float[{_embed.EMBED_DIM}])"
    )
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.commit()


def _write_meta(conn: sqlite3.Connection, count: int) -> None:
    """Stamp the index with the model/dim it was built with, so the sidecar can
    refuse to serve a DB whose vectors were produced by a different model."""
    from datetime import datetime, timezone

    rows = {
        "model": _embed.OLLAMA_EMBED_MODEL,
        "dim": str(_embed.EMBED_DIM),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "count": str(count),
    }
    conn.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)", list(rows.items())
    )
    conn.commit()


def _smoke_test() -> None:
    """Fail loudly at build time if embedding is misconfigured."""
    v = _embed.embed("odysseus share index smoke test")
    if len(v) != _embed.EMBED_DIM:
        raise SystemExit(
            f"FATAL: embedding smoke test returned {len(v)} dims, "
            f"expected {_embed.EMBED_DIM}. Check OLLAMA_HOST / model."
        )
    print(f"[build] embed smoke test OK ({len(v)} dims)")


def build(share_dir: Path, db_path: Path) -> int:
    if not share_dir.is_dir():
        raise SystemExit(
            f"FATAL: share dir not found: {share_dir}\n"
            "Create the Shareable/ folder in your vault (or pass --share-dir) "
            "and put the notes you want Odysseus to see in it."
        )
    _smoke_test()

    md_files = sorted(p for p in share_dir.rglob("*.md") if p.is_file())
    if not md_files:
        print(f"[build] WARNING: no .md files under {share_dir} — empty index.")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = _open_db(db_path)
    try:
        _create_schema(conn)
        n = 0
        skipped = 0
        for md in md_files:
            rel = md.relative_to(share_dir)
            text = md.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            # Resilient per file: one embed failure must not abort the whole
            # build and leave a half-written index that looks complete.
            try:
                vec = _embed.embed(text)
            except Exception as exc:  # noqa: BLE001 — report and skip, keep going
                skipped += 1
                print(f"[build]   WARN: skipped {rel}: {exc}")
                continue
            cur = conn.execute(
                "INSERT INTO entries (path, text) VALUES (?, ?)",
                (str(rel), text),
            )
            conn.execute(
                "INSERT INTO vec_entries (rowid, embedding) VALUES (?, ?)",
                (cur.lastrowid, sqlite_vec.serialize_float32(vec)),
            )
            n += 1
            print(f"[build]   indexed {rel}")
        conn.commit()
        _write_meta(conn, n)
    finally:
        conn.close()

    suffix = f" ({skipped} skipped — re-run after fixing)" if skipped else ""
    print(f"[build] done: {n} of {len(md_files)} file(s) -> {db_path}{suffix}")
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Build the Odysseus share index.")
    ap.add_argument(
        "--share-dir",
        type=Path,
        default=None,
        help="Vault Shareable/ folder (default: $DEUS_VAULT_SHARE_PATH or vault/Shareable).",
    )
    ap.add_argument(
        "--db",
        type=Path,
        # DEUS_SHARE_DB_PATH: config path, tracked in PR #715
        default=Path(
            os.environ.get("DEUS_SHARE_DB_PATH", "~/.deus/odysseus-share.db")
        ).expanduser(),
        help="Output sqlite-vec DB path (default: ~/.deus/odysseus-share.db).",
    )
    args = ap.parse_args()

    share_dir = args.share_dir or _default_share_dir()
    if share_dir is None:
        raise SystemExit(
            "FATAL: could not resolve the Shareable/ folder. "
            "Set DEUS_VAULT_SHARE_PATH or pass --share-dir."
        )
    build(Path(share_dir).expanduser(), Path(args.db).expanduser())


if __name__ == "__main__":
    main()
