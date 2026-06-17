#!/usr/bin/env python3
"""Layer 2 of the LIA-143 facade-prevention mechanism: net-new DEUS_* flag-gate lint.

Scans a PR diff for newly introduced ``DEUS_*`` feature-flag gates and requires
each to cite a tracking issue (``LIA-NNN`` / ``#NNN``) or carry a ``dev-only``
escape in an adjacent comment. A flag is *net-new* only if its name does not
already appear anywhere in the base ref — re-reading one of the ~40 existing
flags never fires.

Why: every facade in the LIA-133 audit was merged behind a flag that was never
turned on. Forcing a citation at merge time ties each new flag to its intent and
makes an untracked dark-ship visible. This is the deterministic, hard-failing
companion to the advisory ``connectivity-wiring`` warden rule (Layer 1).

Scope: this catches flags that are *read* as an env gate (``process.env.DEUS_*``,
``os.environ.get`` / ``getenv`` / ``os.environ[...]``, shell ``$DEUS_*``). A flag
that is only *mentioned* in a comment/TODO and never actually read gates nothing,
so it is out of scope here by design — that module-level "documented but unwired"
form is covered by the Layer 1 connectivity rule and the Layer 3 orphan-sweep.

The 3-line citation window covers lines visible in the diff (added + context);
a citation in unchanged code outside the hunk is not seen.

See docs/decisions/facade-prevention-mechanism.md.

Exit codes: 0 = clean, 1 = uncited net-new flag(s), 2 = usage/git error.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass

# Never scanned — the lint's own source/fixtures would trip on their DEUS_* examples.
EXCLUDED_PREFIXES = (
    "scripts/ci/",
    "scripts/tests/",
    "tests/",
    "evolution/tests/",
)

CODE_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".py", ".sh", ".bash", ".zsh")
SHELL_EXTS = (".sh", ".bash", ".zsh")

# Ops/framework DEUS_* vars that gate nothing and need no citation; extend as needed.
# DEUS_REPO: internal computed repo-root path (scripts/deus_init.sh) — never read as
# an env gate, gates nothing; resolved from the script's own location.
# DEUS_PORT / DEUS_TOKEN: the Deus backend port + bearer token that `deus web`
# passes to scripts/webui-serve.sh — config plumbing for an already-wired
# launcher; they toggle no feature (DEUS_TOKEN's absence is a fatal check, not
# a feature gate).
ALLOWLIST: frozenset[str] = frozenset({"DEUS_REPO", "DEUS_PORT", "DEUS_TOKEN"})

# Env-gate forms across TS/JS and Python. The flag name is the capture group.
_GENERAL_GATE_PATTERNS = (
    r"process\.env\.(DEUS_[A-Z0-9_]+)",
    r"""process\.env\[\s*['"](DEUS_[A-Z0-9_]+)['"]\s*\]""",
    r"""os\.environ\.get\(\s*['"](DEUS_[A-Z0-9_]+)['"]""",
    r"""os\.environ\[\s*['"](DEUS_[A-Z0-9_]+)['"]\s*\]""",
    r"""os\.getenv\(\s*['"](DEUS_[A-Z0-9_]+)['"]""",
)
# Shell env read: $DEUS_X or ${DEUS_X}. Only applied to shell files.
_SHELL_GATE_PATTERN = r"(?<![\w$])\$\{?(DEUS_[A-Z0-9_]+)\}?"

_GENERAL_RE = [re.compile(p) for p in _GENERAL_GATE_PATTERNS]
_SHELL_RE = re.compile(_SHELL_GATE_PATTERN)

_CITATION_RE = re.compile(r"(LIA-\d+|#\d+)")
_DEV_ONLY_RE = re.compile(r"dev-only", re.IGNORECASE)

# How far (in new-file lines) a citation/escape may sit from the flag gate.
_CITE_WINDOW = 3


@dataclass(frozen=True)
class DiffLine:
    """One line from a unified diff, with its new-file line number."""

    lineno: int
    added: bool  # True for '+' lines, False for ' ' context lines
    text: str


@dataclass(frozen=True)
class Violation:
    path: str
    lineno: int
    flag: str


def parse_diff(diff_text: str) -> dict[str, list[DiffLine]]:
    """Parse a unified diff into per-file lines with new-file line numbers.

    Context lines are retained (not just '+' lines) so a citation comment already
    present next to a newly added gate still counts toward the window.
    """
    files: dict[str, list[DiffLine]] = {}
    cur_path: str | None = None
    new_lineno = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            # "+++ b/path" or "+++ /dev/null"
            cur_path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            if cur_path is not None:
                files.setdefault(cur_path, [])
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            new_lineno = int(m.group(1)) if m else 0
            continue
        if cur_path is None:
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            files[cur_path].append(DiffLine(new_lineno, True, raw[1:]))
            new_lineno += 1
        elif raw.startswith(" "):
            files[cur_path].append(DiffLine(new_lineno, False, raw[1:]))
            new_lineno += 1
        # '-' and '\' (no-newline) lines do not advance the new-file counter.
    return files


def _is_scannable(path: str) -> bool:
    if path.startswith(EXCLUDED_PREFIXES):
        return False
    if not path.endswith(CODE_EXTS):
        return False
    fname = path.rsplit("/", 1)[-1]
    if ".test." in fname or ".spec." in fname or "/__tests__/" in f"/{path}":
        return False
    return True


def _flags_in_line(text: str, is_shell: bool) -> set[str]:
    flags: set[str] = set()
    for rx in _GENERAL_RE:
        flags.update(rx.findall(text))
    if is_shell:
        flags.update(_SHELL_RE.findall(text))
    return flags


def _has_citation_near(lines: list[DiffLine], lineno: int) -> bool:
    for ln in lines:
        if abs(ln.lineno - lineno) <= _CITE_WINDOW:
            if _CITATION_RE.search(ln.text) or _DEV_ONLY_RE.search(ln.text):
                return True
    return False


def flag_exists_in_base(flag: str, base_ref: str, repo_dir: str) -> bool:
    """True if ``flag`` appears anywhere in ``base_ref`` (so it is not net-new).

    Conservative bias: any error or match is treated as "exists" so the lint
    never hard-fails on a flag that is actually pre-existing.
    """
    try:
        rc = subprocess.run(
            ["git", "-C", repo_dir, "grep", "-q", "-F", "-e", flag, base_ref],
            capture_output=True,
        ).returncode
    except OSError:
        return True
    # 0 = match; 1 = absent; >1 = git error (e.g. unknown ref) → treat as exists, never false-block.
    return rc != 1


def scan(
    files: dict[str, list[DiffLine]],
    base_ref: str,
    repo_dir: str,
    allowlist: frozenset[str] = ALLOWLIST,
) -> list[Violation]:
    violations: list[Violation] = []
    base_cache: dict[str, bool] = {}
    for path, lines in files.items():
        if not _is_scannable(path):
            continue
        is_shell = path.endswith(SHELL_EXTS)
        for ln in lines:
            if not ln.added:
                continue
            for flag in _flags_in_line(ln.text, is_shell):
                if flag in allowlist:
                    continue
                if flag not in base_cache:
                    base_cache[flag] = flag_exists_in_base(flag, base_ref, repo_dir)
                if base_cache[flag]:
                    continue  # pre-existing flag, just read again
                if _has_citation_near(lines, ln.lineno):
                    continue
                violations.append(Violation(path, ln.lineno, flag))
    return violations


def _get_diff(base_ref: str, repo_dir: str) -> str:
    return subprocess.run(
        ["git", "-C", repo_dir, "diff", f"{base_ref}...HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default="origin/main", help="base ref to diff against")
    ap.add_argument("--repo", default=".", help="repo root")
    args = ap.parse_args(argv)

    try:
        diff_text = _get_diff(args.base, args.repo)
    except (subprocess.CalledProcessError, OSError) as exc:
        print(f"flag-lint: git diff failed: {exc}", file=sys.stderr)
        return 2

    violations = scan(parse_diff(diff_text), args.base, args.repo)
    if not violations:
        print("flag-lint: no uncited net-new DEUS_* flag gates.")
        return 0

    print("flag-lint: net-new DEUS_* flag gate(s) without a tracking citation:\n", file=sys.stderr)
    for v in violations:
        print(f"  {v.path}:{v.lineno}  {v.flag}", file=sys.stderr)
    print(
        "\nEach new DEUS_* flag must cite a tracking issue (LIA-NNN or #NNN) or carry a\n"
        "`dev-only` comment within 3 lines, so it cannot become a built-but-not-wired\n"
        "facade. See docs/decisions/facade-prevention-mechanism.md.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
