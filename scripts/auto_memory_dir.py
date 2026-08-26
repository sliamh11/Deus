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
    return _encode_project_dir(this_repo_root().as_posix())


def _same_dir(a: Path, b: Path) -> bool:
    """True when two paths name the SAME directory on disk.

    Compares ``(st_dev, st_ino)`` rather than strings. LIA-125: on a
    case-insensitive filesystem (macOS by default) a repo reached as ``~/Repo``
    and as ``~/repo`` is ONE directory with one inode, and ``Path.resolve()``
    does NOT normalise case -- it preserves whichever spelling reached the
    module. ``resolve_this_repo_dir_name()`` therefore encodes a different
    string depending on how it was imported, while the on-disk projects
    directory is fixed at whichever spelling created it. An encoded-string
    compare then silently misclassifies this repo's own auto-memory as a
    FOREIGN project under the other spelling -- and if that is the spelling the
    retrieval hook happens to be registered under, the failing branch is the
    default one rather than the exotic one.

    Inode identity closes the whole class rather than just the case collision:
    symlinks, bind mounts, and any other path that reaches the same directory
    by a different name all compare equal. ``~/.deus/auto-memory`` is already
    reached through a symlink from this repo's project memory directory, so the
    class is live here, not hypothetical.

    Falls back to a case-folded ``realpath`` compare when ``stat`` raises (a
    path that does not exist, or is unreadable) -- never to a bare string
    compare, which is the bug.
    """
    try:
        sa, sb = a.stat(), b.stat()
        return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)
    except OSError:
        return os.path.realpath(a).casefold() == os.path.realpath(b).casefold()


def this_repo_root() -> Path:
    """This repo's root, worktree-unwound. One definition, used by both
    :func:`is_this_repo` and :func:`resolve_project_id`."""
    return _unwind_worktree(Path(__file__).resolve().parent.parent)


def is_this_repo(path: Path | str) -> bool:
    """True when ``path`` IS this repo's root, by filesystem identity.

    The single predicate every caller must use instead of comparing encoded
    directory-name strings. See :func:`_same_dir` for why string comparison is
    wrong here.
    """
    candidate = Path(path).expanduser()
    return _same_dir(_unwind_worktree(candidate), this_repo_root())


def _encode_segment(name: str) -> str:
    """One path SEGMENT as Claude Code names it inside ``~/.claude/projects``.

    It collapses ``.`` as well as the separators, so ``.claude`` becomes
    ``-claude`` and ``example.com`` becomes ``example-com``. That extra
    substitution is what makes the encoding non-invertible, and is the reason
    :func:`decode_project_dir_name` matches FORWARD instead of guessing at a
    reverse mapping.
    """
    return name.replace("\\", "-").replace("/", "-").replace(".", "-")


# A directory with more children than this is not a plausible step on the way
# to a project root; refuse to scan it rather than stall the walk.
_DECODE_MAX_CHILDREN = 4096


def decode_project_dir_name(
    dir_name: str, *, root: str = "/", limit: int = 32
) -> list[str]:
    """Resolve a ``~/.claude/projects/<dir_name>`` basename back to real paths.

    Returns every existing directory whose encoding equals ``dir_name`` --
    normally exactly one, empty for a project whose repo has been moved or
    deleted, and more than one only for a genuine collision.

    LIA-123: the encoding is LOSSY, so it cannot be inverted. ``/`` and ``.``
    both become ``-``, and repo names in this corpus contain literal dashes
    (``cyber-olympians-platform``, ``quote-builder``), so
    ``-Users-x-example-com`` is ambiguous between ``example/com``,
    ``example-com`` and ``example.com`` on the string alone.

    So do not invert it. At each level, list the directory's REAL children,
    run each through :func:`_encode_segment`, and descend into any child whose
    encoded form is a prefix of the remaining token string. The filesystem
    prunes the search, and the result is exact by construction for every
    character the encoder collapses -- including ones added later.

    Two earlier approaches were measured and rejected: a dot-aware inverse
    decoder cannot recover a dot INSIDE a name, and reading ``cwd`` from the
    directory's session transcripts round-trips on only 10 of 89 directories
    (a session's recorded cwd is where it ended up, not where it launched).
    """
    found: dict[tuple[int, int], str] = {}

    def walk(base: str, rest: str) -> None:
        if len(found) >= limit:
            return
        if rest == "":
            try:
                st = os.stat(base)
            except OSError:
                return
            found.setdefault((st.st_dev, st.st_ino), os.path.realpath(base))
            return
        try:
            children = os.listdir(base)
        except OSError:
            return
        if len(children) > _DECODE_MAX_CHILDREN:
            return
        for child in children:
            encoded = _encode_segment(child)
            if not encoded:
                continue
            nxt = os.path.join(base, child)
            if rest == encoded:
                walk(nxt, "")
            elif rest.startswith(encoded + "-"):
                walk(nxt, rest[len(encoded) + 1:])

    walk(root, dir_name.lstrip("-"))
    return sorted(found.values())


def canonical_project_id(dir_name: str) -> str | None:
    """The scoping identity for a ``~/.claude/projects/<dir_name>`` directory,
    computed the SAME way :func:`resolve_project_id` computes it at retrieval
    time -- decode to a real path, unwind any linked worktree, re-encode.

    Returns ``DEUS_PROJECT_ID`` when the decoded path IS this repo -- checked
    against the RESOLVED path, never against the projects-directory basename,
    which is a different directory entirely.

    Returns ``None`` when the directory cannot be resolved to exactly one real
    path (its repo was moved or deleted, or the name is genuinely ambiguous).
    Callers must NOT substitute ``NULL`` for that: ``NULL`` is the GLOBAL tier,
    so an unresolved project's memory would compete in every other project's
    retrieval. Quarantine under the raw ``dir_name`` instead -- invisible to
    every live scope, still reachable by asking for that project explicitly,
    and reported rather than silent.
    """
    candidates = decode_project_dir_name(dir_name)
    if len(candidates) != 1:
        return None
    resolved = Path(candidates[0])
    if is_this_repo(resolved):
        return DEUS_PROJECT_ID
    return _encode_project_dir(_unwind_worktree(resolved).as_posix())


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
    # LIA-125: identity by inode, not by resolved-path string -- a case-only
    # spelling difference (~/deus vs ~/Deus) is the SAME directory here and
    # must not produce two identities. is_this_repo() owns that comparison so
    # index-time and retrieval-time can never drift apart on it.
    if is_this_repo(root):
        return DEUS_PROJECT_ID
    return _encode_project_dir(root.as_posix())
