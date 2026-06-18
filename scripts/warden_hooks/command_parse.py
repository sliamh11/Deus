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


def _extract_pr_ref(command: str) -> str | None:
    """Return the PR number, URL, or branch from a ``gh pr merge`` command.

    Scans past flags so ``gh pr merge --squash 294`` is handled correctly.
    """
    _FLAGS_WITH_VALUE = frozenset({
        "-R", "--repo", "-t", "--subject-body",
        "--match-head-commit", "--author",
        "-b", "--body", "-F", "--body-file", "-A", "--author-email",
    })
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
