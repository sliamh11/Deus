"""Repository identity and staged-tree subject resolution for warden_policy.

Two identifiers, both derived straight from `git`, never invented:

- ``repo_id``: a hash of the repo's canonical absolute common-dir path. Linked
  worktrees of one repo share a common-dir, so they share one ``repo_id`` --
  this is what avoids the cwd/worktree-bucket-mismatch class of bug
  (LIA-446/467) the wider Deus warden system has hit before. Hashed (not the
  raw path) so it's safe to put in policy input, block messages, and logs.
- ``subject_key``: the sha of the tree the CURRENT INDEX would produce via
  ``git write-tree`` -- byte-identical to what a plain ``git commit`` writes,
  verifiable post-hoc via ``git rev-parse HEAD^{tree}``. Deliberately NOT a
  synthetic snapshot of the whole working tree (staged + unstaged +
  untracked): binding to unstaged edits would false-block ordinary partial
  staging, and it's not what actually gets committed.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


class GitSubjectError(Exception):
    """Raised when git state can't be resolved (not a repo, git missing, etc.)."""


def _git(repo_path: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GitSubjectError(f"git {' '.join(args)} failed: {exc}") from exc
    return result.stdout.strip()


def resolve_repo_id(repo_path: Path) -> str:
    """Return ``git-common-dir-sha256:<hex>`` for *repo_path*.

    Linked worktrees of the same repo share a common-dir and therefore the
    same repo_id. Two unrelated repos never collide (sha256 of an absolute,
    canonical path).
    """
    common_dir = _git(repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir")
    canonical = str(Path(common_dir).resolve())
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"git-common-dir-sha256:{digest}"


def resolve_subject_key(repo_path: Path) -> str:
    """Return ``git-tree:<object-format>:<oid>`` for the CURRENT INDEX of *repo_path*.

    ``git write-tree`` writes (or looks up an existing) tree object for
    exactly what's staged right now -- unstaged and untracked changes do not
    affect it. This is deliberately the same tree a plain ``git commit``
    would create.
    """
    object_format = _git(repo_path, "rev-parse", "--show-object-format") or "sha1"
    oid = _git(repo_path, "write-tree")
    if not oid:
        raise GitSubjectError("git write-tree returned no output")
    return f"git-tree:{object_format}:{oid}"


def resolve(repo_path: Path) -> tuple[str, str]:
    """Convenience: return ``(repo_id, subject_key)`` for *repo_path*."""
    return resolve_repo_id(repo_path), resolve_subject_key(repo_path)
