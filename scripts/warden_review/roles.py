"""Warden ROLE specs: per-role rules file, the change-gatherer, and the Claude marker.

This is where per-role input differences live. Phase 2 wires only ``code-reviewer``
(reviews a git diff). Phase 3 adds ai-eng-warden (diff), plan-reviewer (the plan file),
and threat-modeler (plan/design text) by adding entries here — no engine change.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import cross_family_review as cfr


@dataclass(frozen=True)
class RoleSpec:
    role: str            # warden role name (matches the subagent type / config key)
    rules_path: str      # rules file, RELATIVE to repo root
    claude_marker: str   # the existing Claude verdict marker for this role
    gather: Callable[[str, str | None, str | None], str]  # (root, rev_range, diff_file) -> content
    is_diff: bool = True  # True: content is a unified diff (split per-file). False: review the
                          # whole content as one unit (e.g. plan-reviewer's plan text, which has
                          # no `diff --git` boundaries and would otherwise be dropped to "no files").


def _gather_diff(root: str, rev_range: str | None, diff_file: str | None) -> str:
    """Working-tree (or rev-range / file) diff — reuses the Stage-1 gatherer."""
    return cfr.get_diff(root, rev_range, diff_file)


def _gather_file(root: str, rev_range: str | None, diff_file: str | None) -> str:
    """Read review content verbatim from a file path (for non-diff roles, e.g. plan-reviewer).

    The path arrives in the ``diff_file`` slot (codex_warden.py routes ``--content-file`` there).
    Plain ``read_text`` — no shell, no diff parsing. Raises if the path is absent/unreadable so
    the driver surfaces a clear usage error rather than reviewing empty content. Role-agnostic:
    the message names no specific role so any future content-file role can reuse this gatherer."""
    if not diff_file:
        raise cfr.ReviewError(
            cfr.USAGE_ERROR,
            "this role requires --content-file <path> (no git diff to review)",
        )
    return Path(diff_file).read_text(encoding="utf-8")


ROLE_SPECS: dict[str, RoleSpec] = {
    "code-reviewer": RoleSpec(
        role="code-reviewer",
        rules_path=".claude/wardens/code-review-rules.md",
        claude_marker="code-reviewed",
        gather=_gather_diff,
    ),
    "ai-eng-warden": RoleSpec(
        role="ai-eng-warden",
        rules_path=".claude/wardens/ai-engineering-rules.md",
        claude_marker="ai-eng-reviewed",
        gather=_gather_diff,
    ),
    "plan-reviewer": RoleSpec(
        role="plan-reviewer",
        rules_path=".claude/wardens/plan-review-rules.md",
        claude_marker="plan-reviewed",
        gather=_gather_file,
        is_diff=False,  # reviews plan TEXT, not a diff — review it as one whole unit.
    ),
}
