#!/usr/bin/env python3
# macOS-local maintenance tool — the .claude worktree path layout it scans is
# specific to a macOS developer machine (matches the sibling orphan_sweep.py).
"""
Prune warden verdict-store backups (LIA-200).

`_write_atomic()` in scripts/codex_warden_hooks.py writes a `{name}.bak-<stamp>`
copy on EVERY warden verdict-store write (stamp = UTC strftime "%Y%m%d%H%M%S",
14 digits), with no retention cap. The gates fire many times per session across
all worktrees, so these backups accumulate without bound. They are purely
defensive dead copies — `os.replace` already makes the live write atomic and
nothing ever reads a `.bak` back — so capping retention is safe.

This is a standalone maintenance tool (never touches the hot write path). It is
wired into scripts/maintenance.py's daily block, which launchd runs at 04:30
(com.deus.maintenance.plist), so it bounds accumulation on a real schedule.

It keeps the N newest backups per store path across the three in-repo families:

    <repo>/.claude/*.bak-*                         (main-worktree flat store)
    <repo>/.claude/worktree-markers/*/*.bak-*      (per-worktree marker buckets)
    <repo>/.claude/worktrees/*/.claude/*.bak-*     (full-checkout worktrees)

Because the stamp is fixed-width and zero-padded (YYYYMMDDHHMMSS), a plain
lexicographic sort of the filename is identical to chronological order, so
"newest N" needs no stat() and is filesystem-clock-independent.

Run with --help for flags (--keep / --dry-run / --verbose).
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# A backup is "<live store name>.bak-<14 digits>". The 14-digit anchor is what
# keeps this from ever matching a live store file (.warden-verdicts.json) or a
# tempfile left by an interrupted _write_atomic.
_BACKUP_SUFFIX = re.compile(r"\.bak-\d{14}$")

# Glob patterns rooted at <repo>/.claude that enumerate every backup family.
_FAMILY_GLOBS = (
    "*.bak-*",
    "worktree-markers/*/*.bak-*",
    "worktrees/*/.claude/*.bak-*",
)


def _store_key(path: Path) -> str:
    """Group key = the live store path a backup belongs to (its own dir plus the
    filename with the `.bak-<stamp>` suffix stripped). Two worktrees that both
    back up `.warden-verdicts.json` land in DIFFERENT groups because their parent
    directories differ."""
    return str(path.parent / _BACKUP_SUFFIX.sub("", path.name))


def find_backups(claude_dir: Path) -> list[Path]:
    """All warden `.bak-<14 digits>` files under a `.claude` directory."""
    found: list[Path] = []
    for pattern in _FAMILY_GLOBS:
        for p in claude_dir.glob(pattern):
            if p.is_file() and _BACKUP_SUFFIX.search(p.name):
                found.append(p)
    return found


def prune(
    claude_dir: Path, keep: int, dry_run: bool, verbose: bool
) -> tuple[int, int]:
    """Keep the `keep` newest backups per store path; delete the rest.

    Returns (deleted, kept). Best-effort: a single unlink failure (e.g. a
    concurrent writer removed the file first) never aborts the run."""
    groups: dict[str, list[Path]] = {}
    for f in find_backups(claude_dir):
        groups.setdefault(_store_key(f), []).append(f)

    deleted = 0
    kept = 0
    for key, files in sorted(groups.items()):
        # Newest first — lexicographic == chronological for fixed-width stamps.
        files.sort(key=lambda p: p.name, reverse=True)
        keepers, losers = files[:keep], files[keep:]
        kept += len(keepers)
        if verbose:
            for k in keepers:
                print(f"  keep   {k}")
        for old in losers:
            if dry_run:
                print(f"  prune  {old}")
                deleted += 1
                continue
            try:
                old.unlink()
                deleted += 1
            except FileNotFoundError:
                pass  # concurrent prune / writer already removed it
            except OSError as e:
                print(f"  WARN could not remove {old}: {e}", file=sys.stderr)
    return deleted, kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[2],
        help="Repo root (default: inferred from this script's location).",
    )
    parser.add_argument(
        "--keep", type=int, default=10,
        help="Backups to retain per store path (default: 10).",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be removed; delete nothing.")
    parser.add_argument("--verbose", action="store_true",
                        help="Also print the backups that are kept.")
    args = parser.parse_args(argv)

    if args.keep < 0:
        print("--keep must be >= 0", file=sys.stderr)
        return 1

    claude_dir = args.repo / ".claude"
    if not claude_dir.is_dir():
        # Nothing to prune is a success state, not an error.
        print(f"prune_warden_backups: no {claude_dir} — nothing to prune")
        return 0

    deleted, kept = prune(claude_dir, args.keep, args.dry_run, args.verbose)
    verb = "would prune" if args.dry_run else "pruned"
    print(f"prune_warden_backups: {verb} {deleted}, kept {kept} "
          f"(keep={args.keep} per store path)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
