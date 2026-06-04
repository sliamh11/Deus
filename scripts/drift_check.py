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
  python3 scripts/drift_check.py                   # drift check (mtime-based)
  python3 scripts/drift_check.py --bump            # touch drifted patterns to reset mtime
  python3 scripts/drift_check.py --coverage        # report uncovered docs/
  python3 scripts/drift_check.py --paths           # verify all pattern path refs exist
  python3 scripts/drift_check.py --adr             # flag patterns stale vs ADRs
  python3 scripts/drift_check.py --shadow          # check private/public symlink integrity
  python3 scripts/drift_check.py --indexes         # verify every indexed dir: INDEX.md refs match on-disk leaves
  python3 scripts/drift_check.py --bench-labels    # validate benchmark expected paths exist in vault
  python3 scripts/drift_check.py --bench-snapshot  # run benchmark and check against stored snapshot (local)
  python3 scripts/drift_check.py --all             # run every fast check above
  python3 scripts/drift_check.py --cache-map       # validate .claude/codebase_map.md is fresh (CI gating)
  python3 scripts/drift_check.py --codegraph-format # validate codegraph gate scan predicate + live transcript shape
  python3 scripts/drift_check.py --validate        # LLM pattern content check (slow)
  python3 scripts/drift_check.py --validate-router # LLM router selection check (slow)
  python3 scripts/drift_check.py --contradictions  # LLM cross-pattern contradictions (slow)
  npm run drift-check
"""
import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent


def _has_uncommitted_changes(path: Path, project_root: Path) -> bool:
    """True if `path` has uncommitted changes (tracked but modified, or
    untracked). Directories return True if any child has changes.
    """
    try:
        rel = path.relative_to(project_root)
    except ValueError:
        return False
    try:
        # `git status --porcelain -- <path>` returns one line per changed
        # entry (tracked-modified OR untracked). Empty output = clean.
        result = subprocess.run(
            ["git", "status", "--porcelain", "--", str(rel)],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _git_commit_time(path: Path, project_root: Path) -> float:
    """Return the unix timestamp of the last commit that touched `path`.

    Precedence:
      1. If the file has uncommitted changes (working tree), use its mtime
         so local edits are caught immediately before you commit.
      2. Otherwise use `git log -1 --format=%ct -- <path>` (reproducible
         across fresh clones, including CI).
      3. Fall back to filesystem mtime if git is unavailable or the file
         is untracked and clean (shouldn't happen in practice).

    Using commit time on CI avoids false drift reports: `git checkout`
    sets every file's mtime to the clone time, which would make every
    pattern look "drifted" against every source file otherwise.
    """
    try:
        rel = path.relative_to(project_root)
    except ValueError:
        return path.stat().st_mtime if path.exists() else 0.0

    if _has_uncommitted_changes(path, project_root):
        return path.stat().st_mtime if path.exists() else 0.0

    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", str(rel)],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = result.stdout.strip()
        if result.returncode == 0 and out:
            return float(out)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass
    return path.stat().st_mtime if path.exists() else 0.0


def _dir_commit_time(dir_path: Path, project_root: Path) -> float:
    """Return the commit timestamp of the most recently committed file
    inside `dir_path`, falling back to an mtime walk if the directory has
    uncommitted changes (so local edits are caught immediately).

    Build artifact dirs (__pycache__, dist/, node_modules/, caches) are
    always skipped in the mtime-walk fallback.
    """
    skip_dirs = {"__pycache__", "node_modules", "dist", ".pytest_cache", ".mypy_cache"}
    skip_suffixes = {".pyc", ".pyo"}

    try:
        rel = dir_path.relative_to(project_root)
    except ValueError:
        return 0.0

    # Local-dev fast path: if anything inside the dir is dirty, walk mtimes.
    if _has_uncommitted_changes(dir_path, project_root):
        mtimes: list[float] = []
        for f in dir_path.rglob("*"):
            if not f.is_file():
                continue
            if any(part in skip_dirs for part in f.parts):
                continue
            if f.suffix in skip_suffixes:
                continue
            mtimes.append(f.stat().st_mtime)
        return max(mtimes) if mtimes else 0.0

    # Clean tree: use git log scoped to the directory (reproducible on CI).
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", str(rel)],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        out = result.stdout.strip()
        if result.returncode == 0 and out:
            return float(out)
    except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
        pass

    # Untracked-but-clean fallback: walk mtimes.
    mtimes_fallback: list[float] = []
    for f in dir_path.rglob("*"):
        if not f.is_file():
            continue
        if any(part in skip_dirs for part in f.parts):
            continue
        if f.suffix in skip_suffixes:
            continue
        mtimes_fallback.append(f.stat().st_mtime)
    return max(mtimes_fallback) if mtimes_fallback else 0.0


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


def _changed_files_since(base_ref: str, project_root: Path) -> set[str]:
    """Return set of file paths changed between base_ref and HEAD."""
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}...HEAD"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return {f.strip() for f in result.stdout.strip().splitlines() if f.strip()}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return set()


def _file_in_changed_set(rel_path: str, changed: set[str], project_root: Path) -> bool:
    """Check if a governed path (file or directory prefix) overlaps with any changed file."""
    target = Path(rel_path)
    for f in changed:
        fp = Path(f)
        # Direct match or the changed file is under the governed directory
        if fp == target or str(fp).startswith(rel_path.rstrip("/") + "/"):
            return True
    return False


def main(base_ref: str | None = None, bump: bool = False) -> int:
    patterns = discover_patterns()
    if not patterns:
        print("No patterns found in patterns/INDEX.md.")
        return 0

    # When --base is given, only check governed files that changed in this PR.
    # This prevents cascading drift failures across sequential PRs: if a governed
    # file wasn't touched in this PR, any apparent drift comes from a prior merge
    # that already included the pattern bump.
    changed_files: set[str] | None = None
    if base_ref:
        changed_files = _changed_files_since(base_ref, PROJECT_ROOT)

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

        pattern_time = _git_commit_time(pattern_path, PROJECT_ROOT)
        governs = parse_governs(pattern_path)

        # In --base mode: if the pattern file itself is changed in this PR,
        # skip drift check entirely — the pattern is being updated alongside
        # its governed files in the same PR. This handles the case where
        # multiple commits within a single PR touch pattern and source at
        # different times.
        if changed_files is not None:
            pattern_rel = str(pattern_path.relative_to(PROJECT_ROOT))
            if pattern_rel in changed_files:
                rows.append({
                    "pattern": pattern_path.name,
                    "status": "OK",
                    "drifted": "—",
                })
                continue

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

            # In --base mode: skip governed files not changed in this PR.
            if changed_files is not None and not _file_in_changed_set(rel_path, changed_files, PROJECT_ROOT):
                continue

            if governed.is_dir():
                governed_time = _dir_commit_time(governed, PROJECT_ROOT)
            else:
                governed_time = _git_commit_time(governed, PROJECT_ROOT)

            # 1-second tolerance absorbs rounding noise between git commit
            # timestamps; drift must be strictly later than the pattern.
            if governed_time > pattern_time + 1.0:
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
        if bump:
            import datetime
            import time as _time
            today = datetime.date.today().isoformat()
            ts = int(_time.time())
            drifted_patterns = [r for r in rows if r["status"] == "DRIFTED"]
            for r in drifted_patterns:
                p = PROJECT_ROOT / "patterns" / r["pattern"]
                text = p.read_text()
                updated = re.sub(
                    r'(last_verified:\s*)"?\d{4}-\d{2}-\d{2}"?.*',
                    rf'\g<1>"{today}" # auto-bump @{ts}',
                    text,
                )
                if updated != text:
                    p.write_text(updated)
                    print(f"  bumped {r['pattern']} → {today}")
                else:
                    p.touch()
                    print(f"  touched {r['pattern']} (no last_verified field)")
            print(f"\nBumped {len(drifted_patterns)} pattern(s). Stage them with your commit.")
            return 0
        print("\nDRIFTED: update the flagged pattern files to match source changes.")
        print("Run with --bump to touch drifted patterns (resets their mtime).")
    else:
        print("\nMISSING: pattern file or governed path not found.")

    return exit_code


def extract_body_paths(pattern_text: str) -> set[str]:
    """Extract backtick-quoted repo file paths from a pattern's body.

    Only returns tokens that look like concrete files under known top-level
    directories. Globs, placeholders, and URL-like tokens are skipped so the
    check stays deterministic.
    """
    # Strip frontmatter before scanning so governs: paths aren't double-counted.
    body_match = re.match(r"^---\s*\n.*?\n---\s*\n(.*)", pattern_text, re.DOTALL)
    body = body_match.group(1) if body_match else pattern_text

    # Explicit allowlist of top-level directories keeps false positives low
    # (e.g. skips things like `node_modules/foo` or random CLI args).
    top_dirs = r"(?:src|scripts|patterns|docs|container|packages|eval|evolution|setup|tests|\.claude|\.mex)"
    rx = rf"`({top_dirs}/[\w./*-]+?)`"

    found: set[str] = set()
    for match in re.finditer(rx, body):
        path = match.group(1)
        # Skip globs, template placeholders, and wildcards — they're not verifiable.
        if any(ch in path for ch in "*{<"):
            continue
        found.add(path.rstrip("/"))
    return found


def check_paths(project_root: Path) -> int:
    """Verify every repo path referenced by any pattern actually exists.

    Two sources of path references are checked:
      - frontmatter `governs:` lists (bookkeeping for drift check)
      - inline backtick-quoted paths in the pattern body

    The body check catches references that are visible to Claude when reading
    a pattern but never validated — e.g. a pattern citing `src/server-base.ts`
    long after the file was renamed.
    """
    patterns = discover_patterns()
    if not patterns:
        print("No patterns found in patterns/INDEX.md.")
        return 0

    missing: list[tuple[str, str, str]] = []  # (pattern_name, path, source)

    for pattern_path in patterns:
        if not pattern_path.exists():
            missing.append((pattern_path.name, str(pattern_path), "pattern file"))
            continue

        text = pattern_path.read_text()

        # 1. governs: paths (frontmatter)
        for rel_path in parse_governs(pattern_path):
            if not (project_root / rel_path).exists():
                missing.append((pattern_path.name, rel_path, "governs"))

        # 2. inline backtick-quoted paths (body)
        for rel_path in extract_body_paths(text):
            if not (project_root / rel_path).exists():
                missing.append((pattern_path.name, rel_path, "body"))

    if not missing:
        print(f"All pattern paths exist ({len(patterns)} patterns checked).")
        return 0

    print(f"Missing paths ({len(missing)}):")
    for pattern_name, path, source in missing:
        print(f"  {pattern_name} [{source}]: {path}")
    print("\nFIX: update or remove the stale references, then re-run.")
    return 1


def _normalize_path(p: str) -> str:
    """Strip backticks, whitespace, and trailing slash from a scope/governs token."""
    return p.strip().strip("`").rstrip("/")


def _paths_overlap(a: str, b: str) -> bool:
    """True if two paths refer to overlapping filesystem locations.

    `src/` overlaps with `src/startup-gate.ts`. `eval/` does not overlap
    with `evolution/`. Exact matches always overlap.
    """
    a = _normalize_path(a)
    b = _normalize_path(b)
    if a == b:
        return True
    return a.startswith(b + "/") or b.startswith(a + "/")


def parse_adr(adr_path: Path) -> dict | None:
    """Extract Date and Scope from an ADR markdown file.

    Looks in the first ~20 header lines for `**Date:** YYYY-MM-DD` and
    `**Scope:** path1, path2, ...`. Returns None if Date is missing.
    Scopes may be comma-separated and individually backtick-quoted.
    """
    try:
        text = adr_path.read_text()
    except FileNotFoundError:
        return None

    header = "\n".join(text.splitlines()[:20])

    # Use [ \t]* instead of \s* so the regex can't cross line boundaries and
    # accidentally pick up content from the next header line.
    date_match = re.search(r"\*\*Date:\*\*[ \t]*(\d{4}-\d{2}-\d{2})", header)
    if not date_match:
        return None

    scope_match = re.search(r"\*\*Scope:\*\*[ \t]*(.*)", header)
    scope_raw = scope_match.group(1).strip() if scope_match else ""
    scopes = [_normalize_path(s) for s in scope_raw.split(",") if s.strip()]

    return {"date": date_match.group(1), "scopes": scopes}


def parse_last_verified(pattern_path: Path) -> str | None:
    """Extract last_verified date from a pattern's YAML frontmatter."""
    try:
        text = pattern_path.read_text()
    except FileNotFoundError:
        return None
    match = re.search(r'^last_verified:\s*"?(\d{4}-\d{2}-\d{2})"?', text, re.MULTILINE)
    return match.group(1) if match else None


def parse_test_tasks(pattern_path: Path) -> list[str]:
    """Extract the test_tasks list from a pattern's YAML frontmatter.

    Handles quoted and unquoted list items. Stops at the next top-level
    frontmatter key.
    """
    try:
        text = pattern_path.read_text()
    except FileNotFoundError:
        return []
    match = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return []
    frontmatter = match.group(1)

    tasks: list[str] = []
    in_tasks = False
    for line in frontmatter.splitlines():
        if line.strip().startswith("test_tasks:"):
            in_tasks = True
            continue
        if in_tasks:
            stripped = line.strip()
            if stripped.startswith("- "):
                value = stripped[2:].strip()
                # Strip matching surrounding quotes if present.
                if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                    value = value[1:-1]
                tasks.append(value)
            elif stripped and not stripped.startswith("#"):
                in_tasks = False
    return tasks


def check_test_tasks(project_root: Path, minimum: int = 3) -> int:
    """Verify every pattern has at least `minimum` test_tasks entries.

    test_tasks feed the --validate LLM check. Without them, a pattern has
    no golden inputs to test against, and correctness gaps can't be caught.
    """
    patterns = discover_patterns()
    if not patterns:
        return 0

    insufficient: list[tuple[str, int]] = []
    for p in patterns:
        if not p.exists():
            continue
        tasks = parse_test_tasks(p)
        if len(tasks) < minimum:
            insufficient.append((p.name, len(tasks)))

    if not insufficient:
        print(f"All patterns have >= {minimum} test_tasks ({len(patterns)} patterns).")
        return 0

    print(f"Patterns with insufficient test_tasks ({len(insufficient)}):")
    for name, count in insufficient:
        print(f"  {name}: {count} entries (minimum {minimum})")
    print(
        "\nFIX: add a test_tasks: list (3+ short task descriptions) to each "
        "pattern's YAML frontmatter. These feed the --validate LLM check."
    )
    return 1


def check_adr(project_root: Path) -> int:
    """Flag patterns whose `last_verified:` predates an ADR touching their scope.

    Pattern freshness is semantic: if an ADR was decided after the pattern was
    last reviewed, and the ADR's Scope overlaps the pattern's governs list,
    the pattern may be missing new constraints and should be re-reviewed.
    """
    patterns = discover_patterns()
    adr_dir = project_root / "docs" / "decisions"

    if not adr_dir.exists():
        print("No docs/decisions/ directory — skipping ADR freshness check.")
        return 0

    adrs: list[dict] = []
    warnings: list[str] = []
    for adr_path in sorted(adr_dir.glob("*.md")):
        if adr_path.name == "INDEX.md":
            continue
        parsed = parse_adr(adr_path)
        rel = adr_path.relative_to(project_root)
        if parsed is None:
            warnings.append(f"  {rel}: missing **Date:** field — add it to enable freshness checks")
            continue
        if not parsed["scopes"]:
            warnings.append(f"  {rel}: missing **Scope:** field — add it to enable pattern mapping")
            continue
        parsed["name"] = adr_path.name
        adrs.append(parsed)

    if warnings:
        print("ADR frontmatter warnings:")
        for w in warnings:
            print(w)
        print()

    stale: list[tuple[str, str, str, str]] = []
    for pattern_path in patterns:
        if not pattern_path.exists():
            continue
        last_verified = parse_last_verified(pattern_path)
        if last_verified is None:
            continue
        governs = parse_governs(pattern_path)
        if not governs:
            continue

        for adr in adrs:
            if adr["date"] <= last_verified:
                continue  # ADR pre-dates the last review
            if any(_paths_overlap(g, s) for g in governs for s in adr["scopes"]):
                stale.append((pattern_path.name, adr["name"], adr["date"], last_verified))

    exit_code = 1 if (stale or warnings) else 0

    if not stale:
        if not warnings:
            print(f"All patterns fresh vs ADRs ({len(patterns)} patterns × {len(adrs)} ADRs).")
        return exit_code

    print(f"Stale patterns ({len(stale)}):")
    for pattern, adr, adr_date, pat_date in stale:
        print(f"  {pattern} (last_verified: {pat_date}) ← {adr} ({adr_date})")
    print('\nFIX: re-review each flagged pattern, then bump its last_verified: to today.')
    return 1


def _load_source_docs(project_root: Path) -> dict[str, str]:
    """Load every source doc that patterns distill from.

    Returns a dict of {relative_path: content}. Missing files are silently
    skipped — the set is additive, not required.
    """
    docs: dict[str, str] = {}
    docs_dir = project_root / "docs"
    if docs_dir.exists():
        for md in sorted(docs_dir.rglob("*.md")):
            rel = str(md.relative_to(project_root))
            docs[rel] = md.read_text()
    return docs


def check_validate(project_root: Path, pattern_filter: str | None = None) -> int:
    """LLM-based correctness check — the behavioral backstop.

    For every test_task in each pattern:
      1. Planner LLM: given only ROUTER.md + pattern, produce a list of
         rules/steps it would follow for the task.
      2. Auditor LLM: given the plan + full source docs, report any rules
         from the source docs that the plan missed.

    If the auditor finds gaps, the pattern's content is incomplete for that
    task class — re-distil. Exits 0 if no Gemini key is available (so CI
    without a key doesn't block; scheduled runs still enforce).
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as e:
        print(f"SKIP: --validate needs google-genai ({e})", file=sys.stderr)
        return 0

    try:
        from evolution.config import GEN_MODELS, load_api_key
    except ImportError as e:
        print(f"SKIP: --validate needs evolution.config ({e})", file=sys.stderr)
        return 0

    try:
        api_key = load_api_key()
    except RuntimeError as e:
        print(f"SKIP: --validate needs GEMINI_API_KEY ({e})", file=sys.stderr)
        return 0

    client = genai.Client(api_key=api_key)

    patterns = discover_patterns()
    if pattern_filter:
        patterns = [p for p in patterns if pattern_filter in p.name]
        if not patterns:
            print(f"No patterns match filter: {pattern_filter}")
            return 0

    router_path = project_root / ".mex" / "ROUTER.md"
    router = router_path.read_text() if router_path.exists() else ""

    # general-code.md holds universal rules that apply to every pattern
    # (ROUTER.md §Universal rules). The planner always sees them.
    universal_path = project_root / "patterns" / "general-code.md"
    universal_rules = universal_path.read_text() if universal_path.exists() else ""

    source_docs = _load_source_docs(project_root)
    source_blob = "\n\n".join(
        f"### {rel}\n{content}" for rel, content in source_docs.items()
    )

    def call_gemini(prompt: str) -> str | None:
        """Call Gemini with model fallback on quota exhaustion."""
        for model in GEN_MODELS:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.1, max_output_tokens=2048
                    ),
                )
                return (response.text or "").strip()
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    continue
                print(f"  WARN: Gemini error ({model}): {e}", file=sys.stderr)
                return None
        print("  WARN: all Gemini models quota-exhausted", file=sys.stderr)
        return None

    total_gaps = 0
    total_tasks = 0
    total_failures = 0

    for pattern_path in patterns:
        if not pattern_path.exists():
            continue
        pattern_content = pattern_path.read_text()
        tasks = parse_test_tasks(pattern_path)
        if not tasks:
            print(f"SKIP {pattern_path.name}: no test_tasks defined")
            continue

        print(f"\n=== {pattern_path.name} ({len(tasks)} tasks) ===")

        is_general = pattern_path.name == "general-code.md"

        for task in tasks:
            total_tasks += 1

            # Always include the universal rules (from general-code.md) alongside
            # the task-specific pattern — ROUTER.md says they apply to every task.
            # When validating general-code.md itself, skip the duplicate block.
            extra = "" if is_general else (
                f"\n\n=== UNIVERSAL RULES (always apply) ===\n{universal_rules}"
            )

            planner_prompt = (
                "You are an AI assistant planning a code change. You have ONLY "
                "the routing guide, task pattern file, and universal rules "
                "below — no other context.\n\n"
                f"=== ROUTING GUIDE ===\n{router}\n\n"
                f"=== PATTERN FILE ===\n{pattern_content}"
                f"{extra}\n\n"
                f"=== TASK ===\n{task}\n\n"
                "List every constraint, rule, or required step you would follow "
                "while doing this task, based ONLY on the text above. Output a "
                "flat numbered list, one rule per line. Do not invent rules not "
                "in the text."
            )
            plan = call_gemini(planner_prompt)
            if plan is None:
                print(f"  FAIL to plan: {task}")
                total_failures += 1
                continue

            auditor_prompt = (
                "You are auditing an AI assistant's plan for a code change. "
                "You have the FULL source documentation below. Your job: find "
                "rules the plan missed that the source docs require for THIS "
                "specific task.\n\n"
                f"=== SOURCE DOCS ===\n{source_blob}\n\n"
                f"=== TASK ===\n{task}\n\n"
                f"=== PLAN ===\n{plan}\n\n"
                "Strict rules for what counts as a GAP:\n"
                "1. The rule must exist in the SOURCE DOCS above.\n"
                "2. The rule must be DIRECTLY required for this specific task "
                "(not 'might apply if X'). If the task is e.g. 'rebuild an MCP "
                "package', MCP-package-specific rules count; unrelated rules "
                "about channel registration do NOT.\n"
                "3. Do NOT flag generic software hygiene (write tests, use "
                "branches, follow commit format) UNLESS the task text literally "
                "describes a source code change. A pure deploy/restart task "
                "does not add code.\n"
                "4. Do NOT flag a rule that the plan already covers with "
                "different wording.\n\n"
                "If the plan is complete under these rules, respond with "
                "exactly: NO_GAPS\n"
                "Otherwise output a flat numbered list, one gap per line, "
                "each gap citing the source doc section where the rule lives."
            )
            audit = call_gemini(auditor_prompt)
            if audit is None:
                print(f"  FAIL to audit: {task}")
                total_failures += 1
                continue

            if "NO_GAPS" in audit.upper():
                print(f"  OK: {task}")
            else:
                total_gaps += 1
                print(f"  GAP: {task}")
                for line in audit.splitlines():
                    if line.strip():
                        print(f"      {line}")

    print("\n=== VALIDATE SUMMARY ===")
    print(f"Tasks checked: {total_tasks}")
    print(f"Gaps found: {total_gaps}")
    if total_failures:
        print(f"Failures (unable to check): {total_failures}")
    return 1 if total_gaps > 0 else 0


def _normalize_router_response(name: str, valid: list[str]) -> str:
    """Normalize an LLM's router response to a canonical pattern filename.

    Handles common response variants from Gemini:
      - leading path (`patterns/foo.md` → `foo.md`)
      - missing `.md` suffix (`foo` → `foo.md` if that pattern exists)
      - truncation (`cross-` → `cross-platform.md` via unique-prefix match)
      - prose prefix (`The answer is foo.md` → `foo.md` if possible)
      - empty response → empty string (preserved as an explicit failure)

    Returned string is always lowercased. If normalization fails, returns
    the lowercased cleaned token so the caller can still report it as a
    mismatch.
    """
    s = name.strip().strip("`").strip("'\"").lower()
    if not s:
        return ""
    # Strip any leading path segments the model might have added.
    if "/" in s:
        s = s.rsplit("/", 1)[1]
    # Keep only the first token — the model sometimes prepends prose.
    tokens = s.split()
    s = tokens[0] if tokens else s
    s = s.strip(".,;:")
    if s in valid:
        return s
    if not s.endswith(".md") and f"{s}.md" in valid:
        return f"{s}.md"
    # Unique-prefix match against valid filenames (handles truncation).
    prefix_matches = [v for v in valid if v.startswith(s)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]
    # Retry prefix match after stripping trailing non-alphanumerics.
    s_clean = re.sub(r"[^a-z0-9]+$", "", s)
    if s_clean and s_clean != s:
        prefix_matches = [v for v in valid if v.startswith(s_clean)]
        if len(prefix_matches) == 1:
            return prefix_matches[0]
    return s


def check_validate_router(project_root: Path, pattern_filter: str | None = None) -> int:
    """LLM-based router validation — closes the router-selection blind spot.

    `--validate` tests each pattern's content in isolation. This check tests
    the other half: given a task, does ROUTER.md route it to the correct
    pattern? For every test_task in every pattern, ask an LLM which pattern
    file it would load (given only ROUTER.md and the list of valid pattern
    names). If the answer doesn't match the pattern the task was declared in,
    the router has a gap.

    Uses temperature=0.0 and a constrained output format so the comparison
    is deterministic. Skips gracefully without GEMINI_API_KEY.
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as e:
        print(f"SKIP: --validate-router needs google-genai ({e})", file=sys.stderr)
        return 0

    try:
        from evolution.config import GEN_MODELS, load_api_key
    except ImportError as e:
        print(f"SKIP: --validate-router needs evolution.config ({e})", file=sys.stderr)
        return 0

    try:
        api_key = load_api_key()
    except RuntimeError as e:
        print(f"SKIP: --validate-router needs GEMINI_API_KEY ({e})", file=sys.stderr)
        return 0

    router_path = project_root / ".mex" / "ROUTER.md"
    if not router_path.exists():
        print("SKIP: .mex/ROUTER.md not found")
        return 0
    router = router_path.read_text()

    client = genai.Client(api_key=api_key)

    patterns = discover_patterns()
    if pattern_filter:
        patterns = [p for p in patterns if pattern_filter in p.name]
        if not patterns:
            print(f"No patterns match filter: {pattern_filter}")
            return 0

    # Build the allowed-answer list so the planner can only name real files.
    valid_names = sorted({p.name for p in patterns if p.exists()})
    valid_list = "\n".join(f"  - {n}" for n in valid_names)

    def call_gemini(prompt: str) -> str | None:
        for model in GEN_MODELS:
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=genai_types.GenerateContentConfig(
                        temperature=0.0, max_output_tokens=256
                    ),
                )
                return (response.text or "").strip()
            except Exception as e:
                err = str(e)
                if "429" in err or "RESOURCE_EXHAUSTED" in err:
                    continue
                print(f"  WARN: Gemini error ({model}): {e}", file=sys.stderr)
                return None
        print("  WARN: all Gemini models quota-exhausted", file=sys.stderr)
        return None

    mismatches: list[tuple[str, str, str]] = []  # (expected, task, chosen)
    total_tasks = 0
    failures = 0

    for pattern_path in patterns:
        if not pattern_path.exists():
            continue
        tasks = parse_test_tasks(pattern_path)
        if not tasks:
            continue
        expected = pattern_path.name

        print(f"\n=== {expected} ({len(tasks)} tasks) ===")

        for task in tasks:
            total_tasks += 1

            prompt = (
                "You are routing a code task to the correct pattern file. You "
                "have ONLY the routing guide below — no other context.\n\n"
                f"=== ROUTING GUIDE ===\n{router}\n\n"
                f"=== VALID PATTERN FILES ===\n{valid_list}\n\n"
                f"=== TASK ===\n{task}\n\n"
                "Pick the single most specific pattern file for this task. "
                "Respond with EXACTLY the filename (e.g. `deployment.md`), "
                "nothing else. No path, no quotes, no explanation. If no "
                "specific pattern fits, respond with `general-code.md`."
            )

            response = call_gemini(prompt)
            if response is None:
                print(f"  FAIL: {task}")
                failures += 1
                continue

            chosen = _normalize_router_response(response, valid_names)
            expected_norm = expected.lower()

            if chosen == expected_norm:
                print(f"  OK: {task}")
            else:
                mismatches.append((expected, task, chosen))
                print(f"  MISMATCH: {task}")
                print(f"      expected: {expected}")
                print(f"      chosen:   {chosen}")

    print("\n=== VALIDATE-ROUTER SUMMARY ===")
    print(f"Tasks checked: {total_tasks}")
    print(f"Mismatches: {len(mismatches)}")
    if failures:
        print(f"Failures (unable to check): {failures}")
    if mismatches:
        print(
            "\nFIX: a mismatch means either the router is picking the wrong "
            "pattern for this task class, or the test_task is too generic to "
            "disambiguate. Tighten ROUTER.md or reword the test_task."
        )
    return 1 if mismatches else 0


def check_contradictions(project_root: Path, pattern_filter: str | None = None) -> int:
    """LLM-based cross-pattern contradictions check.

    Loads all pattern bodies into a single prompt and asks the LLM to find
    rules that directly contradict each other across different patterns.
    Not wired into --all (opt-in only, LLM-based).
    """
    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError as e:
        print(f"SKIP: --contradictions needs google-genai ({e})", file=sys.stderr)
        return 0

    try:
        from evolution.config import GEN_MODELS, load_api_key
    except ImportError as e:
        print(f"SKIP: --contradictions needs evolution.config ({e})", file=sys.stderr)
        return 0

    try:
        api_key = load_api_key()
    except RuntimeError as e:
        print(f"SKIP: --contradictions needs GEMINI_API_KEY ({e})", file=sys.stderr)
        return 0

    patterns = discover_patterns()
    if pattern_filter:
        patterns = [p for p in patterns if pattern_filter in p.name]
        if not patterns:
            print(f"No patterns match filter: {pattern_filter}")
            return 0

    # Build a single blob with all pattern contents, separated by filename
    blob_parts: list[str] = []
    for p in patterns:
        if not p.exists():
            continue
        blob_parts.append(f"--- {p.name} ---\n{p.read_text()}")

    if not blob_parts:
        print("No pattern files found.")
        return 0

    patterns_blob = "\n\n".join(blob_parts)

    prompt = (
        f"{patterns_blob}\n\n"
        "Above are pattern files — cheat-sheets for different task types.\n\n"
        "Find DIRECT contradictions: pattern A says DO X, pattern B says "
        "DON'T DO X, and both apply to the exact same situation. A developer "
        "following both patterns simultaneously would receive impossible "
        "instructions.\n\n"
        "NOT contradictions (never flag these):\n"
        "- Different rules for different components or contexts\n"
        "- Same rule stated with different wording (agreement)\n"
        "- One rule more specific than another (refinement)\n"
        "- Patterns that agree on a classification (e.g., if both call "
        "something 'static' and say it goes in .env, that is agreement)\n"
        "- Anything requiring external domain knowledge to judge — only "
        "flag what the text itself makes contradictory\n\n"
        "Be conservative. When in doubt, it is NOT a contradiction.\n\n"
        "Respond NO_CONTRADICTIONS if none found.\n"
        "Otherwise: CONTRADICTION: <a.md> \"<rule>\" vs <b.md> \"<rule>\""
    )

    client = genai.Client(api_key=api_key)
    response_text = None
    for model in GEN_MODELS:
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    temperature=0.0, max_output_tokens=2048
                ),
            )
            response_text = (response.text or "").strip()
            break
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                continue
            print(f"WARN: Gemini error ({model}): {e}", file=sys.stderr)
            return 0
    else:
        print("WARN: all Gemini models quota-exhausted", file=sys.stderr)
        return 0

    if not response_text:
        print("WARN: empty response from Gemini", file=sys.stderr)
        return 0

    if "NO_CONTRADICTIONS" in response_text:
        print(f"No contradictions found across {len(blob_parts)} patterns.")
        return 0

    print(f"=== CONTRADICTIONS FOUND ===\n{response_text}")
    return 1



def check_agent_native_mcp(project_root: Path) -> int:
    """Verify new MCP tool registrations include compact/select params.

    Scans packages/mcp-*/src/ for server.tool() calls and checks that
    each has compact and select in its schema. Blocking — fails CI on
    any violation. Baseline was cleared by PR #448 before this gate
    was activated; see git history for the activation commit.
    """
    packages_dir = project_root / "packages"
    if not packages_dir.exists():
        return 0

    violations: list[str] = []
    for pkg in sorted(packages_dir.iterdir()):
        if not pkg.name.startswith("mcp-") or not pkg.is_dir():
            continue
        src_dir = pkg / "src"
        if not src_dir.exists():
            continue
        for ts_file in sorted(src_dir.glob("*.ts")):
            text = ts_file.read_text(errors="replace")
            lines = text.splitlines()
            for i, line in enumerate(lines, 1):
                if "server.tool(" in line:
                    # Schema check (15-line window): zod schema literals sit in
                    # the call's third positional arg. 15 lines is wide enough
                    # for the largest schemas in this codebase (e.g. mcp-gcal
                    # `create_event` has 6 typed fields plus compact/select)
                    # while still being narrower than the action-marker scan
                    # below, so we don't accidentally match handler-body usage.
                    # Match against `compact:` / `select:` with trailing colon
                    # (zod schema syntax) so descriptions that mention
                    # `compact=true` as a usage hint don't cause false-negatives.
                    block = "\n".join(lines[i - 1 : i + 15])
                    if "compact:" not in block and "select:" not in block:
                        # Action-marker scan uses a wider window (30 lines)
                        # because confirmation strings can appear deep in the
                        # handler body — e.g., server-base.ts `connect` returns
                        # 'Connected.' 10 lines below the server.tool() decl;
                        # `disconnect` returns 'Disconnected.' 14 lines below.
                        action_block = "\n".join(lines[i - 1 : i + 30])
                        action_markers = (
                            "'Message sent.'",
                            "'OK'",
                            "'Connected.'",
                            "'Disconnected.'",
                            "'Email sent.'",
                            "'Liked.'",
                            "'Like removed.'",
                            "'Retweeted.'",
                            "'Retweet removed.'",
                            "mcpResponse({",
                        )
                        if any(m in action_block for m in action_markers):
                            continue
                        rel = ts_file.relative_to(project_root)
                        violations.append(f"  {rel}:{i} — server.tool() without compact/select params")

    if violations:
        print(f"MCP tools missing agent-native params ({len(violations)}):")
        for v in violations:
            print(v)
        print("\nFIX: add compact: z.boolean().optional(), select: z.string().optional() to tool schemas.")
        print("See docs/decisions/printing-press-adoption.md and patterns/channel-add.md.")
        return 1  # blocking — fails CI on any new violation
    else:
        print(f"All MCP tool registrations include agent-native params.")
    return 0


def check_mcp_description_hints(project_root: Path) -> int:
    """Verify MCP tools with compact/select schemas mention hints in descriptions.

    Sibling to check_agent_native_mcp. For every server.tool() block whose
    schema includes BOTH compact and select (i.e., the tool participates in
    field projection), the description string should mention 'select',
    'compact', or 'payload' so the LLM caller learns when to pass them.

    Scope limits (documented to avoid surprise):
      - AND requirement: half-projection tools (compact-only OR select-only)
        are skipped — none exist in the codebase today.
      - Multi-line descriptions (e.g., 'foo ' + 'bar') are not supported;
        only the first quoted-string line after the tool name is checked.

    Blocking — fails CI on any violation. Baseline was cleared by PR
    #448 before this gate was activated alongside its agent-native
    sibling; see git history for the activation commit.
    See docs/decisions/printing-press-adoption.md "Empirical findings".
    """
    packages_dir = project_root / "packages"
    if not packages_dir.exists():
        return 0

    hint_keywords = ("select", "compact", "payload")
    violations: list[str] = []
    for pkg in sorted(packages_dir.iterdir()):
        if not pkg.name.startswith("mcp-") or not pkg.is_dir():
            continue
        src_dir = pkg / "src"
        if not src_dir.exists():
            continue
        for ts_file in sorted(src_dir.glob("*.ts")):
            text = ts_file.read_text(errors="replace")
            lines = text.splitlines()
            for i, line in enumerate(lines, 1):
                if "server.tool(" not in line:
                    continue
                # Look at the next 12 lines for the schema + description.
                block = "\n".join(lines[i - 1 : i + 12])
                # Only applies to tools whose schema accepts BOTH compact AND select.
                if "compact" not in block or "select" not in block:
                    continue
                # Description is the second quoted-string line after server.tool(
                # (the first is the tool name).
                quoted: list[str] = []
                for follow in lines[i : i + 6]:
                    stripped = follow.strip()
                    if stripped.startswith(("'", '"')):
                        quoted.append(stripped)
                if len(quoted) < 2:
                    continue  # Unparseable; skip rather than false-positive.
                description = quoted[1].lower()
                if not any(kw in description for kw in hint_keywords):
                    rel = ts_file.relative_to(project_root)
                    violations.append(
                        f"  {rel}:{i} — server.tool() schema has compact/select but description lacks hint"
                    )

    if violations:
        print(f"MCP tools missing description hints ({len(violations)}):")
        for v in violations:
            print(v)
        print(
            "\nFIX: add a usage hint to the description string. Example: "
            "'List events. Pass select=\"id,start,summary\" + compact=true to cut payload.'"
        )
        print("See docs/decisions/printing-press-adoption.md and patterns/channel-add.md.")
        return 1  # blocking — fails CI on any new violation
    print("All projection-capable MCP tools include description hints.")
    return 0


def check_cache_map(project_root: Path) -> int:
    """Validate that .claude/codebase_map.md is fresh (stored SHA == HEAD SHA).

    Exit codes:
      0 — map exists and its SHA matches HEAD (FRESH)
      1 — map is missing or its SHA does not match HEAD (STALE)

    Designed for CI: ``python3 scripts/drift_check.py --cache-map``
    """
    map_path = project_root / ".claude" / "codebase_map.md"

    if not map_path.exists():
        print("STALE: .claude/codebase_map.md not found — run `python3 scripts/codebase_map.py`")
        return 1

    # Extract the SHA stored in the first line: <!-- sha: <40-hex> -->
    try:
        first_line = map_path.open(encoding="utf-8").readline()
    except OSError as exc:
        print(f"STALE: could not read {map_path}: {exc}")
        return 1

    m = re.search(r"<!--\s*sha:\s*([a-f0-9]{40})\s*-->", first_line)
    if not m:
        print("STALE: codebase_map.md has no valid SHA header")
        return 1
    stored_sha = m.group(1)

    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        head_sha = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        print("SKIP: git not available — cannot verify codebase_map freshness", file=sys.stderr)
        return 0

    if not head_sha:
        print("SKIP: git returned empty SHA", file=sys.stderr)
        return 0

    if stored_sha == head_sha:
        print(f"FRESH: codebase_map.md (sha {head_sha[:8]})")
        return 0

    print(
        f"STALE: codebase_map.md (stored {stored_sha[:8]}, HEAD {head_sha[:8]}) — "
        "run `python3 scripts/codebase_map.py`"
    )
    return 1


def check_codegraph_transcript_format(project_root: Path) -> int:
    """Validate that the codegraph-first gate scan predicate still works.

    Two layers:

    (CI) Fixture test — loads ``scripts/tests/fixtures/codegraph_transcript_sample.jsonl``
    and asserts the shared predicate (``_line_is_codegraph_toolcall`` from
    ``codex_warden_hooks.py``) correctly identifies positives and rejects
    negatives.  If CC changes its transcript format this fixture will no longer
    match reality, but the test will still pass — that's intentional.  The
    fixture test is a regression guard on the *scan code*, not on the live format.

    (Host) Live shape check — finds the most recent CC transcript under
    ``~/.claude/projects/`` and asserts the structural invariants the predicate
    depends on still hold in real data.  SKIPs gracefully when no transcripts
    are found (CI, fresh installs) exactly like ``check_cache_map`` skips when
    git is absent.

    (Host) Version pin — compares current ``claude --version`` against the
    version recorded in the fixture metadata.  A version change triggers a
    WARNING (not a failure) prompting re-capture.  This is a cheap "go
    re-validate" nudge; it does not block CI.

    Exit codes:
      0 — all checks passed (or host checks skipped)
      1 — fixture test failed (CI-blocking) OR live invariant check failed
      2 — fixture file missing
    """
    import importlib.util as ilu

    # ------------------------------------------------------------------ #
    # Load the shared predicate from codex_warden_hooks.py               #
    # ------------------------------------------------------------------ #
    hooks_path = project_root / "scripts" / "codex_warden_hooks.py"
    if not hooks_path.exists():
        print(f"ERROR: {hooks_path} not found")
        return 2

    spec = ilu.spec_from_file_location("_cwh", hooks_path)
    cwh = ilu.module_from_spec(spec)
    # Register in sys.modules BEFORE exec_module so dataclasses (Python 3.14+)
    # can resolve the module's __module__ reference during class decoration.
    import sys as _sys
    _sys.modules["_cwh"] = cwh
    try:
        spec.loader.exec_module(cwh)
    except Exception as exc:
        print(f"ERROR: failed to load codex_warden_hooks.py: {exc}")
        return 1

    predicate = getattr(cwh, "_line_is_codegraph_toolcall", None)
    if predicate is None:
        print(
            "FAIL: _line_is_codegraph_toolcall not found in codex_warden_hooks.py — "
            "the shared predicate was removed or renamed. "
            "The codegraph-first gate and the fixture test are now out of sync."
        )
        return 1

    # ------------------------------------------------------------------ #
    # (CI) Fixture test                                                   #
    # ------------------------------------------------------------------ #
    fixture_path = project_root / "scripts" / "tests" / "fixtures" / "codegraph_transcript_sample.jsonl"
    if not fixture_path.exists():
        print(f"FAIL: fixture not found at {fixture_path}")
        print(
            "  Re-capture from a real CC session: find a transcript in "
            "~/.claude/projects/.../*.jsonl that contains mcp__codegraph__ tool_use "
            "lines, extract representative positive and negative samples, scrub personal "
            "data, and write to the fixture path."
        )
        return 2

    import json as _json

    fixture_failures: list[str] = []
    for raw in fixture_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = _json.loads(raw)
        except _json.JSONDecodeError as exc:
            fixture_failures.append(f"parse error: {exc}")
            continue
        role = obj.pop("_fixture_role", None)
        if role is None:
            fixture_failures.append(f"missing _fixture_role: {raw[:80]}")
            continue
        result = predicate(obj)
        if role.startswith("pos_") and not result:
            fixture_failures.append(
                f"FAIL: positive sample '{role}' not detected by _line_is_codegraph_toolcall. "
                f"CC may have changed its transcript format or the predicate was broken. "
                f"Re-capture the fixture from a live transcript and update the predicate."
            )
        elif role.startswith("neg_") and result:
            fixture_failures.append(
                f"FAIL: negative sample '{role}' wrongly detected as codegraph call. "
                f"The predicate is producing false positives — review _line_is_codegraph_toolcall."
            )

    if fixture_failures:
        print("=== codegraph transcript fixture test: FAIL ===")
        for msg in fixture_failures:
            print(f"  {msg}")
        return 1
    print(f"OK: codegraph transcript fixture ({fixture_path.name}): all samples match predicate")

    # ------------------------------------------------------------------ #
    # (Host) Live shape check — SKIP when no transcripts found           #
    # ------------------------------------------------------------------ #
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        print("SKIP: ~/.claude/projects/ not found — skipping live shape check (CI/fresh install)")
        return 0

    # Find the most recently modified transcript file.
    all_transcripts = sorted(
        projects_dir.rglob("*.jsonl"),
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )
    live_path: Path | None = None
    for candidate in all_transcripts[:50]:  # cap scan cost
        if candidate.stat().st_size > 0:
            live_path = candidate
            break

    if live_path is None:
        print("SKIP: no non-empty transcript files found — skipping live shape check")
        return 0

    # Validate invariants against real data.
    live_failures: list[str] = []
    assistant_turns = 0
    tool_use_count = 0
    parse_errors = 0
    try:
        for raw in live_path.read_text(encoding="utf-8", errors="replace").splitlines()[:500]:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = _json.loads(raw)
            except _json.JSONDecodeError:
                parse_errors += 1
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "assistant":
                assistant_turns += 1
                msg = obj.get("message")
                if not isinstance(msg, dict):
                    live_failures.append(
                        f"assistant line has non-dict 'message' field (key: {list(obj.keys())[:5]})"
                    )
                    break
                content = msg.get("content")
                if content is not None and not isinstance(content, list):
                    live_failures.append(
                        f"message.content is {type(content).__name__}, expected list"
                    )
                    break
                if isinstance(content, list):
                    for blk in content:
                        if isinstance(blk, dict) and blk.get("type") == "tool_use":
                            tool_use_count += 1
                            if "name" not in blk:
                                live_failures.append(
                                    "tool_use block missing 'name' key: "
                                    f"{list(blk.keys())}"
                                )
    except OSError as exc:
        print(f"SKIP: could not read live transcript {live_path}: {exc}")
        return 0

    if live_failures:
        print(f"FAIL: live transcript shape check ({live_path.name}):")
        for msg in live_failures:
            print(f"  {msg}")
        print(
            "  The CC transcript format appears to have changed. "
            "Re-capture scripts/tests/fixtures/codegraph_transcript_sample.jsonl "
            "from a fresh session, update _line_is_codegraph_toolcall in "
            "codex_warden_hooks.py to match the new format, then re-run."
        )
        return 1

    print(
        f"OK: live transcript shape check ({live_path.name}): "
        f"{assistant_turns} assistant turns, {tool_use_count} tool_use blocks, "
        f"{parse_errors} parse errors"
    )

    # ------------------------------------------------------------------ #
    # (Host) Version pin — WARNING only, not a failure                   #
    # ------------------------------------------------------------------ #
    meta_path = fixture_path.parent / "codegraph_transcript_sample.meta.json"
    if meta_path.exists():
        try:
            meta = _json.loads(meta_path.read_text(encoding="utf-8"))
            pinned_version = meta.get("cc_version", "")
        except Exception:
            pinned_version = ""

        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            cc_line = result.stdout.strip().split("\n")[0]
            # e.g. "2.1.157 (Claude Code)"
            current_version = cc_line.split()[0] if cc_line else ""
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            current_version = ""

        if pinned_version and current_version and current_version != pinned_version:
            print(
                f"WARN: CC version changed {pinned_version} → {current_version}. "
                "Re-validate the codegraph fixture against a fresh transcript: "
                "python3 scripts/drift_check.py --codegraph-format. "
                "If live shape check passed, the format is still compatible. "
                "Update meta.json cc_version to silence this warning."
            )
        elif current_version:
            print(f"OK: CC version {current_version} matches pinned {pinned_version or '(none)'}")

    # ------------------------------------------------------------------ #
    # (Host) Subagent-path derivation invariant.                          #
    # The gate resolves a Task-spawned subagent's transcript as           #
    # <session_id>/subagents/agent-<id>.jsonl from the parent session     #
    # path + agent_id (_resolve_agent_transcript). If CC changes that     #
    # layout, the gate scans the wrong file and silently fails open.      #
    # Verify the formula still round-trips against a real subagent file.  #
    # ------------------------------------------------------------------ #
    sub_files = list(projects_dir.rglob("subagents/agent-*.jsonl"))
    if sub_files:
        sample = max(sub_files, key=lambda p: p.stat().st_mtime)
        session_dir = sample.parent.parent  # .../<session_id>
        parent_file = session_dir.with_suffix(".jsonl")
        agent_id = sample.stem[len("agent-"):]
        # Mirror _resolve_agent_transcript: Path(parent).with_suffix("")/subagents/agent-<id>.jsonl
        derived = parent_file.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
        if derived != sample:
            print(
                f"FAIL: subagent path derivation no longer matches CC layout "
                f"(sample {sample}, derived {derived}). Update "
                "_resolve_agent_transcript in codex_warden_hooks.py."
            )
            return 1
        print(
            f"OK: subagent path-derivation layout intact ({sample.name}); "
            f"parent session file {'present' if parent_file.exists() else 'absent'}"
        )
    else:
        print("SKIP: no subagent transcripts found — cannot verify path derivation")

    # Workflow-subagent path-derivation invariant (SEPARATE from the flat check
    # above). Workflow agents write at <session>/subagents/workflows/wf_*/agent-*.jsonl;
    # round-trip the real resolver against a real workflow file so a CC layout change
    # FAILs here instead of silently failing the gate open. SKIP (never FAIL) when no
    # workflow transcripts exist (cold environment).
    resolve = getattr(cwh, "_resolve_agent_transcript", None)
    if resolve is None:
        print(
            "FAIL: _resolve_agent_transcript not found in codex_warden_hooks.py — "
            "the workflow-path fallback was removed or renamed."
        )
        return 1
    wf_files = list(projects_dir.rglob("subagents/workflows/*/agent-*.jsonl"))
    if wf_files:
        wf_sample = max(wf_files, key=lambda p: p.stat().st_mtime)
        # Reconstruct <proj>/<session_id>.jsonl from the session-boundary 'subagents'.
        # Use the LAST occurrence so a project dir literally named 'subagents' can't
        # shift the boundary; it is guaranteed present (we globbed on it).
        parts = wf_sample.parts
        sidx = max(i for i, p in enumerate(parts) if p == "subagents")
        session_dir = Path(*parts[:sidx])  # <proj>/<session_id>
        parent_file = session_dir.parent / (session_dir.name + ".jsonl")
        wf_agent_id = wf_sample.stem[len("agent-"):]
        derived = resolve({"transcript_path": str(parent_file), "agent_id": wf_agent_id})
        if Path(derived) != wf_sample:
            print(
                f"FAIL: workflow subagent path derivation no longer resolves the real "
                f"file (sample {wf_sample}, derived {derived}). Update the glob-fallback "
                "in _resolve_agent_transcript in codex_warden_hooks.py."
            )
            return 1
        print(f"OK: workflow subagent path-derivation intact ({wf_sample.name})")
    else:
        print(
            "SKIP: no workflow subagent transcripts found — "
            "cannot verify workflow path derivation"
        )

    return 0


def check_all(project_root: Path, base_ref: str | None = None) -> int:
    """Run every fast check in sequence and aggregate exit codes.

    Runs: drift (main), paths, adr, test_tasks, coverage. The worst exit code
    wins. Coverage is informational (always returns 0) so it contributes only
    its report, never a failure.
    """
    print("=== drift (mtime) ===")
    drift_rc = main(base_ref=base_ref)
    print("\n=== paths ===")
    paths_rc = check_paths(project_root)
    print("\n=== index completeness ===")
    idx_rc = check_index_completeness(project_root)
    print("\n=== adr freshness ===")
    adr_rc = check_adr(project_root)
    print("\n=== test_tasks frontmatter ===")
    tt_rc = check_test_tasks(project_root)
    print("\n=== shadow (private overrides) ===")
    shadow_rc = check_shadow(project_root)
    print("\n=== bootstrap mirror ===")
    bm_rc = check_bootstrap_mirror(project_root)
    print("\n=== bench labels ===")
    bench_rc = check_bench_labels(project_root)
    print("\n=== backend strategy ===")
    bs_rc = check_backend_strategy(project_root)
    print("\n=== platform parity ===")
    pp_rc = check_platform_parity(project_root)
    print("\n=== agent-native MCP ===")
    anp_rc = check_agent_native_mcp(project_root)
    print("\n=== MCP description hints ===")
    mhint_rc = check_mcp_description_hints(project_root)
    print("\n=== codegraph transcript format ===")
    cg_rc = check_codegraph_transcript_format(project_root)
    print("\n=== coverage (informational) ===")
    cov_rc = check_coverage(project_root)

    worst = max(drift_rc, paths_rc, idx_rc, adr_rc, tt_rc, shadow_rc, bm_rc, bench_rc, bs_rc, pp_rc, anp_rc, mhint_rc, cg_rc, cov_rc)
    print()
    if worst == 0:
        print("ALL CHECKS PASSED")
    else:
        print(f"FAILED (worst exit code: {worst})")
    return worst


# Two copies of the bootstrap harness exist by design — `src/bootstrap.ts`
# logs through pino+FatalError, `container/agent-runner/src/bootstrap.ts`
# logs through console.error (no pino dep). Issue #218 considered extracting
# them into a shared package and rejected it: the only divergence is the
# logger, which would require a third LogAdapter abstraction that's a bigger
# design call than the duplication is worth. Instead we mechanically enforce
# structural mirror with this check.
_BOOTSTRAP_MIRROR_PAIR = (
    "src/bootstrap.ts",
    "container/agent-runner/src/bootstrap.ts",
)


def _strip_for_mirror(text: str) -> str:
    """Normalize a bootstrap.ts file for structural diff against its mirror.

    Strips:
      1. Lines between `// MIRROR-IGNORE-START` and `// MIRROR-IGNORE-END`
         (inclusive). These mark the deliberately-divergent sections —
         logger calls, helper functions for logging, and supporting imports.
      2. JSDoc blocks (`/** ... */`).
      3. Single-line `// ...` comments (NOT URLs in code, since they'd be
         in string literals — bootstrap.ts has no URL strings).
      4. Blank lines.
      5. Leading/trailing whitespace on each remaining line.

    The result is a structure-only view: function signatures, control flow,
    interface definitions, and the harness skeleton. Two mirror copies must
    produce byte-identical normalized text.
    """
    lines = text.splitlines()
    stripped: list[str] = []

    in_jsdoc = False
    in_mirror_ignore = False
    for line in lines:
        s = line.strip()

        # JSDoc state first — text inside a JSDoc block can mention the
        # MIRROR-IGNORE keywords (e.g., the file header documenting the
        # marker convention), and those mentions must not be mistaken for
        # actual markers.
        if in_jsdoc:
            if "*/" in s:
                in_jsdoc = False
            continue
        if s.startswith("/**"):
            # Handle single-line JSDoc like `/** foo */`.
            if s.endswith("*/") and len(s) > 3:
                continue
            in_jsdoc = True
            continue

        # Mirror-ignore markers (only outside JSDoc).
        if not in_mirror_ignore and (
            s == "// MIRROR-IGNORE-START"
            or s.startswith("// MIRROR-IGNORE-START ")
        ):
            in_mirror_ignore = True
            continue
        if in_mirror_ignore:
            if s == "// MIRROR-IGNORE-END" or s.startswith("// MIRROR-IGNORE-END "):
                in_mirror_ignore = False
            continue

        # Single-line comment lines
        if s.startswith("//"):
            continue
        if not s:
            continue

        stripped.append(s)

    return "\n".join(stripped)


def check_bootstrap_mirror(project_root: Path) -> int:
    """Verify `src/bootstrap.ts` and `container/agent-runner/src/bootstrap.ts`
    stay structurally aligned. See issue #218.

    Both files must be structurally identical after stripping JSDoc, single-
    line comments, blank lines, and the deliberately divergent regions
    bracketed by `// MIRROR-IGNORE-START` / `// MIRROR-IGNORE-END`.

    Returns 0 on match, 1 on diff or missing file.
    """
    paths = [project_root / p for p in _BOOTSTRAP_MIRROR_PAIR]
    for p in paths:
        if not p.exists():
            print(f"MISSING: {p.relative_to(project_root)}")
            return 1

    normalized = [_strip_for_mirror(p.read_text()) for p in paths]
    if normalized[0] == normalized[1]:
        print(
            f"Both bootstrap copies aligned "
            f"({len(normalized[0].splitlines())} structural lines each)."
        )
        return 0

    # Show a unified diff so the failure is actionable.
    import difflib
    diff = list(
        difflib.unified_diff(
            normalized[0].splitlines(),
            normalized[1].splitlines(),
            fromfile=_BOOTSTRAP_MIRROR_PAIR[0] + " (normalized)",
            tofile=_BOOTSTRAP_MIRROR_PAIR[1] + " (normalized)",
            lineterm="",
        )
    )
    print("BOOTSTRAP MIRROR DRIFT — structural shape diverges between:")
    print(f"  {_BOOTSTRAP_MIRROR_PAIR[0]}")
    print(f"  {_BOOTSTRAP_MIRROR_PAIR[1]}")
    print()
    for line in diff:
        print(line)
    print()
    print(
        "FIX: apply the same change to both files. The two harnesses must "
        "stay behaviorally aligned — see issue #218 for why they live in "
        "two places. Wrap deliberately-divergent regions (logger calls and "
        "their helpers) in `// MIRROR-IGNORE-START` / `// MIRROR-IGNORE-END`."
    )
    return 1


def check_shadow(project_root: Path) -> int:
    """Check that src/private/ mirrors are properly symlinked.

    Scans src/private/ for files that shadow public equivalents (e.g.,
    src/private/scripts/foo.py shadows scripts/foo.py). Warns if:
    - A shadow exists but /tmp/ symlink points to the public version
    - A shadow exists but no /tmp/ symlink exists at all
    Returns 0 if all shadows are properly linked, 1 if issues found.
    """
    private_root = project_root / "src" / "private"
    if not private_root.exists():
        print("No src/private/ directory — nothing to check.")
        return 0

    issues = 0
    for private_file in sorted(private_root.rglob("*")):
        if private_file.is_dir() or private_file.name.startswith("."):
            continue
        # Compute the public equivalent path
        # src/private/scripts/foo.py → scripts/foo.py
        rel = private_file.relative_to(private_root)
        public_file = project_root / rel
        if not public_file.exists():
            continue  # No shadow — private-only file, fine

        # Check /tmp/ symlink
        tmp_link = Path("/tmp") / private_file.name
        if tmp_link.is_symlink():
            target = tmp_link.resolve()
            if target != private_file.resolve():
                print(f"  WARN: /tmp/{private_file.name} → {target}")
                print(f"        should point to {private_file}")
                issues += 1
            else:
                print(f"  OK: /tmp/{private_file.name} → private (correct)")
        elif tmp_link.exists():
            print(f"  WARN: /tmp/{private_file.name} exists but is not a symlink")
            issues += 1
        else:
            print(f"  WARN: {rel} shadows public version but no /tmp/ symlink")
            print(f"        run: ln -sf {private_file} /tmp/{private_file.name}")
            issues += 1

    if issues == 0:
        print("All private shadows properly linked.")
    else:
        print(f"\n{issues} shadow issue(s) found.")
    return 1 if issues else 0


# Index files whose completeness we enforce: (index_path, leaves_glob).
# For each glob, every match (minus INDEX.md itself) must be referenced in
# the index; every reference in the index must resolve to a real file.
# Keep this list in sync with new index files as the repo grows.
_INDEX_COVERAGE: list[tuple[str, str]] = [
    ("patterns/INDEX.md",       "patterns/*.md"),
    ("docs/decisions/INDEX.md", "docs/decisions/*.md"),
]


def _extract_index_refs(index_path: Path, leaf_dir: Path) -> set[str]:
    """Extract the set of leaf filenames referenced by an index file.

    Matches markdown links `[text](path/file.md)` and backtick-quoted
    `path/file.md` tokens where the path is under `leaf_dir`. Returns the
    basenames only so we can compare against a directory listing.
    """
    try:
        text = index_path.read_text()
    except FileNotFoundError:
        return set()
    # Leaf filenames live at the end of either [](...) or `...` tokens.
    # Examples: `[general](patterns/general-code.md)`, `` `0042-name.md` ``.
    # We match both with or without a directory prefix, then grab the basename.
    refs: set[str] = set()
    for m in re.finditer(r"(?:\(|`)([^`)\s]+?\.md)(?:\)|`)", text):
        token = m.group(1).strip()
        if not token:
            continue
        # Only keep references that live under leaf_dir (or its basename),
        # so we don't cross-count e.g. patterns/INDEX.md referring to
        # `docs/foo.md`.
        leaf_prefix = leaf_dir.name + "/"
        if token.startswith(leaf_prefix) or "/" not in token:
            refs.add(Path(token).name)
    return refs


def check_index_completeness(project_root: Path) -> int:
    """Detect drift between an index file and the leaves it indexes.

    For each (index, leaves_glob) pair in _INDEX_COVERAGE:
      - Orphans: leaf files on disk not referenced by the index
      - Dangling: references in the index with no matching file

    Protects against the common failure mode where a contributor adds a new
    pattern/decision doc but forgets to wire it into the index, leaving the
    agent unable to route to it.
    """
    issues: list[str] = []
    for index_rel, leaves_glob in _INDEX_COVERAGE:
        index_path = project_root / index_rel
        leaf_dir = (project_root / leaves_glob).parent
        # Skip entirely if the indexed directory doesn't exist in this project.
        # Repos that don't use one of the indexed dirs (e.g. no docs/decisions/)
        # shouldn't fail this check.
        if not leaf_dir.exists():
            continue
        if not index_path.exists():
            issues.append(f"  {index_rel}: index file is missing")
            continue

        # Listing via glob keeps it deterministic and ignores non-.md files.
        on_disk: set[str] = {
            p.name for p in project_root.glob(leaves_glob)
            if p.is_file() and p.name != "INDEX.md"
        }
        referenced = _extract_index_refs(index_path, leaf_dir)
        # INDEX.md may reference itself ("this file"); exclude it from the diff.
        referenced = {r for r in referenced if r != "INDEX.md"}

        orphans = sorted(on_disk - referenced)
        dangling = sorted(referenced - on_disk)

        if orphans:
            for name in orphans:
                issues.append(f"  {index_rel}: orphan (leaf exists, not in index): {leaf_dir.name}/{name}")
        if dangling:
            for name in dangling:
                issues.append(f"  {index_rel}: dangling (in index, leaf missing): {leaf_dir.name}/{name}")

    if not issues:
        total = sum(
            len([p for p in project_root.glob(g) if p.is_file() and p.name != "INDEX.md"])
            for _, g in _INDEX_COVERAGE
        )
        print(f"All indexes in sync with leaves ({total} leaves across {len(_INDEX_COVERAGE)} indexes).")
        return 0

    print(f"Index drift ({len(issues)} issue(s)):")
    for i in issues:
        print(i)
    print("\nFIX: add the orphaned leaf to its index, or delete the dangling reference.")
    return 1


def check_backend_strategy(project_root: Path) -> int:
    """Enforce the Backend strategy trait convention (ADR: backend-strategy-trait.md).

    Scans app-level TUI code (everything outside tui/src/backend/) for patterns
    that indicate provider-specific logic leaked out of the trait:
    - Direct CLI binary references: Command::new("claude"), Command::new("codex")
    - Provider-specific JSONL field parsing: "item.completed", "turn.completed"
    - Hardcoded model registries that duplicate what backends declare

    The backend/ directory itself is exempt — that's where these belong.
    """
    tui_src = project_root / "tui" / "src"
    if not tui_src.exists():
        print("tui/src/ not found (skipped)")
        return 0

    backend_dir = tui_src / "backend"
    leak_patterns = [
        ('Command::new("claude")', "CLI invocation belongs in backend/claude.rs"),
        ('Command::new("codex")', "CLI invocation belongs in backend/codex.rs"),
        ('Command::new("ollama")', "CLI invocation belongs in backend/ollama.rs"),
        ('"item.completed"', "Codex JSONL parsing belongs in backend/codex.rs"),
        ('"turn.completed"', "Codex JSONL parsing belongs in backend/codex.rs"),
        ('"turn.failed"', "Codex JSONL parsing belongs in backend/codex.rs"),
        ('"stream-json"', "Claude output format belongs in backend/claude.rs"),
    ]

    issues: list[str] = []
    for rs_file in tui_src.rglob("*.rs"):
        if backend_dir in rs_file.parents or rs_file.parent == backend_dir:
            continue
        content = rs_file.read_text()
        rel = rs_file.relative_to(project_root)
        for pattern, reason in leak_patterns:
            if pattern in content:
                issues.append(f"  {rel}: contains {pattern} — {reason}")

    if not issues:
        print("Backend strategy: no provider-specific leaks in app-level TUI code.")
        return 0

    print(f"Backend strategy VIOLATION — provider logic outside backend/ ({len(issues)} issue(s)):")
    for issue in issues:
        print(issue)
    print("\nFIX: move provider-specific logic into the Backend trait implementation.")
    print("ADR: docs/decisions/backend-strategy-trait.md")
    return 1


def check_platform_parity(project_root: Path) -> int:
    """Verify src/platform.ts and tui/src/platform.rs expose aligned capabilities.

    Extracts exported function/const names from each file and checks that
    the Rust module covers every capability category in the TypeScript module.
    Naming conventions differ (camelCase vs snake_case) so we compare normalized
    category stems rather than exact names.
    """
    import re

    ts_file = project_root / "src" / "platform.ts"
    rs_file = project_root / "tui" / "src" / "platform.rs"

    if not ts_file.exists():
        print(f"  {ts_file.relative_to(project_root)} not found (skipped)")
        return 0
    if not rs_file.exists():
        print(f"  {rs_file.relative_to(project_root)} not found (skipped)")
        return 0

    def ts_exports(content: str) -> set[str]:
        names: set[str] = set()
        for m in re.finditer(r'export\s+(?:const|function)\s+(\w+)', content):
            names.add(m.group(1))
        return names

    def rs_exports(content: str) -> set[str]:
        names: set[str] = set()
        for m in re.finditer(r'pub\s+(?:fn|const)\s+(\w+)', content):
            names.add(m.group(1))
        return names

    def normalize(name: str) -> str:
        """camelCase/PascalCase/SCREAMING_SNAKE → lowercase words."""
        s = re.sub(r'([a-z])([A-Z])', r'\1_\2', name)
        return s.lower().replace('_', '')

    # Category buckets that must be present in both
    REQUIRED_CATEGORIES = {
        "homedir": "Home directory accessor",
        "ismacos": "macOS platform detection",
        "iswindows": "Windows platform detection",
        "islinux": "Linux platform detection",
    }

    ts_content = ts_file.read_text()
    rs_content = rs_file.read_text()

    ts_names = {normalize(n) for n in ts_exports(ts_content)}
    rs_names = {normalize(n) for n in rs_exports(rs_content)}

    missing_in_rust: list[str] = []
    for cat, desc in REQUIRED_CATEGORIES.items():
        if cat in ts_names and cat not in rs_names:
            missing_in_rust.append(f"  Missing in Rust: {cat} ({desc})")

    if missing_in_rust:
        print(f"Platform parity VIOLATION — {len(missing_in_rust)} gap(s):")
        for m in missing_in_rust:
            print(m)
        print(f"\nFIX: add missing capabilities to tui/src/platform.rs")
        return 1

    print(f"Platform parity: TS has {len(ts_exports(ts_content))} exports, "
          f"RS has {len(rs_exports(rs_content))} exports — required categories aligned.")
    return 0


def check_coverage(project_root: Path) -> int:
    """Report docs/ files that are not referenced by any pattern (informational)."""
    patterns_dir = project_root / "patterns"
    docs_dir = project_root / "docs"

    if not docs_dir.exists():
        print("No docs/ directory found.")
        return 0

    covered: set[str] = set()

    # 1. Honor governs: frontmatter — a directory entry covers all .md beneath it
    for pattern_file in patterns_dir.glob("*.md"):
        if pattern_file.name == "INDEX.md":
            continue
        for g in parse_governs(pattern_file):
            governed_path = project_root / g
            if governed_path.is_dir():
                for md in governed_path.rglob("*.md"):
                    covered.add(str(md.relative_to(project_root)))
            elif governed_path.exists() and g.endswith(".md"):
                covered.add(g)

    # 2. Explicit docs/ path mentions in pattern text (existing behavior)
    index = patterns_dir / "INDEX.md"
    sources = [index] + list(patterns_dir.glob("*.md")) if index.exists() else list(patterns_dir.glob("*.md"))
    for src in sources:
        try:
            text = src.read_text()
        except FileNotFoundError:
            continue
        for match in re.finditer(r"docs/[\w./-]+\.md", text):
            covered.add(match.group(0))

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


# ── Benchmark label validation ────────────────────────────────────────────────

_BENCH_FIXTURE = "scripts/tests/fixtures/memory_tree_queries.jsonl"
_BENCH_SNAPSHOT = "scripts/tests/fixtures/memory_tree_snapshot.json"

_VAULT_SKIP_DIRS = frozenset(
    {"Session-Logs", "Checkpoints", "Atoms", "ARCHIVE", ".git", ".obsidian"}
)

_AUTO_MEMORY_GLOBS = (
    Path("~/.claude/projects").expanduser(),
)


def _collect_vault_paths(vault: Path) -> set[str]:
    """Collect all .md relative paths from the vault that could be tree nodes."""
    paths: set[str] = set()
    if not vault.is_dir():
        return paths
    for p in vault.rglob("*.md"):
        rel = p.relative_to(vault)
        if any(part in _VAULT_SKIP_DIRS for part in rel.parts):
            continue
        paths.add(str(rel))
    return paths


def _collect_auto_memory_paths() -> set[str]:
    """Collect auto-memory/ namespace paths from all Claude project memory dirs."""
    paths: set[str] = set()
    for base in _AUTO_MEMORY_GLOBS:
        if not base.is_dir():
            continue
        for memory_dir in base.rglob("memory"):
            if not memory_dir.is_dir():
                continue
            for p in memory_dir.glob("*.md"):
                if p.name == "MEMORY.md":
                    continue
                paths.add(f"auto-memory/{p.name}")
    return paths


def check_bench_labels(project_root: Path) -> int:
    """Validate that benchmark expected paths exist as vault or auto-memory files.

    Catches stale benchmark labels after vault restructures — the specific
    failure mode that masked the Apr 2026 recall regression for 7 days.
    """
    import json

    bench_path = project_root / _BENCH_FIXTURE
    if not bench_path.exists():
        print(f"Benchmark fixture not found: {_BENCH_FIXTURE} (skipped)")
        return 0

    # Resolve vault path: env var → config.json → legacy fallback
    vault_env = os.environ.get("DEUS_VAULT_PATH")
    if vault_env:
        vault = Path(vault_env).expanduser()
    else:
        cfg_path = Path("~/.config/deus/config.json").expanduser()
        vault = None
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                vp = cfg.get("vault_path")
                if isinstance(vp, str) and vp:
                    vault = Path(vp).expanduser()
            except (OSError, json.JSONDecodeError):
                pass
        if vault is None:
            vault = Path("~/Desktop/אישי/Brain Dump/Second Brain/Deus").expanduser()

    vault_paths = _collect_vault_paths(vault) if vault.is_dir() else set()
    auto_paths = _collect_auto_memory_paths()
    all_known = vault_paths | auto_paths

    if not all_known:
        print("No vault or auto-memory paths found (vault not mounted?). Skipped.")
        return 0

    if vault.is_dir() and not vault_paths:
        print("Vault directory exists but could not enumerate files (sandbox permission). Skipped.")
        return 0

    data = [
        json.loads(line)
        for line in bench_path.read_text().splitlines()
        if line.strip()
    ]

    issues: list[str] = []
    for i, item in enumerate(data, 1):
        paths = item.get("expected_paths") or []
        if isinstance(item.get("expected_path"), str):
            paths = [item["expected_path"]] + [p for p in paths if p != item["expected_path"]]
        for p in paths:
            if p and p not in all_known:
                issues.append(f"  line {i}: expected_path '{p}' not found in vault or auto-memory")

    if not issues:
        print(f"Benchmark labels OK: {len(data)} queries, all expected paths exist in vault.")
        return 0

    print(f"Stale benchmark labels ({len(issues)} issue(s)):")
    for issue in issues:
        print(issue)
    print("\nFIX: update expected_path in", _BENCH_FIXTURE, "to match current vault structure.")
    return 1


def check_bench_snapshot(project_root: Path) -> int:
    """Compare current benchmark results against a stored snapshot.

    The snapshot records last-known-good recall and threshold. If the snapshot
    exists and current results are below threshold, this fails. If no snapshot
    exists, this is informational only.

    Requires: EMBEDDING_PROVIDER env, Ollama running, ~/.deus/memory_tree.db.
    Skips gracefully if any dependency is missing.
    """
    import json

    snapshot_path = project_root / _BENCH_SNAPSHOT
    bench_path = project_root / _BENCH_FIXTURE
    if not snapshot_path.exists():
        print(f"No benchmark snapshot at {_BENCH_SNAPSHOT}. Run `npm run bench:snapshot` to create one.")
        return 0
    if not bench_path.exists():
        return 0

    snapshot = json.loads(snapshot_path.read_text())
    min_recall = snapshot.get("min_retrieval_recall", 0.85)

    # Try to import and run the benchmark
    try:
        sys.path.insert(0, str(project_root / "scripts"))
        from memory_tree import open_db, retrieve
    except Exception:
        print("memory_tree import failed (sqlite-vec not available?). Skipped.")
        return 0

    db_path = Path("~/.deus/memory_tree.db").expanduser()
    if not db_path.exists():
        print("No memory_tree.db found. Skipped.")
        return 0

    try:
        db = open_db()
    except Exception:
        print("Could not open memory_tree.db. Skipped.")
        return 0

    data = [
        json.loads(line)
        for line in bench_path.read_text().splitlines()
        if line.strip()
    ]

    hits = 0
    total = 0
    for item in data:
        if item.get("abstain"):
            continue
        total += 1
        try:
            r = retrieve(db, item["query"], k=5)
        except Exception:
            continue
        expected = item.get("expected_paths") or []
        if isinstance(item.get("expected_path"), str):
            expected = [item["expected_path"]] + [p for p in expected if p != item["expected_path"]]
        found = [x["path"] for x in r.get("results", [])]
        if any(e in found for e in expected):
            hits += 1

    if total == 0:
        print("No retrieval queries in benchmark. Skipped.")
        return 0

    recall = hits / total
    passed = recall >= min_recall
    status = "PASS" if passed else "REGRESSION"
    print(f"Benchmark {status}: retrieval recall = {recall:.1%} ({hits}/{total}), threshold = {min_recall:.1%}")

    if not passed:
        print(f"\nRecall dropped below {min_recall:.1%}. Investigate before merging.")
        print(f"Snapshot from: {snapshot.get('created', 'unknown')}")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Drift checker for pattern files.")
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Report docs/ files not referenced by any pattern (informational)",
    )
    parser.add_argument(
        "--paths",
        action="store_true",
        help="Verify every path referenced by a pattern (governs + body) exists",
    )
    parser.add_argument(
        "--adr",
        action="store_true",
        help="Flag patterns whose last_verified predates an overlapping ADR",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Run every fast check (drift + paths + adr + test_tasks + coverage)",
    )
    parser.add_argument(
        "--validate",
        nargs="?",
        const="",
        metavar="PATTERN",
        help="LLM-based correctness check (slow, needs GEMINI_API_KEY). "
             "Optional PATTERN arg filters to matching pattern files.",
    )
    parser.add_argument(
        "--validate-router",
        nargs="?",
        const="",
        metavar="PATTERN",
        dest="validate_router",
        help="LLM-based router check: verify ROUTER.md picks the correct "
             "pattern for each test_task (slow, needs GEMINI_API_KEY).",
    )
    parser.add_argument(
        "--contradictions",
        nargs="?",
        const="",
        metavar="PATTERN",
        help="LLM-based cross-pattern contradictions check (slow, needs "
             "GEMINI_API_KEY). Optional PATTERN arg filters to matching files.",
    )
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="Check src/private/ files shadow public equivalents with correct /tmp/ symlinks",
    )
    parser.add_argument(
        "--bootstrap-mirror",
        action="store_true",
        dest="bootstrap_mirror",
        help="Verify src/bootstrap.ts and container/agent-runner/src/bootstrap.ts stay structurally aligned (issue #218)",
    )
    parser.add_argument(
        "--indexes",
        action="store_true",
        help="Check every indexed directory: index file references match on-disk leaves (orphans + dangling)",
    )
    parser.add_argument(
        "--bench-labels",
        action="store_true",
        dest="bench_labels",
        help="Validate memory_tree benchmark expected paths exist in vault",
    )
    parser.add_argument(
        "--bench-snapshot",
        action="store_true",
        dest="bench_snapshot",
        help="Run memory_tree benchmark and compare against stored snapshot (needs Ollama)",
    )
    parser.add_argument(
        "--backend-strategy",
        action="store_true",
        dest="backend_strategy",
        help="Check that provider-specific logic stays inside tui/src/backend/ (ADR: backend-strategy-trait.md)",
    )
    parser.add_argument(
        "--platform-parity",
        action="store_true",
        dest="platform_parity",
        help="Verify src/platform.ts and tui/src/platform.rs expose aligned capabilities",
    )
    parser.add_argument(
        "--base",
        metavar="REF",
        help="Only check governed files changed since REF (e.g. origin/main). "
             "Prevents cascading drift failures across sequential PRs.",
    )
    parser.add_argument(
        "--bump",
        action="store_true",
        help="Touch drifted pattern files to reset their mtime. "
             "Stage the touched files alongside your source changes.",
    )
    parser.add_argument(
        "--cache-map",
        action="store_true",
        dest="cache_map",
        help="Validate that .claude/codebase_map.md is fresh (SHA matches HEAD). "
             "Exits 0 if fresh, 1 if stale or missing. For CI gating.",
    )
    parser.add_argument(
        "--codegraph-format",
        action="store_true",
        dest="codegraph_format",
        help="Validate codegraph-first gate scan predicate against fixture + live transcript shape. "
             "CI: fixture test; host: live shape check + CC version pin. "
             "Exits 0 if OK/skipped, 1 if predicate broken, 2 if fixture missing.",
    )
    args = parser.parse_args()

    if args.codegraph_format:
        sys.exit(check_codegraph_transcript_format(PROJECT_ROOT))
    elif args.cache_map:
        sys.exit(check_cache_map(PROJECT_ROOT))
    elif args.platform_parity:
        sys.exit(check_platform_parity(PROJECT_ROOT))
    elif args.backend_strategy:
        sys.exit(check_backend_strategy(PROJECT_ROOT))
    elif args.bench_labels:
        sys.exit(check_bench_labels(PROJECT_ROOT))
    elif args.bench_snapshot:
        sys.exit(check_bench_snapshot(PROJECT_ROOT))
    elif args.shadow:
        sys.exit(check_shadow(PROJECT_ROOT))
    elif args.bootstrap_mirror:
        sys.exit(check_bootstrap_mirror(PROJECT_ROOT))
    elif args.indexes:
        sys.exit(check_index_completeness(PROJECT_ROOT))
    elif args.contradictions is not None:
        sys.exit(check_contradictions(PROJECT_ROOT, args.contradictions or None))
    elif args.validate_router is not None:
        sys.exit(check_validate_router(PROJECT_ROOT, args.validate_router or None))
    elif args.validate is not None:
        sys.exit(check_validate(PROJECT_ROOT, args.validate or None))
    elif args.all:
        sys.exit(check_all(PROJECT_ROOT, base_ref=args.base))
    elif args.coverage:
        sys.exit(check_coverage(PROJECT_ROOT))
    elif args.paths:
        sys.exit(check_paths(PROJECT_ROOT))
    elif args.adr:
        sys.exit(check_adr(PROJECT_ROOT))
    elif args.bump:
        sys.exit(main(bump=True, base_ref=args.base))
    else:
        sys.exit(main(base_ref=args.base))
