#!/usr/bin/env python3
"""
Code Review Benchmark — Dynamic Bug Injection Framework

Injects realistic, well-disguised bugs into real source files to measure
the code review pipeline's detection rate. Each run produces a different
set of bugs by randomly selecting patterns and injection points.

Usage:
    python3 scripts/review_benchmark.py inject [--count N] [--seed S]
    python3 scripts/review_benchmark.py diff
    python3 scripts/review_benchmark.py score <findings.json>
    python3 scripts/review_benchmark.py revert
    python3 scripts/review_benchmark.py run [--count N] [--seed S]

The `run` command does inject → diff → revert in one step, outputting
the diff and ground truth manifest for external review.
"""
import argparse
import json
import os
import random
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).parent.parent
MANIFEST_PATH = REPO_ROOT / ".review-benchmark-manifest.json"

# Files suitable for injection — mix of TS and Python, core and evolution
TARGET_FILES = [
    "src/container-runner.ts",
    "src/message-orchestrator.ts",
    "src/ipc.ts",
    "src/task-scheduler.ts",
    "src/credential-proxy.ts",
    "src/group-folder.ts",
    "src/remote-control.ts",
    "src/db.ts",
    "src/router.ts",
    "evolution/cli.py",
    "evolution/reflexion/store.py",
    "evolution/reflexion/generator.py",
    "evolution/ilog/interaction_log.py",
    "evolution/storage/providers/sqlite.py",
]


@dataclass
class InjectionResult:
    file: str
    line: int
    category: str  # security | logic | style
    severity: str  # CRITICAL | HIGH | MEDIUM | LOW
    bug_type: str
    description: str
    disguise: str  # how it's camouflaged


@dataclass
class BugPattern:
    name: str
    category: str
    severity: str
    lang: str  # ts | py | both

    def find_targets(self, content: str, filepath: str) -> list[tuple[int, str]]:
        """Return list of (line_number, matched_text) injection points."""
        raise NotImplementedError

    def inject(self, content: str, line_no: int, match: str, context: dict) -> tuple[str, str, str]:
        """Apply the bug. Returns (modified_content, description, disguise_note)."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# SECURITY PATTERNS
# ---------------------------------------------------------------------------

class PathTraversalBypass(BugPattern):
    """Remove path sanitization and add a misleading 'validated' comment."""

    def __init__(self):
        super().__init__("path_traversal_bypass", "security", "HIGH", "ts")

    def find_targets(self, content, filepath):
        targets = []
        for i, line in enumerate(content.split("\n"), 1):
            # Look for path validation patterns
            if re.search(r'(\.includes\(\s*["\']\.\.)', line) or \
               re.search(r'(path\.resolve|path\.normalize|path\.relative)', line):
                targets.append((i, line.strip()))
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        original = lines[idx]

        if ".includes('..')" in original or '.includes("..")' in original:
            # Comment out the traversal check, add misleading comment
            lines[idx] = f"  // path validated by caller — see resolveGroupPath()"
            lines.insert(idx + 1, f"  // {original.strip()}")
            desc = f"Disabled path traversal check at line {line_no}, disguised as 'validated by caller'"
            disguise = "Added a comment referencing a nonexistent validation function"
        else:
            # Wrap in a no-op: assign result but don't use it
            indent = len(original) - len(original.lstrip())
            spaces = " " * indent
            varname = f"_resolved{random.randint(100,999)}"
            lines[idx] = f"{spaces}const {varname} = {original.strip()}  // normalize for logging"
            lines.insert(idx + 1, f"{spaces}{original.strip().split('=')[0].strip()} = {original.strip().split('=')[1].strip() if '=' in original else match}")
            desc = f"Path normalization result captured but unused at line {line_no}"
            disguise = "Looks like a logging preparation step"

        return "\n".join(lines), desc, disguise


class HardcodedSecret(BugPattern):
    """Insert a realistic-looking API key constant near other constants."""

    def __init__(self):
        super().__init__("hardcoded_secret", "security", "CRITICAL", "both")

    def find_targets(self, content, filepath):
        targets = []
        for i, line in enumerate(content.split("\n"), 1):
            stripped = line.strip()
            # Find existing constant/config declarations to blend in
            if re.search(r'(const\s+[A-Z_]+\s*=|^[A-Z_]+\s*=)', stripped) and \
               not re.search(r'(require|import|from)', stripped):
                targets.append((i, stripped))
            # Also match non-uppercase const declarations
            elif re.search(r'const\s+\w+(Timeout|Limit|Max|Threshold|Port|Url|Path)\s*=', stripped):
                targets.append((i, stripped))
            # Python: match uppercase assignments at module level
            elif filepath.endswith(".py") and re.search(r'^[A-Z][A-Z_]*\s*=\s*', stripped):
                targets.append((i, stripped))
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        spaces = " " * indent

        # Generate a realistic-looking secret
        secret_type = random.choice([
            ("FALLBACK_AUTH_TOKEN", f"ghp_{''.join(random.choices('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=36))}"),
            ("INTERNAL_API_KEY", f"sk-{''.join(random.choices('abcdefghijklmnopqrstuvwxyz0123456789', k=48))}"),
            ("WEBHOOK_SECRET", f"whsec_{''.join(random.choices('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=32))}"),
            ("MONITORING_TOKEN", f"xoxb-{''.join(random.choices('0123456789', k=12))}-{''.join(random.choices('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=24))}"),
        ])

        name, value = secret_type
        filepath = context["filepath"]
        is_py = filepath.endswith(".py")

        if is_py:
            secret_line = f"{spaces}{name} = \"{value}\"  # fallback for CI environments"
        else:
            secret_line = f"{spaces}const {name} = \"{value}\";  // fallback for CI environments"

        # Insert after the matched constant
        lines.insert(idx + 1, secret_line)

        desc = f"Hardcoded {name} inserted after line {line_no}"
        disguise = "Placed next to existing constants with a 'CI fallback' comment"
        return "\n".join(lines), desc, disguise


class CommandInjection(BugPattern):
    """Add unsanitized string interpolation in a shell command."""

    def __init__(self):
        super().__init__("command_injection", "security", "CRITICAL", "ts")

    def find_targets(self, content, filepath):
        targets = []
        for i, line in enumerate(content.split("\n"), 1):
            if re.search(r'(spawn|exec|execSync|execFile)\s*\(', line):
                targets.append((i, line.strip()))
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        spaces = " " * indent

        # Add a "logging" line before the spawn that actually constructs an unsanitized command
        log_line = f"{spaces}const debugCmd = `${{containerArgs.join(' ')}} --label=${{groupFolder}}`;  // debug trace"
        lines.insert(idx, log_line)

        desc = f"Unsanitized shell argument interpolation via template literal at line {line_no}"
        disguise = "Looks like a debug logging statement"
        return "\n".join(lines), desc, disguise


class InsecureDeserialization(BugPattern):
    """Remove error handling around JSON.parse or json.loads."""

    def __init__(self):
        super().__init__("insecure_deserialization", "security", "MEDIUM", "both")

    def find_targets(self, content, filepath):
        targets = []
        in_try = False
        for i, line in enumerate(content.split("\n"), 1):
            stripped = line.strip()
            if stripped.startswith("try"):
                in_try = True
            if in_try and re.search(r'(JSON\.parse|json\.loads)\s*\(', stripped):
                targets.append((i, stripped))
            if stripped.startswith(("} catch", "except ")):
                in_try = False
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        spaces = " " * indent

        # Add a "fast path" that skips the try/catch
        is_py = "json.loads" in match
        if is_py:
            fast_path = f"{spaces}# Fast path: skip validation for known-good internal payloads"
            fast_parse = f"{spaces}if isinstance({match.split('(')[1].rstrip(')').strip()}, str) and len({match.split('(')[1].rstrip(')').strip()}) < 10000:"
            fast_return = f"{spaces}    return json.loads({match.split('(')[1].rstrip(')')})  # trusted input"
        else:
            arg = re.search(r'\((.*?)\)', match)
            arg_name = arg.group(1).strip() if arg else "data"
            fast_path = f"{spaces}// Fast path: skip validation for known-good internal payloads"
            fast_parse = f"{spaces}if (typeof {arg_name} === 'string' && {arg_name}.length < 10000) {{"
            fast_return = f"{spaces}  return JSON.parse({arg_name});  // trusted input"
            fast_close = f"{spaces}}}"

        lines.insert(idx, fast_path)
        lines.insert(idx + 1, fast_parse)
        lines.insert(idx + 2, fast_return)
        if not is_py:
            lines.insert(idx + 3, fast_close)

        desc = f"Added unprotected JSON parse 'fast path' bypassing try/catch at line {line_no}"
        disguise = "Labeled as 'fast path for trusted input' with a size check that looks reasonable"
        return "\n".join(lines), desc, disguise


# ---------------------------------------------------------------------------
# LOGIC PATTERNS
# ---------------------------------------------------------------------------

class OffByOne(BugPattern):
    """Change < to <= or > to >= in comparisons (not just loops)."""

    def __init__(self):
        super().__init__("off_by_one", "logic", "HIGH", "both")

    def find_targets(self, content, filepath):
        targets = []
        for i, line in enumerate(content.split("\n"), 1):
            # Match any comparison with < or > (not just in loops)
            if re.search(r'[^<>=!]\s*<\s*[^<=]', line) and not line.strip().startswith(("//", "#", "*", "/*")):
                targets.append((i, line.strip()))
            elif re.search(r'[^<>=!]\s*>\s*[^>=]', line) and not line.strip().startswith(("//", "#", "*", "/*")):
                targets.append((i, line.strip()))
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        original = lines[idx]

        # Flip < to <= or > to >=
        if re.search(r'[^<>=!]\s*<\s*[^<=]', original):
            modified = re.sub(r'([^<>=!]\s*)<(\s*[^<=])', r'\1<=\2', original, count=1)
            desc = f"Changed < to <= in loop condition at line {line_no} (off-by-one)"
        else:
            modified = re.sub(r'([^<>=!]\s*)>(\s*[^>=])', r'\1>=\2', original, count=1)
            desc = f"Changed > to >= in loop condition at line {line_no} (off-by-one)"

        lines[idx] = modified
        disguise = "Subtle operator change — looks like a boundary fix"
        return "\n".join(lines), desc, disguise


class MissingAwait(BugPattern):
    """Remove await from an async call that needs it."""

    def __init__(self):
        super().__init__("missing_await", "logic", "HIGH", "ts")

    def find_targets(self, content, filepath):
        targets = []
        for i, line in enumerate(content.split("\n"), 1):
            stripped = line.strip()
            # Find awaited calls that aren't assignments (so removing await is subtle)
            if re.search(r'\bawait\s+\w+\.\w+\(', stripped) and \
               not stripped.startswith(("const ", "let ", "var ", "return ")):
                targets.append((i, stripped))
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        original = lines[idx]

        # Remove the await keyword
        modified = re.sub(r'\bawait\s+', '', original, count=1)
        # Add a comment that makes it look intentional
        modified = modified.rstrip() + "  // fire-and-forget"

        lines[idx] = modified
        desc = f"Removed await from async call at line {line_no} — will cause race condition"
        disguise = "Added '// fire-and-forget' comment to make removal look intentional"
        return "\n".join(lines), desc, disguise


class InvertedCondition(BugPattern):
    """Flip && to || or !== to === in a critical check."""

    def __init__(self):
        super().__init__("inverted_condition", "logic", "HIGH", "both")

    def find_targets(self, content, filepath):
        targets = []
        is_py = filepath.endswith(".py")
        for i, line in enumerate(content.split("\n"), 1):
            stripped = line.strip()
            if is_py:
                if re.search(r'\bif\b.*\band\b', stripped) or re.search(r'\bif\b.*\bor\b', stripped):
                    targets.append((i, stripped))
            else:
                if re.search(r'\bif\s*\(.*&&', stripped) or re.search(r'\bif\s*\(.*\|\|', stripped):
                    targets.append((i, stripped))
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        original = lines[idx]
        is_py = "and " in original or "or " in original

        if is_py:
            if " and " in original:
                modified = original.replace(" and ", " or ", 1)
                desc = f"Flipped 'and' to 'or' in condition at line {line_no}"
            else:
                modified = original.replace(" or ", " and ", 1)
                desc = f"Flipped 'or' to 'and' in condition at line {line_no}"
        else:
            if "&&" in original:
                modified = original.replace("&&", "||", 1)
                desc = f"Flipped && to || in condition at line {line_no}"
            else:
                modified = original.replace("||", "&&", 1)
                desc = f"Flipped || to && in condition at line {line_no}"

        lines[idx] = modified
        disguise = "Single-character change in a boolean expression — easy to miss in diff review"
        return "\n".join(lines), desc, disguise


class SilentCatch(BugPattern):
    """Make a catch block swallow errors silently."""

    def __init__(self):
        super().__init__("silent_catch", "logic", "MEDIUM", "both")

    def find_targets(self, content, filepath):
        targets = []
        is_py = filepath.endswith(".py")
        for i, line in enumerate(content.split("\n"), 1):
            stripped = line.strip()
            if is_py:
                if re.search(r'except\s+\w+', stripped) and "pass" not in stripped:
                    targets.append((i, stripped))
            else:
                if re.search(r'}\s*catch\s*\(', stripped):
                    # Check next few lines for throw/log
                    next_lines = content.split("\n")[i:i+3]
                    has_rethrow = any("throw" in l or "log" in l.lower() or "console" in l for l in next_lines)
                    if has_rethrow:
                        targets.append((i, stripped))
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        is_py = lines[idx].strip().startswith("except")

        if is_py:
            # Find the next few lines after except and comment out the handler
            for j in range(idx + 1, min(idx + 4, len(lines))):
                if lines[j].strip() and not lines[j].strip().startswith("#"):
                    indent = len(lines[j]) - len(lines[j].lstrip())
                    lines[j] = " " * indent + "pass  # handled upstream"
                    break
        else:
            # Find throw/log in catch body and replace with comment
            for j in range(idx + 1, min(idx + 5, len(lines))):
                if "throw" in lines[j] or "log" in lines[j].lower() or "console" in lines[j]:
                    indent = len(lines[j]) - len(lines[j].lstrip())
                    lines[j] = " " * indent + "// error handled by caller"
                    break

        desc = f"Silenced error handling in catch block at line {line_no}"
        disguise = "Replaced error propagation with 'handled upstream/by caller' comment"
        return "\n".join(lines), desc, disguise


class WrongOperator(BugPattern):
    """Swap === to == or !== to != (loose equality in TS) or == to is/is not in Python."""

    def __init__(self):
        super().__init__("wrong_operator", "logic", "MEDIUM", "ts")

    def find_targets(self, content, filepath):
        targets = []
        for i, line in enumerate(content.split("\n"), 1):
            if "===" in line and "!==" not in line:
                targets.append((i, line.strip()))
            elif "!==" in line:
                targets.append((i, line.strip()))
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        original = lines[idx]

        if "!==" in original:
            modified = original.replace("!==", "!=", 1)
            desc = f"Changed !== to != (loose inequality) at line {line_no}"
        else:
            modified = original.replace("===", "==", 1)
            desc = f"Changed === to == (loose equality) at line {line_no}"

        lines[idx] = modified
        disguise = "Single character removal — extremely subtle in a diff"
        return "\n".join(lines), desc, disguise


# ---------------------------------------------------------------------------
# STYLE PATTERNS
# ---------------------------------------------------------------------------

class UnusedImport(BugPattern):
    """Add a plausible-looking import that's never used."""

    def __init__(self):
        super().__init__("unused_import", "style", "LOW", "both")

    def find_targets(self, content, filepath):
        targets = []
        is_py = filepath.endswith(".py")
        for i, line in enumerate(content.split("\n"), 1):
            stripped = line.strip()
            if is_py and re.search(r'^(import |from )', stripped):
                targets.append((i, stripped))
            elif not is_py and re.search(r'^import\s', stripped):
                targets.append((i, stripped))
            elif i > 15:
                break
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        filepath = context.get("filepath", "")
        is_py = filepath.endswith(".py")

        if is_py:
            imports = random.choice([
                "from typing import Protocol",
                "from functools import lru_cache",
                "from contextlib import asynccontextmanager",
                "from collections import defaultdict",
            ])
        else:
            imports = random.choice([
                "import { createHash } from 'crypto';",
                "import { performance } from 'perf_hooks';",
                "import { EventEmitter } from 'events';",
                "import { Readable } from 'stream';",
            ])

        lines.insert(idx + 1, imports)
        desc = f"Added unused import '{imports.strip()}' after line {line_no}"
        disguise = "Placed next to existing imports — looks like it belongs"
        return "\n".join(lines), desc, disguise


class DeadCodeBlock(BugPattern):
    """Add unreachable code after a return statement."""

    def __init__(self):
        super().__init__("dead_code", "style", "LOW", "both")

    def find_targets(self, content, filepath):
        targets = []
        is_py = filepath.endswith(".py")
        for i, line in enumerate(content.split("\n"), 1):
            stripped = line.strip()
            if is_py:
                if stripped.startswith("return ") and i < len(content.split("\n")):
                    targets.append((i, stripped))
            else:
                if stripped.startswith("return ") and stripped.endswith(";"):
                    targets.append((i, stripped))
        return targets

    def inject(self, content, line_no, match, context):
        lines = content.split("\n")
        idx = line_no - 1
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        spaces = " " * indent
        is_py = not match.endswith(";")

        # Add dead code that looks like a cleanup step
        if is_py:
            dead = [
                f"{spaces}# Cleanup temporary state",
                f"{spaces}log.debug('finalizing operation')",
            ]
        else:
            dead = [
                f"{spaces}// Cleanup temporary state",
                f"{spaces}log.debug('finalizing operation');",
            ]

        for j, d in enumerate(dead):
            lines.insert(idx + 1 + j, d)

        desc = f"Added unreachable code after return at line {line_no}"
        disguise = "Looks like cleanup code that should run, but is after return"
        return "\n".join(lines), desc, disguise


# ---------------------------------------------------------------------------
# PATTERN REGISTRY
# ---------------------------------------------------------------------------

ALL_PATTERNS: list[BugPattern] = [
    PathTraversalBypass(),
    HardcodedSecret(),
    CommandInjection(),
    InsecureDeserialization(),
    OffByOne(),
    MissingAwait(),
    InvertedCondition(),
    SilentCatch(),
    WrongOperator(),
    UnusedImport(),
    DeadCodeBlock(),
]


def select_patterns(count: int, seed: Optional[int] = None) -> list[BugPattern]:
    """Select N random patterns, ensuring category diversity."""
    rng = random.Random(seed)

    by_cat = {"security": [], "logic": [], "style": []}
    for p in ALL_PATTERNS:
        by_cat[p.category].append(p)

    selected = []
    # Ensure at least one from each category
    for cat in ["security", "logic", "style"]:
        if by_cat[cat] and count > 0:
            selected.append(rng.choice(by_cat[cat]))
            count -= 1

    # Fill remaining randomly
    remaining = [p for p in ALL_PATTERNS if p not in selected]
    rng.shuffle(remaining)
    selected.extend(remaining[:count])

    rng.shuffle(selected)
    return selected


def find_and_inject(
    pattern: BugPattern,
    used_files: set[str],
    seed: Optional[int] = None,
) -> Optional[InjectionResult]:
    """Try to inject a bug using the given pattern. Returns result or None."""
    rng = random.Random(seed)

    eligible_files = [
        f for f in TARGET_FILES
        if f not in used_files
        and (REPO_ROOT / f).exists()
        and (pattern.lang == "both"
             or (pattern.lang == "ts" and f.endswith(".ts"))
             or (pattern.lang == "py" and f.endswith(".py")))
    ]
    rng.shuffle(eligible_files)

    for filepath in eligible_files:
        full_path = REPO_ROOT / filepath
        content = full_path.read_text()
        targets = pattern.find_targets(content, filepath)

        if not targets:
            continue

        line_no, match = rng.choice(targets)
        context = {"filepath": filepath, "rng": rng}

        try:
            modified, desc, disguise = pattern.inject(content, line_no, match, context)
        except Exception as e:
            continue

        # Verify the content actually changed
        if modified == content:
            continue

        # Write the modified file
        full_path.write_text(modified)
        used_files.add(filepath)

        return InjectionResult(
            file=filepath,
            line=line_no,
            category=pattern.category,
            severity=pattern.severity,
            bug_type=pattern.name,
            description=desc,
            disguise=disguise,
        )

    return None


BENCHMARK_BRANCH = "benchmark/injected-bugs"


def _git(*args: str, check: bool = True, **kwargs) -> subprocess.CompletedProcess:
    """Run a git command in the repo root."""
    return subprocess.run(
        ["git", *args],
        capture_output=True, text=True, cwd=REPO_ROOT,
        check=check, **kwargs,
    )


def _get_current_branch() -> str:
    return _git("branch", "--show-current").stdout.strip()


def cmd_inject(count: int = 8, seed: Optional[int] = None):
    """Inject bugs on a throwaway branch. Original branch stays untouched."""
    # Refuse if working tree is dirty
    dirty = _git("diff", "--name-only")
    if dirty.stdout.strip():
        print(f"ERROR: Working tree has uncommitted changes. Commit or stash first.")
        sys.exit(1)

    # Refuse if benchmark branch already exists (leftover from a failed run)
    existing = _git("branch", "--list", BENCHMARK_BRANCH)
    if existing.stdout.strip():
        print(f"ERROR: Branch '{BENCHMARK_BRANCH}' already exists. Run `revert` first to clean up.")
        sys.exit(1)

    if seed is None:
        seed = random.randint(0, 2**32)

    # Record which branch we came from
    original_branch = _get_current_branch() or _git("rev-parse", "HEAD").stdout.strip()

    # === CRITICAL: Create throwaway branch BEFORE any file modifications ===
    # This is the safety gate — if this fails, no files have been touched.
    try:
        _git("checkout", "-b", BENCHMARK_BRANCH)
    except subprocess.CalledProcessError as e:
        print(f"CRITICAL: Failed to create benchmark branch. No files were modified.")
        print(f"  Error: {e.stderr.strip() if e.stderr else e}")
        sys.exit(1)

    # Verify we're actually on the benchmark branch (belt + suspenders)
    current = _get_current_branch()
    if current != BENCHMARK_BRANCH:
        print(f"CRITICAL: Expected to be on '{BENCHMARK_BRANCH}' but on '{current}'. Aborting.")
        sys.exit(1)

    print(f"Created throwaway branch: {BENCHMARK_BRANCH}")
    print(f"Original branch: {original_branch}")
    print(f"Seed: {seed} (reuse with --seed {seed} to reproduce)")

    patterns = select_patterns(count, seed)
    results: list[InjectionResult] = []
    used_files: set[str] = set()

    for i, pattern in enumerate(patterns):
        result = find_and_inject(pattern, used_files, seed=seed + i)
        if result:
            results.append(result)
            print(f"  [{result.category.upper():8}] {result.bug_type:25} → {result.file}:{result.line}")
        else:
            print(f"  [SKIP    ] {pattern.name:25} — no suitable injection point found")

    if not results:
        # Nothing injected — clean up the branch
        _git("checkout", original_branch)
        _git("branch", "-D", BENCHMARK_BRANCH)
        print("No bugs injected. Branch deleted.")
        return results

    # Commit the injected bugs on the throwaway branch
    _git("add", "-A")
    _git("commit", "-m", f"benchmark: inject {len(results)} bugs (seed {seed})", check=False)

    # Save manifest (ground truth)
    manifest = {
        "seed": seed,
        "original_branch": original_branch,
        "count": len(results),
        "injections": [asdict(r) for r in results],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))
    print(f"\nInjected {len(results)} bugs on '{BENCHMARK_BRANCH}'.")
    print(f"Manifest: {MANIFEST_PATH}")
    print("Run `python3 scripts/review_benchmark.py diff` to see the diff.")
    return results


def cmd_diff():
    """Show the unified diff of injected changes vs the original branch."""
    if not MANIFEST_PATH.exists():
        print("No manifest found. Run `inject` first.")
        return

    manifest = json.loads(MANIFEST_PATH.read_text())
    original = manifest.get("original_branch", "main")

    result = _git("diff", f"{original}...{BENCHMARK_BRANCH}", "--unified=5", check=False)
    if result.stdout:
        print(result.stdout)
    else:
        # Fallback: diff working tree
        result = _git("diff", "--unified=5", check=False)
        if result.stdout:
            print(result.stdout)
        else:
            print("No changes detected.")


def cmd_score(findings_json: str):
    """Score review findings against the ground truth manifest."""
    if not MANIFEST_PATH.exists():
        print("No manifest found. Run `inject` first.")
        sys.exit(1)

    manifest = json.loads(MANIFEST_PATH.read_text())
    injections = manifest["injections"]

    try:
        findings = json.loads(Path(findings_json).read_text() if os.path.isfile(findings_json) else findings_json)
    except json.JSONDecodeError:
        print(f"Invalid JSON: {findings_json}")
        sys.exit(1)

    if isinstance(findings, dict):
        findings = findings.get("findings", [])

    # Match findings to injections by file + proximity
    detected = set()
    false_positives = []

    for finding in findings:
        f_file = finding.get("file", "")
        f_line = finding.get("line", 0)
        matched = False

        for i, inj in enumerate(injections):
            if i in detected:
                continue
            if inj["file"] in f_file or f_file in inj["file"]:
                if abs(f_line - inj["line"]) <= 5:  # within 5 lines
                    detected.add(i)
                    matched = True
                    break

        if not matched:
            false_positives.append(finding)

    # Results
    total = len(injections)
    found = len(detected)
    missed = total - found
    fp = len(false_positives)

    precision = found / (found + fp) if (found + fp) > 0 else 0
    recall = found / total if total > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    print(f"\n{'='*50}")
    print(f"CODE REVIEW BENCHMARK RESULTS")
    print(f"{'='*50}")
    print(f"  Bugs injected:    {total}")
    print(f"  Detected:         {found} ({found/total*100:.0f}%)" if total else "")
    print(f"  Missed:           {missed}")
    print(f"  False positives:  {fp}")
    print(f"  Precision:        {precision:.2f}")
    print(f"  Recall:           {recall:.2f}")
    print(f"  F1 Score:         {f1:.2f}")
    print()

    # Detail: what was missed
    if missed:
        print("MISSED BUGS:")
        for i, inj in enumerate(injections):
            if i not in detected:
                print(f"  [{inj['severity']:8}] {inj['bug_type']:25} {inj['file']}:{inj['line']}")
                print(f"           {inj['description']}")
                print(f"           Disguise: {inj['disguise']}")
        print()

    # Detail: false positives
    if false_positives:
        print("FALSE POSITIVES:")
        for fp_item in false_positives:
            print(f"  {fp_item.get('file', '?')}:{fp_item.get('line', '?')} — {fp_item.get('title', fp_item.get('description', '?'))}")
        print()

    return {"precision": precision, "recall": recall, "f1": f1}


def cmd_revert():
    """Discard the throwaway branch and return to the original branch.

    The bugs only exist on the benchmark branch — deleting it removes them
    completely. The original branch is never modified.
    """
    original_branch = None

    if MANIFEST_PATH.exists():
        manifest = json.loads(MANIFEST_PATH.read_text())
        original_branch = manifest.get("original_branch")
        MANIFEST_PATH.unlink()

    current = _get_current_branch()

    # Safety: refuse to delete main/master
    PROTECTED = {"main", "master"}
    if BENCHMARK_BRANCH in PROTECTED:
        print(f"CRITICAL: benchmark branch name '{BENCHMARK_BRANCH}' is a protected branch. Aborting.")
        sys.exit(1)

    # If we're on the benchmark branch, switch away first
    if current == BENCHMARK_BRANCH:
        target = original_branch or "main"
        _git("checkout", target)
        print(f"Switched to {target}.")

    # Delete the benchmark branch if it exists
    existing = _git("branch", "--list", BENCHMARK_BRANCH)
    if existing.stdout.strip():
        _git("branch", "-D", BENCHMARK_BRANCH)
        print(f"Deleted branch '{BENCHMARK_BRANCH}'. All injected bugs are gone.")
    else:
        print("No benchmark branch found — nothing to clean up.")


def cmd_run(count: int = 8, seed: Optional[int] = None):
    """Full pipeline: inject on throwaway branch → diff → cleanup.

    The diff and manifest are printed to stdout. The throwaway branch
    is deleted at the end — the original branch is never touched.
    """
    results = cmd_inject(count, seed)

    if not results:
        print("No bugs injected — nothing to test.")
        return

    print(f"\n{'='*50}")
    print("DIFF (feed this to the review pipeline)")
    print(f"{'='*50}\n")
    cmd_diff()

    print(f"\n{'='*50}")
    print("GROUND TRUTH MANIFEST")
    print(f"{'='*50}\n")
    print(MANIFEST_PATH.read_text())

    # Cleanup: delete the throwaway branch
    print(f"\n{'='*50}")
    print("CLEANUP")
    print(f"{'='*50}\n")
    cmd_revert()

    print("\nTo score: save review findings as JSON and run:")
    print("  python3 scripts/review_benchmark.py score <findings.json>")


def main():
    parser = argparse.ArgumentParser(
        prog="review_benchmark",
        description="Dynamic bug injection benchmark for code review pipelines",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_inject = sub.add_parser("inject", help="Inject disguised bugs into source files")
    p_inject.add_argument("--count", type=int, default=8, help="Number of bugs to inject (default: 8)")
    p_inject.add_argument("--seed", type=int, help="Random seed for reproducibility")

    sub.add_parser("diff", help="Show unified diff of injected changes")

    p_score = sub.add_parser("score", help="Score findings against ground truth")
    p_score.add_argument("findings", help="Path to findings JSON or inline JSON string")

    sub.add_parser("revert", help="Revert all injected changes")

    p_run = sub.add_parser("run", help="Full pipeline: inject → diff → revert")
    p_run.add_argument("--count", type=int, default=8, help="Number of bugs to inject (default: 8)")
    p_run.add_argument("--seed", type=int, help="Random seed for reproducibility")

    args = parser.parse_args()

    if args.cmd == "inject":
        cmd_inject(count=args.count, seed=args.seed)
    elif args.cmd == "diff":
        cmd_diff()
    elif args.cmd == "score":
        cmd_score(args.findings)
    elif args.cmd == "revert":
        cmd_revert()
    elif args.cmd == "run":
        cmd_run(count=args.count, seed=args.seed)


if __name__ == "__main__":
    main()
