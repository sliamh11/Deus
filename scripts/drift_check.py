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
  python3 scripts/drift_check.py              # drift check
  python3 scripts/drift_check.py --coverage   # report uncovered docs/
  npm run drift-check
"""
import argparse
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


def check_coverage(project_root: Path) -> int:
    """Report docs/ files that are not referenced by any pattern (informational)."""
    index = project_root / "patterns" / "INDEX.md"
    patterns_dir = project_root / "patterns"
    docs_dir = project_root / "docs"

    if not docs_dir.exists():
        print("No docs/ directory found.")
        return 0

    # Collect docs references from INDEX.md and all pattern files
    covered: set[str] = set()
    sources = [index] + list(patterns_dir.glob("*.md")) if index.exists() else list(patterns_dir.glob("*.md"))
    for src in sources:
        try:
            text = src.read_text()
        except FileNotFoundError:
            continue
        for match in re.finditer(r"docs/[\w./-]+\.md", text):
            covered.add(match.group(0))

    # Scan docs/ for all .md files (excluding decisions/ sub-docs individually — they're referenced via INDEX.md)
    uncovered: list[str] = []
    for doc_file in sorted(docs_dir.rglob("*.md")):
        rel = str(doc_file.relative_to(project_root))
        if rel not in covered:
            uncovered.append(rel)

    if not uncovered:
        print("All docs/ files are referenced by at least one pattern.")
        return 0

    print(f"Uncovered docs/ files ({len(uncovered)}) — no pattern distils these:")
    for f in uncovered:
        print(f"  {f}")
    print("\nConsider referencing them in patterns/INDEX.md or adding a new pattern.")
    return 0  # informational only — not a blocking failure


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Drift checker for pattern files.")
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Report docs/ files not referenced by any pattern (informational)",
    )
    args = parser.parse_args()

    if args.coverage:
        sys.exit(check_coverage(PROJECT_ROOT))
    else:
        sys.exit(main())
