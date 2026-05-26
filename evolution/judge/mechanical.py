"""
Mechanical scorers for the evolution loop.

Two scorers, both pure functions over ordered tool call sequences. No LLM.

Tool-Economy rules:
  R1  Edit-without-Read:  Edit/Write on a path not preceded by Read on same path
  R2  Duplicate-Read:     Reading the same file_path more than once in a turn
  R4a Explore-no-recon:   Agent(Explore) called before any direct Read or Bash grep

Gate-Audit rules:
  G1  Mark-without-warden:    Bash marks a gate without a matching reviewer agent in the turn
  G2  Trivial-on-source-edit: TRIVIAL gate bypass in a turn that edits source files
"""
from __future__ import annotations


def _looks_like_search(command: str) -> bool:
    # Trailing space guards against lsblk, lsof, etc.
    cmd = command.lower()
    return any(kw in cmd for kw in ("grep ", "find ", "ls ", "rg ", "ag "))


def score_tool_economy(tool_calls: list[dict]) -> tuple[float, dict]:
    """
    Score tool economy for a single turn's ordered tool call sequence.

    Parameters
    ----------
    tool_calls : list of dicts, each with:
        "name"         : str  -- tool name (Read, Edit, Write, Bash, Agent, etc.)
        "file_path"    : str  (optional) -- for Read/Edit/Write
        "subagent_type": str  (optional) -- for Agent
        "command"      : str  (optional) -- for Bash

    Returns
    -------
    (score, diagnostics)
        score       : float in [0.0, 1.0]; 1.0 = no wasteful patterns
        diagnostics : dict with per-rule violation counts
    """
    if not tool_calls:
        return 1.0, {"violations": 0, "rules_fired": []}

    violations = 0
    rules_fired: list[str] = []
    read_paths: set[str] = set()
    has_direct_recon = False

    for i, tc in enumerate(tool_calls):
        name = tc.get("name", "")
        path = tc.get("file_path", "")

        if name == "Read":
            if path:
                if path in read_paths:
                    violations += 1
                    rules_fired.append("R2")
                else:
                    read_paths.add(path)
            has_direct_recon = True

        elif name in ("Edit", "Write"):
            if path and path not in read_paths:
                violations += 1
                rules_fired.append("R1")

        elif name == "Bash":
            cmd = tc.get("command", "")
            if _looks_like_search(cmd):
                has_direct_recon = True

        elif name == "Agent":
            st = tc.get("subagent_type", "")
            if st == "Explore" and not has_direct_recon:
                violations += 1
                rules_fired.append("R4a")

    score = max(0.0, 1.0 - violations * 0.15)

    diagnostics = {
        "violations": violations,
        "rules_fired": rules_fired,
        "edit_without_read": rules_fired.count("R1"),
        "duplicate_read": rules_fired.count("R2"),
        "explore_no_prior_recon": rules_fired.count("R4a"),
    }
    return score, diagnostics


# ---------------------------------------------------------------------------
# Gate-Audit scorer
# ---------------------------------------------------------------------------

_SOURCE_EXTENSIONS = frozenset({".py", ".ts", ".tsx", ".js", ".jsx", ".sh", ".rs"})

_GATE_TO_REVIEWER = {
    "plan-reviewed": "plan-reviewer",
    "code-reviewed": "code-reviewer",
}


def _is_source_file(path: str) -> bool:
    dot = path.rfind(".")
    return dot != -1 and path[dot:] in _SOURCE_EXTENSIONS


def _extract_gate_type(command: str) -> str | None:
    """Parse which gate is being marked from a warden mark command."""
    cmd = command.lower()
    if "warden" not in cmd or "mark" not in cmd:
        return None
    for gate in _GATE_TO_REVIEWER:
        if gate in cmd:
            return gate
    return "other"


def score_gate_audit(tool_calls: list[dict]) -> tuple[float, dict]:
    """
    Score gate/warden compliance for a single turn's tool call sequence.

    Detects self-marking (marking gates without the matching warden agent
    anywhere in the same turn) and TRIVIAL bypass abuse on turns that
    edit source files.

    Returns (score, diagnostics) with score in [0.0, 1.0].
    """
    if not tool_calls:
        return 1.0, {"violations": 0, "rules_fired": []}

    penalty = 0.0
    rules_fired: list[str] = []

    seen_reviewers: set[str] = set()
    has_source_edits = False

    for tc in tool_calls:
        name = tc.get("name", "")
        if name == "Agent":
            st = tc.get("subagent_type", "")
            if st:
                seen_reviewers.add(st)
        elif name in ("Edit", "Write"):
            path = tc.get("file_path", "")
            if path and _is_source_file(path):
                has_source_edits = True

    for tc in tool_calls:
        if tc.get("name") != "Bash":
            continue
        cmd = tc.get("command", "")
        gate_type = _extract_gate_type(cmd)
        if gate_type is None or gate_type == "other":
            continue

        required_reviewer = _GATE_TO_REVIEWER.get(gate_type)
        if required_reviewer and required_reviewer not in seen_reviewers:
            penalty += 0.25
            rules_fired.append("G1")

        if "trivial" in cmd.lower() and has_source_edits:
            penalty += 0.15
            rules_fired.append("G2")

    score = max(0.0, 1.0 - penalty)

    diagnostics = {
        "violations": len(rules_fired),
        "rules_fired": rules_fired,
        "mark_without_warden": rules_fired.count("G1"),
        "trivial_on_source_edit": rules_fired.count("G2"),
    }
    return score, diagnostics
