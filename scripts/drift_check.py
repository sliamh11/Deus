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
  python3 scripts/drift_check.py --coverage        # report uncovered docs/
  python3 scripts/drift_check.py --paths           # verify all pattern path refs exist
  python3 scripts/drift_check.py --adr             # flag patterns stale vs ADRs
  python3 scripts/drift_check.py --all             # run every fast check above
  python3 scripts/drift_check.py --validate        # LLM pattern content check (slow)
  python3 scripts/drift_check.py --validate-router # LLM router selection check (slow)
  python3 scripts/drift_check.py --contradictions  # LLM cross-pattern contradictions (slow)
  npm run drift-check
"""
import argparse
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


def main(base_ref: str | None = None) -> int:
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
        print("\nDRIFTED: update the flagged pattern files to match source changes.")
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
    print("\n=== adr freshness ===")
    adr_rc = check_adr(project_root)
    print("\n=== test_tasks frontmatter ===")
    tt_rc = check_test_tasks(project_root)
    print("\n=== coverage (informational) ===")
    cov_rc = check_coverage(project_root)

    worst = max(drift_rc, paths_rc, adr_rc, tt_rc, cov_rc)
    print()
    if worst == 0:
        print("ALL CHECKS PASSED")
    else:
        print(f"FAILED (worst exit code: {worst})")
    return worst


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
        "--base",
        metavar="REF",
        help="Only check governed files changed since REF (e.g. origin/main). "
             "Prevents cascading drift failures across sequential PRs.",
    )
    args = parser.parse_args()

    if args.contradictions is not None:
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
    else:
        sys.exit(main(base_ref=args.base))
