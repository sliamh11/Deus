#!/usr/bin/env python3
"""
Generate a compact codebase map for token-efficient gate scoping.

Walks src/ and scripts/, extracts key exports/functions, writes a
structured Markdown map to .claude/codebase_map.md with a git-SHA
header for mtime-based invalidation.

Invalidation: if the map's stored SHA matches the current HEAD SHA,
the file is not regenerated (idempotent run).

Exit codes:
  0 — map written or already fresh
  1 — error (git unavailable, write failure)

Usage:
  python3 scripts/codebase_map.py                           # write to default path
  python3 scripts/codebase_map.py --output path/to/map.md  # custom output path
  python3 scripts/codebase_map.py --force                   # regenerate even if fresh
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / ".claude" / "codebase_map.md"

# Directories to walk (relative to project root)
SCAN_DIRS = ["src", "scripts"]

# Max depth for file-tree display (relative to each SCAN_DIR)
TREE_DEPTH = 1

# Skip these dirs entirely when walking
SKIP_DIRS = {
    "__pycache__",
    "node_modules",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    "bench",
    "deus-memory-mcp",
    "tests",
    "__tests__",
}

# Skip test files from both tree display and export extraction
TEST_SUFFIXES = (".test.ts", ".test.js", ".spec.ts", ".spec.js", "_test.py", ".test.py")

# TS/JS export regex - captures `export` declarations
_TS_EXPORT_RE = re.compile(
    r"^export\s+(?:default\s+)?(?:async\s+)?"
    r"(?:function|class|const|let|var|type|interface|enum)\s+(\w+)",
    re.MULTILINE,
)

# Also catch: export { Foo, Bar } re-exports
_TS_REEXPORT_RE = re.compile(r"^export\s*\{([^}]+)\}", re.MULTILINE)


def _git_head_sha(project_root: Path) -> str | None:
    """Return current HEAD SHA, or None if git is unavailable."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        sha = result.stdout.strip()
        return sha if sha else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def _stored_sha(map_path: Path) -> str | None:
    """Extract the SHA stored in the map file header, or None."""
    if not map_path.exists():
        return None
    try:
        first_line = map_path.open().readline()
        m = re.search(r"<!--\s*sha:\s*([a-f0-9]{40})\s*-->", first_line)
        return m.group(1) if m else None
    except OSError:
        return None


def _extract_py_exports(path: Path) -> list[str]:
    """Return top-level function and class names from a Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                names.append(node.name)
    return names


def _extract_ts_exports(path: Path) -> list[str]:
    """Return exported symbol names from a TypeScript/JavaScript file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    names: list[str] = []
    for m in _TS_EXPORT_RE.finditer(text):
        names.append(m.group(1))
    # Named re-exports: export { Foo, Bar as Baz }
    for m in _TS_REEXPORT_RE.finditer(text):
        for part in m.group(1).split(","):
            token = part.strip().split(" as ")[-1].strip()
            if token and token != "*" and re.match(r"^\w+$", token):
                if token not in names:
                    names.append(token)
    return names


def _build_tree_and_exports(
    scan_dir: Path,
    project_root: Path,
) -> tuple[list[str], dict[str, list[str]]]:
    """
    Walk scan_dir and return (tree_lines, exports_map).

    tree_lines: indented file-tree entries (depth <= TREE_DEPTH)
    exports_map: {rel_path_str: [exported_names]} for non-test source files
    """
    tree_lines: list[str] = []
    exports_map: dict[str, list[str]] = {}

    if not scan_dir.exists():
        return tree_lines, exports_map

    def _walk(dir_path: Path, depth: int, prefix: str) -> None:
        try:
            entries = sorted(dir_path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return

        for entry in entries:
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            rel = entry.relative_to(project_root)

            if entry.is_dir():
                if depth <= TREE_DEPTH:
                    tree_lines.append(f"{prefix}{entry.name}/")
                    _walk(entry, depth + 1, prefix + "  ")
            elif entry.is_file():
                # Hide test files from tree display
                is_test = any(entry.name.endswith(s) for s in TEST_SUFFIXES)
                if depth <= TREE_DEPTH and not is_test:
                    tree_lines.append(f"{prefix}{entry.name}")
                # Extract exports from non-test source files
                rel_str = str(rel)
                is_test = any(rel_str.endswith(s) for s in TEST_SUFFIXES)
                if not is_test:
                    if entry.suffix in (".ts", ".js", ".mjs"):
                        names = _extract_ts_exports(entry)
                        if names:
                            exports_map[rel_str] = names
                    elif entry.suffix == ".py":
                        names = _extract_py_exports(entry)
                        if names:
                            exports_map[rel_str] = names

    _walk(scan_dir, 0, "")
    return tree_lines, exports_map


_ARCH_SUMMARY = """\
Deus is a multi-channel AI assistant (WhatsApp, Telegram) built on TypeScript \
with Python tooling. Key layers:

- **Channels** (`src/channels/`): MCP adapters per platform + shared registry
- **Agent runtimes** (`src/agent-runtimes/`): Claude, OpenAI, llama-cpp backends; \
unified registry + resolve
- **Message pipeline** (`src/message-orchestrator.ts`, `src/pipeline.*.ts`, \
`src/router.ts`): inbound routing → agent dispatch → outbound
- **Memory** (`scripts/memory_*.py`): atom storage, tree retrieval, indexer, GC
- **Linear integration** (`src/linear-*.ts`): webhook, dispatcher, gate specs, \
warden-driven issue flow
- **Warden gates** (`.claude/agents/wardens/`): readiness, enrichment, code-review, \
plan-review, threat-model
- **Evolution/eval** (`evolution/`): judge models, benchmarks, reflexion loop
- **Config & DB** (`src/config.ts`, `src/db.ts`): env-driven config, SQLite storage\
"""


def generate_map(project_root: Path, output_path: Path, force: bool = False) -> int:
    """
    Generate (or skip if fresh) the codebase map at output_path.

    Returns 0 on success, 1 on error.
    """
    head_sha = _git_head_sha(project_root)

    # Invalidation check: skip if stored SHA matches HEAD
    if not force and head_sha is not None:
        stored = _stored_sha(output_path)
        if stored == head_sha:
            print(f"codebase_map: fresh (sha {head_sha[:8]}), skipping regeneration")
            return 0

    # Build map content
    lines: list[str] = []

    sha_line = f"<!-- sha: {head_sha} -->" if head_sha else "<!-- sha: unknown -->"
    lines.append(sha_line)
    lines.append("")
    lines.append("# Deus Codebase Map")
    lines.append("")
    lines.append("## Architecture")
    lines.append("")
    lines.append(_ARCH_SUMMARY)
    lines.append("")
    lines.append("## File Tree")
    lines.append("")

    all_exports: dict[str, list[str]] = {}
    for dir_name in SCAN_DIRS:
        scan_dir = project_root / dir_name
        tree, exports = _build_tree_and_exports(scan_dir, project_root)
        all_exports.update(exports)
        if tree:
            lines.append(f"### {dir_name}/")
            lines.append("```")
            lines.extend(tree)
            lines.append("```")
            lines.append("")

    lines.append("## Key Exports")
    lines.append("")
    lines.append("_Format: `path/to/file` → exported symbols_")
    lines.append("")

    # Emit exports grouped - skip files with too many exports (noisy)
    MAX_EXPORTS_PER_FILE = 5
    for rel_path, names in sorted(all_exports.items()):
        truncated = names[:MAX_EXPORTS_PER_FILE]
        suffix = ", ..." if len(names) > MAX_EXPORTS_PER_FILE else ""
        symbols = ", ".join(f"`{n}`" for n in truncated) + suffix
        lines.append(f"- `{rel_path}` → {symbols}")

    content = "\n".join(lines) + "\n"

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        output_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        print(f"ERROR: could not write {output_path}: {exc}", file=sys.stderr)
        return 1

    sha_display = head_sha[:8] if head_sha else "unknown"
    # Report byte size and approximate word count (gate agents use wc -w to verify)
    approx_kb = len(content.encode("utf-8")) / 1024
    print(f"codebase_map: written to {output_path} ({approx_kb:.1f} KB, sha {sha_display})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=str(DEFAULT_OUTPUT),
        help=f"Output path for the map (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate even if the stored SHA matches HEAD",
    )
    parser.add_argument(
        "--repo-root",
        metavar="DIR",
        default=str(PROJECT_ROOT),
        help="Project root (default: auto-detected from script location)",
    )
    args = parser.parse_args(argv)

    root = Path(args.repo_root).resolve()
    out = Path(args.output)
    if not out.is_absolute():
        out = root / out

    return generate_map(root, out, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
