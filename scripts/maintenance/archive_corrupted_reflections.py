#!/usr/bin/env python3
"""LIA-213 cleanup: soft-delete corrupted reflections from the evolution store.

The reflection generator historically saved the judge model's raw output with no
validation, so chat-template debris, judge-prompt leakage, and recursive envelope
echoes were embedded and then prepended verbatim to live agent prompts. This tool
finds active reflections whose content fails ``is_valid_reflection`` and
soft-deletes them (sets ``archived_at``; the row + embedding are retained and
excluded from retrieval and dedup at the SQL layer). No rows are deleted.

Generic: corruption is detected by pattern (reflexion.validation), not hardcoded
IDs, so it works on any user's store. Dry-run by default.

Usage:
    python scripts/maintenance/archive_corrupted_reflections.py            # dry-run
    python scripts/maintenance/archive_corrupted_reflections.py --apply    # soft-delete
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from evolution.config import EVOLUTION_DB_PATH  # noqa: E402
from evolution.reflexion.validation import is_valid_reflection  # noqa: E402


def find_corrupted(db_path: Path) -> tuple[list[dict], int, int]:
    """Scan active reflections for corruption.

    Returns ``(corrupted, total_active, total_active_retrievals)`` where
    ``corrupted`` is a list of ``{id, reason, times_retrieved, preview}`` sorted
    by retrieval impact (most-injected first). Read-only — no vec0 extension
    needed (the reflections table is plain).
    """
    db = sqlite3.connect(str(db_path))
    try:
        rows = db.execute(
            "SELECT id, content, times_retrieved FROM reflections "
            "WHERE archived_at IS NULL"
        ).fetchall()
    finally:
        db.close()

    total_active = len(rows)
    total_retrievals = sum((r[2] or 0) for r in rows)
    corrupted: list[dict] = []
    for rid, content, retrieved in rows:
        ok, reason = is_valid_reflection(content or "")
        if not ok:
            corrupted.append({
                "id": rid,
                "reason": reason,
                "times_retrieved": retrieved or 0,
                "preview": (content or "").replace("\n", " ")[:120],
            })
    corrupted.sort(key=lambda c: c["times_retrieved"], reverse=True)
    return corrupted, total_active, total_retrievals


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Soft-delete corrupted reflections from the evolution store (LIA-213).",
    )
    ap.add_argument("--apply", action="store_true",
                    help="Soft-delete the corrupted reflections (default: dry-run).")
    args = ap.parse_args(argv)

    db_path = Path(EVOLUTION_DB_PATH)
    if not db_path.exists():
        print(f"[archive-corrupted] DB not found: {db_path}", file=sys.stderr)
        return 2

    corrupted, total_active, total_retrievals = find_corrupted(db_path)
    corrupted_retrievals = sum(c["times_retrieved"] for c in corrupted)
    pct = (100.0 * corrupted_retrievals / total_retrievals) if total_retrievals else 0.0

    print(f"[archive-corrupted] active reflections: {total_active}")
    print(f"[archive-corrupted] corrupted: {len(corrupted)} "
          f"({corrupted_retrievals}/{total_retrievals} retrievals = {pct:.1f}%)")
    for c in corrupted:
        print(f"  - {c['id']}  retr={c['times_retrieved']:<5} "
              f"{c['reason']:<28} {c['preview']!r}")

    if not corrupted:
        print("[archive-corrupted] nothing to do.")
        return 0

    if not args.apply:
        print("\n[archive-corrupted] DRY-RUN -- re-run with --apply to soft-delete.")
        return 0

    # Soft-delete through the tested storage path (sets archived_at; idempotent).
    from evolution.reflexion.store import archive_reflection_by_id
    archived = sum(1 for c in corrupted if archive_reflection_by_id(c["id"]))
    print(f"\n[archive-corrupted] soft-deleted {archived}/{len(corrupted)} "
          f"(archived_at set; rows + embeddings retained).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
