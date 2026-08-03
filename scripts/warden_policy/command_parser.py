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

Defense-in-depth: `classify()` also rejects any command whose tokens contain a
shell command- or process-substitution marker (backtick, `$(`, `<(`, `>(`),
checked ONLY after the command is already confirmed to be a genuine,
otherwise-fully-supported git-commit invocation (never before -- an early
check would misclassify an unrelated command that merely mentions "commit"
as a substring, e.g. `rg -F '$(' docs/commit-notes.md`). Found live: Hermes's
`terminal` tool runs commands through a real persistent shell, so a
"supported" commit message like `-m "safe $(touch /tmp/x)"` would execute
arbitrary code as a side effect, independent of git or the tree-attestation
logic entirely -- verified by actually running it through a live Hermes
agent and confirming the target file was created. Process substitution
(`<(`/`>(`) is the same vulnerability class: bash forks and runs the enclosed
command to set up the substitution as a side effect of word-splitting alone,
whether or not the resulting path is ever read -- verified live the same way
(a brief delay is needed before checking, since the fork runs in the
background and can race ahead of a naive immediate check).

Before the marker check runs, the RAW command string is normalized to strip
shell line-continuations (a backslash immediately followed by a newline,
stripped by a real shell before word-splitting/quote-removal even runs,
regardless of quoting style) -- normalizing the raw string, not patching
individual tokens after tokenization, because a per-token fix was tried and
found insufficient: for a quoted value `shlex.split()` preserves the literal
backslash-newline sequence intact, but for an unquoted value `shlex`'s own
escape handling already consumes the backslash while leaving the bare
newline behind, so no backslash remains for a post-tokenization check to
match. Verified live in both forms. Accepted, named tradeoff: stripping the
raw string unconditionally also strips inside what would be a single-quoted
section (where a real shell does NOT remove line continuations at all) --
an over-blocking direction only, never under-blocking.

This is NOT a claim of exhaustive shell quote-removal or metacharacter
sanitization (that would be reinventing a shell parser and remains this
classifier's non-goal, same as the deliberately unaddressed shell-compound
and alternate-invocation gaps below) -- it closes the specific, tractable,
well-known constructs verified live across several adversarial review
rounds.
"""

from __future__ import annotations

import re
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

# Shell command-/process-substitution markers -- see the module docstring's "Defense-in-depth"
# paragraph for the full rationale (why these four, why checked last, why line continuations
# are normalized on the raw string rather than per-token).
_COMMAND_SUBSTITUTION_MARKERS = ("`", "$(", "<(", ">(")
_LINE_CONTINUATION_RE = re.compile(r"\\\r?\n")


def _strip_line_continuations(command: str) -> str:
    return _LINE_CONTINUATION_RE.sub("", command)


def _contains_command_substitution(tokens: list[str]) -> bool:
    return any(marker in tok for tok in tokens for marker in _COMMAND_SUBSTITUTION_MARKERS)


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
    # Normalize line continuations before any other processing -- see the module docstring.
    command = _strip_line_continuations(command)

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

    # Defense-in-depth, checked LAST -- see the module docstring.
    if _contains_command_substitution(tokens):
        return _blocked(
            "commit contains a shell command- or process-substitution marker "
            "(backtick, $(, <(, or >() in a value"
        )

    return Classification(is_commit_shaped=True, supported=True, reason="supported commit form")
