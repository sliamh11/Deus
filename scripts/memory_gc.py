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


def archive_file(memory_dir: Path, md_file: Path, fm: dict, dry_run: bool) -> str:
    archive_dir = memory_dir / "ARCHIVE"
    archive_dir.mkdir(exist_ok=True)
    dest = archive_dir / md_file.name

    if not dry_run:
        content = set_frontmatter_field(md_file.read_text(), "status", "archived")
        dest.write_text(content)
        md_file.unlink()

        # Append to ARCHIVE_INDEX.md
        index_path = archive_dir / "ARCHIVE_INDEX.md"
        name = fm.get("name", md_file.stem)
        desc = fm.get("description", "")
        row = f"- [{name}]({md_file.name}) — {desc}\n"
        with open(index_path, "a") as f:
            f.write(row)

        # Remove pointer from MEMORY.md
        memory_md = memory_dir / "MEMORY.md"
        if memory_md.exists():
            lines = [l for l in memory_md.read_text().splitlines() if md_file.name not in l]
            memory_md.write_text("\n".join(lines) + "\n")

    expiry_str = str(date.fromisoformat(fm["updated_at"]) + timedelta(days=int(fm["ttl_days"])))
    return f"  archived: {md_file.name} (expired {expiry_str})"


def run_gc(memory_dir: Path, dry_run: bool) -> int:
    today = date.today()
    archived = 0

    for md_file in sorted(memory_dir.glob("*.md")):
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
