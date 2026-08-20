#!/usr/bin/env python3
"""
Deus Memory GC — TTL enforcement for auto-memory files.

Reads memory files with ttl_days + updated_at frontmatter, archives expired ones.
Run weekly (or manually) to keep the memory index lean.

Usage:
  python3 memory_gc.py [--memory-dir PATH] [--dry-run]
"""

import argparse
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path


def _load_vault_atoms() -> Path:
    """Resolve vault Atoms/ path from config.json or DEUS_VAULT_PATH env var."""
    env_path = os.environ.get("DEUS_VAULT_PATH")
    if env_path:
        return Path(env_path).expanduser() / "Atoms"
    cfg_path = Path("~/.config/deus/config.json").expanduser()
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
            if cfg.get("vault_path"):
                return Path(cfg["vault_path"]).expanduser() / "Atoms"
        except (json.JSONDecodeError, OSError):
            pass
    print(
        "ERROR: Memory vault not configured.\n"
        "Set DEUS_VAULT_PATH or add vault_path to ~/.config/deus/config.json",
        file=__import__("sys").stderr,
    )
    __import__("sys").exit(1)


VAULT_ATOMS = _load_vault_atoms()


def find_memory_dirs(base: Path) -> list[Path]:
    return [d / "memory" for d in base.iterdir() if (d / "memory").is_dir()]


def parse_frontmatter(content: str) -> dict:
    m = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not m:
        return {}
    fm: dict = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm


def set_frontmatter_field(content: str, key: str, value: str) -> str:
    """Add or replace a field in the YAML frontmatter block."""
    m = re.match(r"^(---\n)(.*?)(\n---)", content, re.DOTALL)
    if not m:
        return content
    body = m.group(2)
    if re.search(rf"^{re.escape(key)}:", body, re.MULTILINE):
        body = re.sub(rf"^{re.escape(key)}:.*$", f"{key}: {value}", body, flags=re.MULTILINE)
    else:
        body = body.rstrip() + f"\n{key}: {value}"
    return f"---\n{body}\n---" + content[m.end():]


def _orphan_tree_node(md_file: Path, fm: dict) -> None:
    """Soft-delete the memory-tree node for a file just archived.

    ``archive_file`` moves the source file into ``ARCHIVE/`` and unlinks the
    original path; without this, the memory-tree DB row for that file would
    keep pointing at a now-moved path until some future reindex sweep. Per
    docs/decisions/no-db-deletion.md, primary-table rows are marked orphaned
    (``orphaned_at``/``orphan_reason``), never hard-deleted — mirrors the
    orphan step in ``memory_tree.reindex_external``.

    No-op when the file was never indexed (no ``id:`` in frontmatter — only
    ``memory_tree._index_external_file`` writes that back). Best-effort:
    any failure to reach the tree DB (import error, missing dependency,
    locked DB) is swallowed — GC must not fail because the tree index is
    unavailable in a given environment.
    """
    node_id = fm.get("id")
    if not node_id:
        return
    try:
        import memory_tree

        db = memory_tree.open_db()
        try:
            # md_file was already unlinked by the caller above, so this is
            # expected to confirm "gone" every time; kept as a genuine check
            # (not a rubber stamp) in case a concurrent process re-created
            # the path between the unlink and this call.
            if not memory_tree._confirm_orphan(md_file, require_id=False):
                return  # file unexpectedly present again -- don't orphan a live node
            db.execute(
                "UPDATE nodes SET orphaned_at = ?, orphan_reason = ? "
                "WHERE id = ? AND orphaned_at IS NULL",
                (memory_tree._utc_iso(), "archived", node_id),
            )
            db.commit()
        finally:
            db.close()
    except Exception as e:
        # Best-effort is a policy choice (GC must not crash when the tree DB
        # is unavailable) -- it must not also be a SILENT one. Without this,
        # GC would print "archived: <file>" and exit 0 while the tree node
        # stays live, pointing at a moved file, with nothing anywhere
        # reporting the gap between the two.
        sys.stderr.write(
            f"[memory_gc] WARNING: could not orphan tree node for {md_file.name}: {e}\n"
        )


def archive_file(memory_dir: Path, md_file: Path, fm: dict, dry_run: bool) -> str:
    archive_dir = memory_dir / "ARCHIVE"
    rel = md_file.relative_to(memory_dir)
    # Preserve the nested path under ARCHIVE/ (not just the basename) — two
    # distinct files with the same basename in different subdirectories
    # (reachable now that run_gc walks recursively) would otherwise collide
    # on a flat ARCHIVE/<name> destination and silently clobber each other.
    dest = archive_dir / rel

    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)
        content = set_frontmatter_field(md_file.read_text(), "status", "archived")
        dest.write_text(content)
        md_file.unlink()

        _orphan_tree_node(md_file, fm)

        # Append to ARCHIVE_INDEX.md
        index_path = archive_dir / "ARCHIVE_INDEX.md"
        name = fm.get("name", md_file.stem)
        desc = fm.get("description", "")
        row = f"- [{name}]({rel.as_posix()}) — {desc}\n"
        with open(index_path, "a") as f:
            f.write(row)

        # Remove pointer from MEMORY.md
        # Matches on the file's path relative to memory_dir (not just its
        # basename) so that archiving one file can't wrongly strip a
        # still-valid pointer to a DIFFERENT file sharing the same basename
        # in another subdirectory (rel is unique; a bare basename is not,
        # once nested discovery is in play). This is still a substring
        # match, not a full parse — a line that happens to mention the exact
        # relative path elsewhere would still be stripped. That broader
        # defect is superseded by a planned slug-based reconciler in a later
        # phase and is intentionally not fixed here.
        memory_md = memory_dir / "MEMORY.md"
        if memory_md.exists():
            rel_str = rel.as_posix()
            lines = [l for l in memory_md.read_text().splitlines() if rel_str not in l]
            memory_md.write_text("\n".join(lines) + "\n")

    expiry_str = str(date.fromisoformat(fm["updated_at"]) + timedelta(days=int(fm["ttl_days"])))
    return f"  archived: {md_file.name} (expired {expiry_str})"


def run_gc(memory_dir: Path, dry_run: bool) -> int:
    today = date.today()
    archived = 0

    # rglob (not glob) so nested files, e.g. memory/procedures/*.md, are
    # reachable. rglob also descends into ARCHIVE/, so skip anything already
    # archived — mirrors memory_tree.reindex_external's skip_dirs pattern
    # (scripts/memory_tree.py:2257) to avoid re-processing archived files.
    for md_file in sorted(memory_dir.rglob("*.md")):
        rel_parts = md_file.relative_to(memory_dir).parts
        if "ARCHIVE" in rel_parts:
            continue
        if md_file.name == "MEMORY.md":
            continue
        content = md_file.read_text()
        fm = parse_frontmatter(content)

        ttl = fm.get("ttl_days")
        updated = fm.get("updated_at")
        if not ttl or not updated:
            continue

        try:
            expiry = date.fromisoformat(updated) + timedelta(days=int(ttl))
        except (ValueError, TypeError):
            continue

        if expiry < today:
            msg = archive_file(memory_dir, md_file, fm, dry_run)
            print(msg)
            archived += 1

    return archived


def run_atoms_gc(dry_run: bool) -> int:
    """Delete expired atom files from the vault Atoms/ directory."""
    if not VAULT_ATOMS.exists():
        return 0

    today = date.today()
    deleted = 0

    for md_file in sorted(VAULT_ATOMS.glob("*.md")):
        content = md_file.read_text()
        fm = parse_frontmatter(content)

        ttl = fm.get("ttl_days")
        updated = fm.get("updated_at")
        if not ttl or ttl == "null" or not updated:
            continue  # fact/decision atoms have no TTL — keep forever

        try:
            expiry = date.fromisoformat(updated) + timedelta(days=int(ttl))
        except (ValueError, TypeError):
            continue

        if expiry < today:
            print(f"  expired atom: {md_file.name} (expired {expiry})")
            if not dry_run:
                md_file.unlink()
            deleted += 1

    return deleted


def main():
    parser = argparse.ArgumentParser(description="Deus memory TTL GC")
    parser.add_argument(
        "--memory-dir",
        metavar="PATH",
        help="Path to a single memory dir. Default: all dirs under ~/.claude/projects/",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be archived without making changes",
    )
    args = parser.parse_args()

    if args.memory_dir:
        dirs = [Path(args.memory_dir).expanduser()]
    else:
        base = Path("~/.claude/projects").expanduser()
        dirs = find_memory_dirs(base) if base.exists() else []

    total = 0
    for d in dirs:
        print(f"\n[{d}]")
        n = run_gc(d, args.dry_run)
        if n == 0:
            print("  nothing to archive")
        total += n

    print(f"\n[{VAULT_ATOMS}]")
    n = run_atoms_gc(args.dry_run)
    if n == 0:
        print("  nothing to delete")
    total += n

    suffix = " (dry run)" if args.dry_run else ""
    print(f"\nDone{suffix}. {total} file(s) processed.")


if __name__ == "__main__":
    main()
