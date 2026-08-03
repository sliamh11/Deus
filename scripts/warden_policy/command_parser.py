"""Classify a terminal command as commit-shaped and, if so, whether it's a
supported form the guardrail can safely bind a tree-sha attestation to.

Design: ALLOWLIST, not a blocklist. Every token after the recognized global
options and the `commit` subcommand must be an explicitly recognized,
non-index-mutating flag (or the value of one) -- anything else (an unknown
flag, a bare pathspec, a shell operator token like `&&`/`;`/`|` that
`shlex.split` leaves as a literal token since it isn't a real shell parser)
is rejected. This is deliberately safer than trying to enumerate every
dangerous git-commit flag: new/obscure flags default to BLOCKED, not
silently allowed.

`-c core.hooksPath=<dir>` is required as a GLOBAL option (before `commit`)
and `--no-verify` is required among `commit`'s own options -- together they
suppress every commit-time git hook (pre-commit, prepare-commit-msg,
commit-msg), closing the TOCTOU gap where a hook could mutate the index or
message between this classifier's authorization and the actual commit
(found in this repo's own `.husky/pre-commit` -> `npx lint-staged`, and by
name for `prepare-commit-msg` in adversarial plan review -- `--no-verify`
alone does not suppress that one).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass

# Flags allowed after `commit` that never mutate which content is committed.
# `--` is git's standard "end of options" separator -- a no-op boundary marker some agents
# (and humans) include defensively even when not strictly required. It's always safe to
# recognize: it consumes nothing and mutates nothing, and anything meaningful appearing AFTER
# it (a real pathspec) still falls through to the same catch-all rejection as any other
# unrecognized token. Found live: a real Hermes agent produced this form unprompted.
_FLAG_NO_VALUE = {
    "--no-verify", "--amend", "--allow-empty", "--allow-empty-message",
    "--no-edit", "--signoff", "-s", "--quiet", "-q", "--verbose", "-v", "--",
}
_FLAG_TAKES_VALUE = {"-m", "--message", "--author"}

# Global options recognized BEFORE the `commit` subcommand.
_GLOBAL_FLAG_TAKES_VALUE = {"-C"}  # value is a path; -c is handled specially (must be hooksPath)


@dataclass(frozen=True)
class Classification:
    is_commit_shaped: bool
    supported: bool
    reason: str


_UNSUPPORTED_PREFIX = "unsupported commit form -- "
_REATTEST_SUFFIX = (
    " stage explicitly with `git add`, re-attest with `warden_attest.py issue`, "
    "then run a plain commit with both `--no-verify` and `-c core.hooksPath=<empty-dir>`."
)


def _not_commit_shaped() -> Classification:
    return Classification(is_commit_shaped=False, supported=False, reason="not a commit command")


def _blocked(reason: str) -> Classification:
    return Classification(is_commit_shaped=True, supported=False, reason=_UNSUPPORTED_PREFIX + reason + _REATTEST_SUFFIX)


def classify(command: str) -> Classification:
    if "commit" not in command:
        return _not_commit_shaped()

    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unparseable quoting. If it mentions "commit" at all, don't guess -- block.
        return _blocked("command could not be parsed (unbalanced quoting)")

    if not tokens or tokens[0] != "git":
        return _not_commit_shaped()

    i = 1
    saw_hooks_path_override = False
    while i < len(tokens):
        tok = tokens[i]
        if tok == "commit":
            i += 1
            break
        if tok in _GLOBAL_FLAG_TAKES_VALUE:
            i += 2
            continue
        if tok == "-c":
            if i + 1 >= len(tokens):
                return _blocked("`-c` with no value")
            key_value = tokens[i + 1]
            if key_value.split("=", 1)[0] == "core.hooksPath":
                saw_hooks_path_override = True
            i += 2
            continue
        # Any other token before `commit` (another subcommand, an unrecognized
        # global flag, `commit-tree`, etc.) means this isn't the plain commit
        # form we classify at all -- not commit-shaped, not our concern.
        return _not_commit_shaped()
    else:
        # Ran off the end without ever seeing `commit` as the subcommand.
        return _not_commit_shaped()

    if not saw_hooks_path_override:
        return _blocked(
            "missing required global option `-c core.hooksPath=<empty-dir>` "
            "(must come BEFORE `commit` -- `git -c core.hooksPath=<dir> commit ...`)."
        )

    saw_no_verify = False
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--no-verify":
            saw_no_verify = True
            i += 1
            continue
        if tok in _FLAG_NO_VALUE:
            i += 1
            continue
        if tok in _FLAG_TAKES_VALUE:
            if i + 1 >= len(tokens):
                return _blocked(f"`{tok}` with no value")
            i += 2
            continue
        if "=" in tok and tok.split("=", 1)[0] in (_FLAG_TAKES_VALUE | {"--message", "--author"}):
            i += 1
            continue
        return _blocked(f"unrecognized or index-mutating commit option `{tok}`")

    if not saw_no_verify:
        return _blocked("missing required `--no-verify`.")

    return Classification(is_commit_shaped=True, supported=True, reason="supported commit form")
