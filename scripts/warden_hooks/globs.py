"""Pathlib-style glob matching with version-independent ``full_match`` semantics.

Extracted verbatim from ``codex_warden_hooks.py`` (LIA-306). Pure leaf: depends
only on ``functools`` + ``re``, holds no shared module state, and is not
monkeypatched by any test (tests read ``hooks._glob_match`` /
``hooks._glob_to_regex`` as attributes, which the entry module re-exports).
"""

from __future__ import annotations

import functools
import re


@functools.lru_cache(maxsize=512)  # patterns come from a small static config; 512 never evicts in practice
def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """Compile a pathlib-style glob to a regex with ``full_match`` semantics on every Python.

    Version-INDEPENDENT by design: ``PurePath.full_match`` (3.13+) treats ``**`` as "zero or
    more path segments" but the pre-3.13 ``PurePath.match`` fallback does not, so a
    version-split implementation silently under-matched ``**`` globs on Python < 3.13. Arms:
    ``**/`` -> zero+ segments; ``**`` -> anything; ``*`` -> within a segment; ``?`` -> one
    char; ``[seq]``/``[!seq]`` -> char class (``!`` mapped to ``^``); unterminated ``[`` ->
    literal; everything else escaped. (Regression matrix: test_glob_match_full_match_semantics.)
    """
    i, n = 0, len(pattern)
    out: list[str] = []
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern[i:i + 2] == "**":
                i += 2
                if i < n and pattern[i] == "/":
                    out.append("(?:[^/]*/)*")  # ** + / -> zero or more whole segments
                    i += 1
                else:
                    out.append(".*")            # trailing/bare ** -> anything incl. '/'
            else:
                out.append("[^/]*")             # * -> within a segment (never crosses '/')
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = i + 1
            if j < n and pattern[j] == "!":       # ONLY '!' negates (glob/pathlib convention)
                j += 1
            if j < n and pattern[j] == "]":      # a literal ']' as the first class member
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:                            # unterminated '[' -> literal
                out.append(r"\[")
                i += 1
            else:
                body = pattern[i + 1:j]
                neg = body.startswith("!")
                if neg:
                    body = body[1:]
                # Escape for a regex char class: '\' and ']' always; a LEADING '^' is a glob
                # literal (only '!' negates) so escape it lest regex read it as negation. Ranges
                # like 'a-z' pass through unchanged.
                body = body.replace("\\", "\\\\").replace("]", r"\]")
                if body.startswith("^"):
                    body = "\\" + body
                out.append("[" + ("^" if neg else "") + body + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    # No DOTALL: matched inputs are file paths (an ``as_posix()``), which never contain a
    # newline, so the ``.*`` emitted for ``**`` has nothing to span — the flag was inert.
    return re.compile("".join(out) + r"\Z")


def _glob_match(rel_posix: str, pattern: str) -> bool:
    # Inputs are always a FILE's ``as_posix()`` (no trailing slash); rstrip defensively so a
    # trailing '/' can't diverge from pathlib's path normalization.
    return _glob_to_regex(pattern).match(rel_posix.rstrip("/")) is not None
