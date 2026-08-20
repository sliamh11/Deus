"""Single source of truth for the auto-memory directory.

The auto-memory population (memory_indexer-promoted atoms, feedback, and
procedures) lives in the Claude project memory dir. ``memory_indexer`` writes
here, ``memory_tree`` indexes from here, ``memory_query`` reads node content
from here, and ``standards_pack`` loads standard atoms from here. They MUST
resolve the SAME directory or a node indexes under an ``auto-memory/`` namespace
path that recall cannot read back (LIA-341) — the live symptom was
``memory_query`` defaulting to a non-existent ``~/.deus/auto-memory`` and
returning ``None`` for every promoted feedback node.

Kept dependency-free (os + pathlib only) so SessionStart-critical importers like
``standards_pack`` add no import weight.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

EXTERNAL_DIR_ENV = "DEUS_AUTO_MEMORY_DIR"

# Sentinel project id for this repo's own (legacy, bare-namespace) auto-memory
# population — a literal product name, not a personal path, so it is safe to
# bake into a public repo. See resolve_project_id().
DEUS_PROJECT_ID = "deus"


def _encode_project_dir(project_dir: str) -> str:
    """Encode a project path the way Claude Code names its project memory dir:
    path separators become dashes, with a leading dash.

    Windows ``CLAUDE_PROJECT_DIR`` uses backslashes, so collapse those first —
    ``standards_pack``'s original encoding only replaced ``/`` and would leave a
    Windows path with raw backslashes, silently missing the directory.
    """
    encoded = project_dir.replace("\\", "-").replace("/", "-")
    if not encoded.startswith("-"):
        encoded = "-" + encoded
    return encoded


def resolve_auto_memory_dir() -> Path:
    """Resolve the canonical auto-memory directory.

    Priority: explicit ``DEUS_AUTO_MEMORY_DIR`` override -> the
    ``CLAUDE_PROJECT_DIR``-derived project memory dir -> this repo's project
    memory dir (derived from the module's location) -> ``~/.deus/auto-memory``
    fallback. Mirrors ``memory_indexer.py``'s promotion target. The two derived
    steps only match when the candidate directory exists; otherwise resolution
    falls through to the next step.
    """
    env = os.environ.get(EXTERNAL_DIR_ENV)
    if env:
        return Path(env).expanduser()

    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if project_dir:
        candidate = Path(
            os.path.expanduser(
                f"~/.claude/projects/{_encode_project_dir(project_dir)}/memory"
            )
        )
        if candidate.is_dir():
            return candidate

    repo_root = Path(__file__).resolve().parent.parent
    legacy = Path(
        os.path.expanduser(
            f"~/.claude/projects/{_encode_project_dir(repo_root.as_posix())}/memory"
        )
    )
    if legacy.is_dir():
        return legacy

    return Path(os.path.expanduser("~/.deus/auto-memory"))


def _git_output(cmd: list[str], cwd: Path) -> str | None:
    """Run a read-only git command, return stripped stdout or None on failure.

    Mirrors scripts/drift_check.py's `_git_output` idiom (same repo, same
    pattern) so worktree-detection behaves identically everywhere it's used.
    """
    try:
        r = subprocess.run(
            ["git", *cmd], cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        return r.stdout.strip() if r.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _unwind_worktree(start: Path) -> Path:
    """Resolve `start` to its real repo root, unwinding a linked git worktree.

    `git rev-parse --git-common-dir` returns the SHARED `.git` metadata
    directory: `<repo>/.git` inside a linked worktree, or a bare `.git`
    (relative to `start`) in a normal checkout -- never the repo root
    itself. Anchor git's possibly-relative output to `start` before
    resolving (same trick as drift_check.py:133-138 --
    `(start / common).resolve(strict=False)` is correct whether `common` is
    relative or absolute, since pathlib drops the left side when the right
    is absolute), then take `.parent` of the resolved `.git` dir to recover
    the actual repo root. Falls back to `start` unchanged when git is
    unavailable, `start` isn't a repo, or the resolved path's basename
    isn't `.git` (defensive; not expected in practice).
    """
    common = _git_output(["rev-parse", "--git-common-dir"], start)
    if not common:
        return start
    common_path = (start / common).resolve(strict=False)
    return common_path.parent if common_path.name == ".git" else start


def resolve_project_root() -> Path | None:
    """The parent repo root for the CURRENT session, worktree-normalized.

    Uses CLAUDE_PROJECT_DIR (which is the WORKTREE path inside a linked
    worktree, not the real repo) and unwinds it via `_unwind_worktree`.
    Returns None when CLAUDE_PROJECT_DIR is unset -- callers must treat
    that as "no scope", never as an empty result set.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        return None
    return _unwind_worktree(Path(project_dir).expanduser())


def resolve_this_repo_dir_name() -> str:
    """The literal ``~/.claude/projects/<dirname>`` encoding of THIS repo,
    worktree-unwound.

    Shares `_unwind_worktree` with `resolve_project_id()` deliberately: a raw
    `Path(__file__).resolve().parent.parent` (no unwind) resolves to the
    *worktree's* root when this module is loaded from a linked worktree --
    the default dev layout for this repo -- which would compute the wrong
    directory name and misclassify the real "deus" auto-memory population
    under a stray per-worktree project tag on every `--all-projects` run
    launched from a worktree session (LIA-122).
    """
    this_repo = _unwind_worktree(Path(__file__).resolve().parent.parent)
    return _encode_project_dir(this_repo.as_posix())


def resolve_project_id() -> str | None:
    """Stable project identifier for memory-tree scoping.

    ONE shared computation used both to tag nodes at index time (a future
    P4 caller of reindex_external's `project=` kwarg) and to derive
    retrieval-time `project_scope` (memory_retrieval_hook.py) -- a prior
    review found two parallel implementations of "which project is this"
    that could drift apart on exactly the worktree-normalization step, so
    both paths must call this one function, never re-derive it.

    Returns DEUS_PROJECT_ID ("deus") when the resolved project root IS this
    very repo -- a SELF-REFERENTIAL comparison (both sides run through the
    identical `_unwind_worktree` primitive, so a linked-worktree session of
    THIS repo still matches its own main checkout), never a hardcoded
    personal path. Returns the dash-encoded root for any other resolvable
    project. Returns None when unresolvable (no CLAUDE_PROJECT_DIR) --
    callers fall back to "no scope", matching today's behaviour.
    """
    root = resolve_project_root()
    if root is None:
        return None
    this_repo = _unwind_worktree(Path(__file__).resolve().parent.parent)
    try:
        if root.resolve(strict=False) == this_repo.resolve(strict=False):
            return DEUS_PROJECT_ID
    except OSError:
        pass
    return _encode_project_dir(root.as_posix())
