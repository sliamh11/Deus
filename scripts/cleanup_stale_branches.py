#!/usr/bin/env python3
"""
Stale branch cleanup utility for Deus.

Detects branches that have been squash-merged into the base branch using `git cherry`.
After a squash-merge, `git branch --merged` won't detect the branch as merged
because the merge commit SHAs don't match. `git cherry <base> <branch>` will show
all commits as `-` (equivalent) when none of the branch's work remains unmerged.

Exit codes:
  0 (SUCCESS)   — stale branches found (or deleted in --delete mode)
  3 (NOT_FOUND) — no stale branches detected

Usage:
  python3 scripts/cleanup_stale_branches.py              # dry-run: list stale branches
  python3 scripts/cleanup_stale_branches.py --delete     # actually delete stale branches
  python3 scripts/cleanup_stale_branches.py --json       # structured JSON output
  python3 scripts/cleanup_stale_branches.py --protect feat/keep-this,chore/also-keep
  python3 scripts/cleanup_stale_branches.py --base-branch develop  # use custom base
  python3 scripts/cleanup_stale_branches.py --delete --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Ensure scripts/ is importable when invoked from the project root
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _exit_codes import NOT_FOUND, SUCCESS
from _agent_io import agent_output

# Branches that should never be deleted, regardless of cherry status
_STATIC_PROTECTED: frozenset[str] = frozenset({"main", "master", "develop", "dev"})


def get_default_branch() -> str:
    """Detect the default remote branch (e.g. main, master) via symbolic-ref."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD", "--short"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip().removeprefix("origin/")
    except subprocess.CalledProcessError:
        return "main"


def _run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    return result.returncode, result.stdout, result.stderr


def get_current_branch() -> str | None:
    """Return the name of the current branch, or None if detached HEAD."""
    rc, stdout, _ = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if rc != 0:
        return None
    name = stdout.strip()
    return None if name == "HEAD" else name


def get_worktree_branches() -> set[str]:
    """Return a set of branch names that currently have an open worktree.

    Uses --porcelain format to avoid whitespace-fragile parsing.
    Lines of interest look like:  branch refs/heads/<name>
    """
    rc, stdout, _ = _run(["git", "worktree", "list", "--porcelain"])
    if rc != 0:
        return set()

    branches: set[str] = set()
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("branch "):
            ref = line[len("branch "):]
            prefix = "refs/heads/"
            if ref.startswith(prefix):
                branches.add(ref[len(prefix):])
    return branches


def list_local_branches() -> list[str]:
    """Return all local branch names."""
    rc, stdout, _ = _run(["git", "branch", "--format=%(refname:short)"])
    if rc != 0:
        return []
    return [b.strip() for b in stdout.splitlines() if b.strip()]


def is_squash_merged(branch: str, base_branch: str = "main") -> bool | None:
    """Return True if all commits on branch are squash-merged into base_branch.

    Uses `git cherry <base_branch> <branch>`:
      - Lines starting with `-` mean the commit is equivalent to something in base_branch.
      - Lines starting with `+` mean the commit is NOT in base_branch.
      - Empty output means the branch tip equals base_branch (no divergent commits) — treat as stale.

    Returns None on git failure.
    """
    rc, stdout, _ = _run(["git", "cherry", base_branch, branch])
    if rc != 0:
        return None

    lines = [l.strip() for l in stdout.splitlines() if l.strip()]

    # Empty output: branch has no commits beyond main — it is stale.
    if not lines:
        return True

    # Stale iff every line is a `-` line (all commits accounted for in main).
    return all(line.startswith("-") for line in lines)


def delete_branch(branch: str) -> tuple[bool, str]:
    """Force-delete a local branch. Returns (success, error_message)."""
    rc, _, stderr = _run(["git", "branch", "-D", branch])
    if rc != 0:
        return False, stderr.strip()
    return True, ""


def build_skip_set(
    current_branch: str | None,
    worktree_branches: set[str],
    extra_protected: set[str],
    base_branch: str = "main",
) -> dict[str, str]:
    """Build a map of branch-name -> skip-reason for all branches to never touch."""
    always_protected = _STATIC_PROTECTED | {base_branch}
    skip: dict[str, str] = {}
    for b in always_protected | extra_protected:
        skip[b] = "protected"
    if current_branch:
        skip[current_branch] = "current"
    for b in worktree_branches:
        if b not in skip:
            skip[b] = "worktree"
    return skip


def run(
    delete: bool = False,
    use_json: bool = False,
    extra_protected: set[str] | None = None,
    base_branch: str | None = None,
) -> int:
    """Core logic. Returns an exit code."""
    if extra_protected is None:
        extra_protected = set()
    if base_branch is None:
        base_branch = get_default_branch()

    current_branch = get_current_branch()
    worktree_branches = get_worktree_branches()
    all_branches = list_local_branches()
    skip_map = build_skip_set(current_branch, worktree_branches, extra_protected, base_branch)

    stale: list[str] = []
    deleted: list[str] = []
    skipped: list[dict[str, str]] = []

    for branch in all_branches:
        if branch in skip_map:
            skipped.append({"branch": branch, "reason": skip_map[branch]})
            continue

        merged = is_squash_merged(branch, base_branch)
        if merged is None:
            # git cherry failed for this branch — skip silently
            skipped.append({"branch": branch, "reason": "git-error"})
            continue

        if not merged:
            skipped.append({"branch": branch, "reason": "unmerged"})
            continue

        stale.append(branch)

        if delete:
            ok, err = delete_branch(branch)
            if ok:
                deleted.append(branch)
            else:
                print(f"ERROR: could not delete {branch}: {err}", file=sys.stderr)

    result = {
        "stale": stale,
        "deleted": deleted,
        "skipped": skipped,
    }

    json_str = agent_output(result, use_json=use_json)
    if json_str is not None:
        print(json_str)
    else:
        if not stale:
            print("No stale branches found.")
        else:
            for b in stale:
                status = "deleted" if (delete and b in deleted) else "stale"
                print(f"  [{status}] {b}")
            if not delete:
                print(
                    f"\n{len(stale)} stale branch(es) found. "
                    "Run with --delete to remove them."
                )

    return SUCCESS if stale else NOT_FOUND


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect and optionally delete branches squash-merged into the base branch."
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        default=False,
        help="Delete stale branches (default: dry-run, list only)",
    )
    parser.add_argument(
        "--json",
        dest="use_json",
        action="store_true",
        default=False,
        help="Emit structured JSON output",
    )
    parser.add_argument(
        "--protect",
        metavar="BRANCH1,BRANCH2",
        default="",
        help="Comma-separated list of additional branches to never delete",
    )
    parser.add_argument(
        "--base-branch",
        metavar="BRANCH",
        default=None,
        help=(
            "Base branch to compare against (default: auto-detected from "
            "refs/remotes/origin/HEAD, falling back to 'main')"
        ),
    )
    args = parser.parse_args()

    extra = {b.strip() for b in args.protect.split(",") if b.strip()}

    sys.exit(
        run(
            delete=args.delete,
            use_json=args.use_json,
            extra_protected=extra,
            base_branch=args.base_branch,
        )
    )


if __name__ == "__main__":
    main()
