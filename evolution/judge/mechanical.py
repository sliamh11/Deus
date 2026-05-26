"""
Mechanical tool-economy scorer for the evolution loop.

Detects wasteful tool-use patterns from the ordered sequence of tool calls
in a single turn. No LLM call required.

Rules:
  R1  Edit-without-Read:  Edit/Write on a path not preceded by Read on same path
  R2  Duplicate-Read:     Reading the same file_path more than once in a turn
  R4a Explore-no-recon:   Agent(Explore) called before any direct Read or Bash grep

Deferred:
  R3  Search-after-grep:  reserved for when a semantic search tool is added
  R4b Full unnecessary-subagent: needs LLM complexity assessment
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
