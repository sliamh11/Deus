"""Shell-command / ``gh`` parsing helpers used by the admin-merge gate.

Extracted verbatim from ``codex_warden_hooks.py`` (LIA-306). Pure leaf: depends
only on ``hashlib`` / ``shlex`` / ``os`` / ``pathlib``, holds no shared module
state, and none of these symbols are monkeypatched by any test. ``_shell_tokens``
reads ``os.name``; the entry module re-exports these names and ``os`` is the same
module object across the split, so the existing ``hooks.os`` monkeypatch in tests
still applies.
"""

from __future__ import annotations

import hashlib
import os
import shlex
from pathlib import Path, PureWindowsPath


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _shell_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return command.split()


def _gh_command_index_after_global_flags(tokens: list[str], gh_index: int) -> int:
    index = gh_index + 1
    flags_with_values = {
        "--config-dir",
        "--hostname",
        "--repo",
        "-R",
    }

    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            return index + 1
        if not token.startswith("-"):
            return index
        if token in flags_with_values and index + 1 < len(tokens):
            index += 2
        else:
            index += 1
    return index


def _is_gh_executable(token: str) -> bool:
    token = token.strip("\"'")
    names = {Path(token).name.lower(), PureWindowsPath(token).name.lower()}
    return bool(names & {"gh", "gh.exe"})


def _is_admin_merge_command(command: str) -> bool:
    tokens = _shell_tokens(command)
    if not any(token == "--admin" or token.startswith("--admin=") for token in tokens):
        return False

    for index, token in enumerate(tokens):
        if not _is_gh_executable(token):
            continue
        command_index = _gh_command_index_after_global_flags(tokens, index)
        if tokens[command_index : command_index + 2] == ["pr", "merge"]:
            return True
    return False


#: git global flags that take a value, checked before the subcommand verb.
#: Known git 2.x global flags -- not exhaustive of every future addition; an
#: unrecognized flag causes `_parse_git_invocation` to fail to identify the
#: verb position for that one invocation (same fail-mode as the old
#: `GIT_COMMIT_RE` regex on any exotic flag it didn't anticipate either -- not
#: a regression). Extend if a gap surfaces, per feedback_no_speculative_hardening.
#: Deliberately NOT gh's flag set (`-R`/`--repo`/`--hostname` are wrong here).
_GIT_GLOBAL_FLAGS_WITH_VALUE = (
    "-C", "-c", "--exec-path", "--html-path", "--man-path", "--info-path",
    "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env",
)
_GIT_GLOBAL_FLAGS_BARE = (
    "-p", "--paginate", "--no-pager", "--no-replace-objects", "--bare",
    "--no-lazy-fetch", "--no-optional-locks", "--literal-pathspecs",
    "--glob-pathspecs", "--noglob-pathspecs", "--icase-pathspecs",
)


def _is_git_executable(token: str) -> bool:
    token = token.strip("\"'")
    names = {Path(token).name.lower(), PureWindowsPath(token).name.lower()}
    return bool(names & {"git", "git.exe"})


def _split_shell_commands(command: str) -> list[str]:
    """Best-effort split of `command` into independent shell invocations on
    unquoted `;`, `&&`, `||`, `|`, `&`, and newlines (`\\n`/`\\r`).

    Hand-rolled quote-tracking state machine rather than `shlex` -- shlex
    treats newline as ordinary whitespace (no way to distinguish "there was a
    boundary" from "which character it was"), and this function needs
    newline to behave as a real separator (a `git commit` on its own line
    within a multi-line Bash tool call is a routine, non-adversarial shape,
    not just a decoy pattern) while still NEVER splitting inside a quoted
    string -- e.g. `git commit -m "line1\\nline2"` must stay ONE sub-command.

    Respects single/double quotes and a leading backslash escape. A
    backslash immediately followed by a newline is treated as a line
    continuation (both characters are dropped, no split, no literal chars
    added) matching real shell semantics; any other backslash-escaped
    character is copied through literally without being treated as an
    operator. NOT a full shell grammar -- subshells (`$(...)`, backticks),
    here-docs, and other advanced constructs are not specially handled. An
    imperfect split only ever affects trigger/widening correctness in the
    safe direction documented on `_resolve_commit_target` (in
    codex_warden_hooks.py) and the callers of this function -- never a
    security bypass by itself.
    """
    pieces: list[str] = []
    current: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        if quote is not None:
            current.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < n:
            nxt = command[i + 1]
            if nxt == "\n":
                i += 2
                continue
            if nxt == "\r" and i + 2 < n and command[i + 2] == "\n":
                i += 3
                continue
            current.append(ch)
            current.append(nxt)
            i += 2
            continue
        if ch in ("\n", "\r", ";"):
            pieces.append("".join(current))
            current = []
            i += 1
            continue
        if ch == "|":
            pieces.append("".join(current))
            current = []
            i += 2 if i + 1 < n and command[i + 1] == "|" else 1
            continue
        if ch == "&":
            pieces.append("".join(current))
            current = []
            i += 2 if i + 1 < n and command[i + 1] == "&" else 1
            continue
        current.append(ch)
        i += 1
    pieces.append("".join(current))
    return [p.strip() for p in pieces if p.strip()]


def _parse_git_invocation(sub_command: str, cwd: Path) -> tuple[bool, Path | None]:
    """Parse ONE already-isolated shell invocation (no unquoted `&&`/`;`/`|`
    in it -- see `_split_shell_commands`). Returns `(is_commit, dash_c_target)`.

    `dash_c_target` is None when this invocation has no `-C` of its own (the
    caller should use `cwd` unchanged in that case) or isn't a git invocation
    at all -- callers must check `is_commit` before using a None target as
    meaningful, since "not a commit" and "a commit with no -C" both yield
    `dash_c_target is None`. Composes multiple `-C` flags on the SAME
    invocation cumulatively left-to-right against `cwd`, matching git's real
    semantics (safe to do within one invocation since they all genuinely
    apply to the same process; a DIFFERENT invocation's `-C` is never
    consulted here -- that isolation is what `_split_shell_commands` exists
    to provide).
    """
    tokens = _shell_tokens(sub_command)
    for index, token in enumerate(tokens):
        if not _is_git_executable(token):
            continue
        i = index + 1
        target = cwd
        saw_c = False
        while i < len(tokens):
            tok = tokens[i]
            if tok in _GIT_GLOBAL_FLAGS_BARE:
                i += 1
                continue
            if tok == "-C" and i + 1 < len(tokens):
                # .expanduser(): a real shell tilde-expands an UNQUOTED `~/x`
                # before git ever sees the argument -- shlex does not, so
                # without this an unquoted `git -C ~/repo commit` (the common
                # real-world form) would be misresolved as the literal
                # relative path `cwd/~/repo` instead of the user's actual
                # home directory (code-reviewer finding). Known, accepted
                # incompleteness: a QUOTED `"~/repo"` is NOT shell-expanded by
                # a real shell (git would receive the literal string), but
                # `_shell_tokens` (shlex) already discards quote-boundary
                # information by the time we see plain string tokens, so this
                # can't distinguish quoted from unquoted post-hoc -- rare
                # form, low-severity divergence (best-effort widening only,
                # not a security boundary by itself; the ambiguity/fail-closed
                # checks elsewhere are the actual backstop).
                candidate = Path(tokens[i + 1]).expanduser()
                target = candidate if candidate.is_absolute() else (target / candidate)
                saw_c = True
                i += 2
                continue
            if any(tok == f or tok.startswith(f + "=") for f in _GIT_GLOBAL_FLAGS_WITH_VALUE):
                i += 1
                if "=" not in tok and i < len(tokens):
                    i += 1
                continue
            # Generic single-letter bare flag (e.g. -q, -s -- not real git
            # globals, but the superseded GIT_COMMIT_RE regex's own
            # `-[A-BD-Za-bd-z]` alternative matched any letter except C/c
            # generically, and this gate's fail-closed philosophy prefers
            # over-matching to under-matching -- see feedback_no_speculative_hardening
            # comment above _GIT_GLOBAL_FLAGS_WITH_VALUE for why this isn't an
            # enumerated allowlist instead).
            if len(tok) == 2 and tok[0] == "-" and tok[1].isalpha() and tok[1] not in ("C", "c"):
                i += 1
                continue
            break
        is_commit = i < len(tokens) and tokens[i] == "commit"
        return is_commit, (target.resolve(strict=False) if saw_c else None)
    return False, None


def _is_git_commit_command(command: str) -> bool:
    """TRIGGER predicate -- replaces the old `GIT_COMMIT_RE` regex. Broad on
    purpose: ANY sub-invocation identified as a commit triggers the caller to
    care (more scrutiny is always safe). Ambiguity/ordering across multiple
    commit sub-invocations doesn't matter here -- that's only relevant for
    the stricter `_resolve_commit_target` in codex_warden_hooks.py, which
    decides WHICH worktree's verdict to check, not WHETHER to check one."""
    placeholder_cwd = Path(".")
    return any(
        _parse_git_invocation(sub, placeholder_cwd)[0]
        for sub in _split_shell_commands(command)
    )


#: ``gh pr merge``/``gh pr view`` flags that consume the following token as
#: their value. Shared by ``_extract_pr_ref`` (to skip over them while hunting
#: for the PR ref) and ``_extract_repo_flag`` (to avoid misreading one of their
#: values as a repo flag).
_FLAGS_WITH_VALUE = frozenset({
    "-R", "--repo", "-t", "--subject-body",
    "--match-head-commit", "--author",
    "-b", "--body", "-F", "--body-file", "-A", "--author-email",
})


def _extract_pr_ref(command: str) -> str | None:
    """Return the PR number, URL, or branch from a ``gh pr merge`` command.

    Scans past flags so ``gh pr merge --squash 294`` is handled correctly.
    """
    tokens = _shell_tokens(command)
    for index, token in enumerate(tokens):
        if not _is_gh_executable(token):
            continue
        command_index = _gh_command_index_after_global_flags(tokens, index)
        if tokens[command_index : command_index + 2] != ["pr", "merge"]:
            continue
        i = command_index + 2
        while i < len(tokens):
            tok = tokens[i]
            if not tok.startswith("-"):
                return tok
            if "=" in tok:
                i += 1
                continue
            if tok in _FLAGS_WITH_VALUE:
                i += 2
                continue
            i += 1
        return None
    return None


def _extract_repo_flag(command: str) -> str | None:
    """Return an explicit ``-R``/``--repo`` value from a ``gh`` invocation, if any.

    ``gh`` accepts ``-R``/``--repo <[HOST/]OWNER/REPO>`` either as a global flag
    (before the subcommand, e.g. ``gh --repo o/r pr merge 294``) or as a
    subcommand-local flag (after it, e.g. ``gh pr merge --repo o/r 294`` — the
    shape production callers actually use). Scans every token after ``gh``
    uniformly, using ``_FLAGS_WITH_VALUE`` to skip the VALUE of every other
    flag so it is never misread as a repo flag. When ``--repo``/``-R`` appears
    more than once, the LAST occurrence wins, matching ``gh``'s own
    last-flag-wins precedence. Returns ``None`` when no explicit repo scope is
    given, so callers fall back to ``gh``'s own cwd-based git-remote
    resolution (today's unchanged default behaviour).
    """
    tokens = _shell_tokens(command)
    for index, token in enumerate(tokens):
        if not _is_gh_executable(token):
            continue
        found: str | None = None
        i = index + 1
        while i < len(tokens):
            tok = tokens[i]
            if tok in ("-R", "--repo"):
                if i + 1 < len(tokens):
                    found = tokens[i + 1]
                i += 2
                continue
            if tok.startswith("--repo="):
                found = tok.split("=", 1)[1]
                i += 1
                continue
            if tok.startswith("-R") and tok != "-R" and not tok.startswith("--"):
                # gh's short-flag attached form: `-Rowner/repo`.
                found = tok[2:]
                i += 1
                continue
            if tok in _FLAGS_WITH_VALUE and i + 1 < len(tokens):
                i += 2
                continue
            i += 1
        return found
    return None
