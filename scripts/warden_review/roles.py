"""Warden ROLE specs: per-role rules file, the change-gatherer, and the Claude marker.

This is where per-role input differences live. Phase 2 wires only ``code-reviewer``
(reviews a git diff). Phase 3 adds ai-eng-warden (diff), plan-reviewer (the plan file),
and threat-modeler (plan/design text) by adding entries here — no engine change.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import cross_family_review as cfr


@dataclass(frozen=True)
class RoleSpec:
    role: str            # warden role name (matches the subagent type / config key)
    rules_path: str      # rules file, RELATIVE to repo root
    claude_marker: str   # the existing Claude verdict marker for this role
    gather: Callable[[str, str | None, str | None], str]  # (root, rev_range, diff_file) -> content


def _gather_diff(root: str, rev_range: str | None, diff_file: str | None) -> str:
    """Working-tree (or rev-range / file) diff — reuses the Stage-1 gatherer."""
    return cfr.get_diff(root, rev_range, diff_file)


ROLE_SPECS: dict[str, RoleSpec] = {
    "code-reviewer": RoleSpec(
        role="code-reviewer",
        rules_path=".claude/wardens/code-review-rules.md",
        claude_marker="code-reviewed",
        gather=_gather_diff,
    ),
}
