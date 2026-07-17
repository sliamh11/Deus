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
