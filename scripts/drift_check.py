#!/usr/bin/env python3
"""
Drift checker for pattern files.

Reads patterns/INDEX.md to discover pattern files, then checks each pattern's
YAML frontmatter `governs:` list against source file mtimes. Flags patterns
whose governed source has been modified since the pattern was last updated.

Exit codes:
  0 — all patterns up-to-date
  1 — one or more patterns drifted (governed source newer than pattern)
  2 — one or more governed paths are missing from the filesystem

Usage:
  python3 scripts/drift_check.py
  npm run drift-check
"""
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def parse_governs(pattern_path: Path) -> list[str]:
    """Extract the governs: list from a pattern file's YAML frontmatter."""
    try:
        text = pattern_path.read_text()
    except FileNotFoundError:
        return []

    # Match YAML frontmatter block between --- delimiters
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return []

    frontmatter = match.group(1)
    # Extract governs list items (lines starting with "  - ")
    governs: list[str] = []
    in_governs = False
    for line in frontmatter.splitlines():
        if line.strip().startswith("governs:"):
            in_governs = True
            continue
        if in_governs:
            stripped = line.strip()
            if stripped.startswith("- "):
                governs.append(stripped[2:].strip())
            elif stripped and not stripped.startswith("#"):
                in_governs = False
    return governs


def discover_patterns() -> list[Path]:
    """Find all pattern files listed in patterns/INDEX.md."""
    index = PROJECT_ROOT / "patterns" / "INDEX.md"
    if not index.exists():
        print(f"ERROR: {index} not found", file=sys.stderr)
        sys.exit(2)

    patterns: list[Path] = []
    for line in index.read_text().splitlines():
        # Match markdown links: [text](patterns/filename.md)
        # or backtick table cells: `patterns/filename.md`
        match = re.search(r"(?:\(|`)patterns/([^`)]+\.md)(?:\)|`)", line)
        if match:
            patterns.append(PROJECT_ROOT / "patterns" / match.group(1))
    return patterns


def main() -> int:
    patterns = discover_patterns()
    if not patterns:
        print("No patterns found in patterns/INDEX.md.")
        return 0

    rows: list[dict] = []
    exit_code = 0

    for pattern_path in patterns:
        if not pattern_path.exists():
            rows.append({
                "pattern": pattern_path.name,
                "status": "MISSING_PATTERN",
                "drifted": str(pattern_path),
            })
            exit_code = max(exit_code, 2)
            continue

        pattern_mtime = pattern_path.stat().st_mtime
        governs = parse_governs(pattern_path)

        drifted: list[str] = []
        for rel_path in governs:
            governed = PROJECT_ROOT / rel_path
            if not governed.exists():
                rows.append({
                    "pattern": pattern_path.name,
                    "status": "MISSING_GOVERNED",
                    "drifted": rel_path,
                })
                exit_code = max(exit_code, 2)
                continue

            if governed.is_dir():
                # For directories, check the most recently modified file inside
                mtimes = [f.stat().st_mtime for f in governed.rglob("*") if f.is_file()]
                governed_mtime = max(mtimes) if mtimes else 0.0
            else:
                governed_mtime = governed.stat().st_mtime

            if governed_mtime > pattern_mtime:
                drifted.append(rel_path)

        if drifted:
            rows.append({
                "pattern": pattern_path.name,
                "status": "DRIFTED",
                "drifted": ", ".join(drifted),
            })
            exit_code = max(exit_code, 1)
        else:
            rows.append({
                "pattern": pattern_path.name,
                "status": "OK",
                "drifted": "—",
            })

    # Print Markdown table
    col_w = max(len(r["pattern"]) for r in rows)
    status_w = max(len(r["status"]) for r in rows)
    drift_w = max(len(r["drifted"]) for r in rows)

    header = f"| {'pattern':<{col_w}} | {'status':<{status_w}} | {'drifted files':<{drift_w}} |"
    sep    = f"| {'-'*col_w} | {'-'*status_w} | {'-'*drift_w} |"
    print(header)
    print(sep)
    for r in rows:
        print(f"| {r['pattern']:<{col_w}} | {r['status']:<{status_w}} | {r['drifted']:<{drift_w}} |")

    if exit_code == 0:
        print("\nAll patterns up-to-date.")
    elif exit_code == 1:
        print("\nDRIFTED: update the flagged pattern files to match source changes.")
    else:
        print("\nMISSING: pattern file or governed path not found.")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
