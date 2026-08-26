#!/usr/bin/env python3
"""Deus Memory MCP Server (stdio transport).

Exposes the memory recall pipeline as a single MCP tool so any agent that can
register an MCP server (Claude Code, Cursor, Windsurf, etc.) gets the same
retrieval quality as the host hook — closing the cross-interface parity gap.

Platform: Linux/macOS only (depends on sqlite_vec C extension + Ollama).

Usage:
    scripts/deus-memory-mcp   # stdio; selects a Python env with mcp installed

Register with Codex:
    codex mcp add deus-memory -- /path/to/deus/scripts/deus-memory-mcp

Register in ~/.claude/settings.json:
    {
      "mcpServers": {
        "deus-memory": {
          "command": "/path/to/deus/scripts/deus-memory-mcp",
          "args": [],
          "env": {}
        }
      }
    }
"""
from __future__ import annotations

import os
import sys

if sys.platform == "win32":
    print(
        "memory_mcp_server.py requires Linux or macOS (sqlite_vec + Ollama).",
        file=sys.stderr,
    )
    sys.exit(1)

from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import memory_query  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP

    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False


def _int_env(name: str, default: int) -> int:
    """Positive-int env override with a safe fallback (invalid/<=0 -> default)."""
    try:
        v = int(os.environ.get(name, ""))  # LIA-344
    except ValueError:
        return default
    return v if v > 0 else default


# LIA-344: bound the MCP recall payload server-side (the host hook caps at 4096,
# but the MCP path returned recall()'s context uncapped with no k ceiling).
# 8192 chars (~2k tok) exceeds the largest real procedure node (2,962) and a
# typical k=3 body (~5 KB) while bounding the k=10 worst case (~57 KB). Both
# env-overridable starting values, tunable post-ship on real MCP traffic.
_MAX_CONTEXT_CHARS = _int_env("DEUS_MCP_RECALL_MAX_CHARS", 8192)  # LIA-344
_K_MAX = _int_env("DEUS_MCP_RECALL_MAX_K", 10)  # LIA-344


_ALL_PROJECTS = "all"


def _resolve_scope(project: str | None) -> tuple[str | None, str]:
    """Resolve the retrieval scope, and a human-readable label for it.

    Returns ``(scope_or_None, label)``. A ``None`` scope means "search
    everything", which is correct for an explicit ``project="all"`` and is a
    KNOWN GAP otherwise -- the label says which, so an indeterminate scope can
    never be mistaken for a deliberate one.
    """
    import os
    from pathlib import Path

    import auto_memory_dir as amd

    if project:
        if project.strip().lower() == _ALL_PROJECTS:
            return None, "ALL projects (explicitly requested)"
        # A path gets normalised through the same worktree-unwinding pipeline
        # retrieval uses; anything else is taken as an id/dirname verbatim, so
        # a QUARANTINED project stays reachable by its raw name.
        candidate = Path(project).expanduser()
        if candidate.is_dir():
            resolved = amd._encode_project_dir(
                amd._unwind_worktree(candidate).as_posix()
            )
            if amd.is_this_repo(candidate):
                resolved = amd.DEUS_PROJECT_ID
            return resolved, resolved
        return project, project

    def _id_for(root: Path) -> str:
        """Identity for a resolved root, worktree-unwound, this-repo aware.

        Computed DIRECTLY from `root`. Routing through
        `resolve_project_id()` here would be a precedence bug: that function
        reads `CLAUDE_PROJECT_DIR`, so when both variables are set,
        `DEUS_PROJECT_ROOT` would be reported as the source while the OTHER
        project's memory was actually returned -- a wrong scope wearing a
        correct-looking label.
        """
        if amd.is_this_repo(root):
            return amd.DEUS_PROJECT_ID
        return amd._encode_project_dir(amd._unwind_worktree(root).as_posix())

    for env_var in ("DEUS_PROJECT_ROOT", "CLAUDE_PROJECT_DIR"):
        raw = os.environ.get(env_var)
        if not raw:
            continue
        root = Path(raw).expanduser()
        if root.is_dir():
            resolved = _id_for(root)
            return resolved, f"{resolved} (from {env_var})"

    cwd = Path.cwd()
    if amd._git_output(["rev-parse", "--show-toplevel"], cwd):
        resolved = _id_for(cwd)
        return resolved, f"{resolved} (from server cwd)"

    return None, (
        "unscoped - project indeterminate. Pass project=<repo path or id> "
        "for scoped results, or project='all' to search every project on purpose."
    )


def memory_recall(
    query: str, k: int = 3, source: str = "mcp", project: str | None = None
) -> dict:
    """Retrieve memory context for a query.

    Wraps ``memory_query.recall()`` so any MCP-capable agent gets the same
    retrieval quality as the Deus host hook.

    Args:
        query:   Natural-language query (e.g. "what is Liam's timezone?").
        k:       Number of top results to return (LIA-344: clamped server-side
                 to 1.._K_MAX, default ceiling 10).
        source:  Identifier written to the retrieval log (default ``"mcp"``).
        project: Which project's memory to search, alongside the global tier.
                 A repo path, a project id, a ``~/.claude/projects`` dirname, or
                 ``"all"`` to search every project deliberately. **Pass this
                 explicitly** -- you know your working directory even when the
                 environment does not carry it.

    Scope resolution, in order: ``project`` -> ``DEUS_PROJECT_ROOT`` ->
    ``CLAUDE_PROJECT_DIR`` -> this server process's cwd if it is inside a git
    repo -> indeterminate. LIA-123: a bare
    ``auto_memory_dir.resolve_project_id()`` default is NOT enough, because it
    reads only ``CLAUDE_PROJECT_DIR`` and this server is registered as a plain
    stdio command with an empty env and no pinned cwd -- a Codex or other
    non-Claude client need never set that variable, so the default would
    silently collapse to unscoped and pull every project's memory into every
    answer.

    When the scope cannot be determined the results are still returned, but the
    ``scope`` field says so explicitly rather than quietly reading as scoped.

    The formatted context is capped server-side to ``_MAX_CONTEXT_CHARS``
    (LIA-344) so a caller cannot pull an unbounded payload.

    Returns:
        ``{"context": str, "paths": [str], "confidence": float,
        "fell_back": bool, "scope": str}``
    """
    # Procedures recall by default on the MCP path (the broad external recall
    # surface). Kill-switch is an explicit DEUS_PROCEDURE_MEMORY=0; any other value
    # (incl. unset) keeps them eligible via {"standard"} (None falls through to
    # recall()'s default which ALSO drops procedures). Intentionally diverges from
    # the default-off host hook — see docs/decisions/procedure-memory-default-on.md.
    proc_disabled = os.environ.get("DEUS_PROCEDURE_MEMORY", "").strip() == "0"
    exclude_kinds = None if proc_disabled else {"standard"}
    # LIA-344: clamp k and cap the formatted context so an MCP caller cannot pull
    # an unbounded payload (the sentinel framing survives — see _truncate_body).
    k = min(max(1, k), _K_MAX)

    # LIA-122/123: scoping is gated on the SAME flag as the host hook
    # (memory_retrieval_hook.py), so both surfaces flip together.
    #
    # Scoping this path unconditionally while the hook stays opt-in was a real
    # defect, caught by a peer session: with the live store still holding 23
    # `deus`-tagged nodes, a cyber-olympians session asking through MCP would
    # scope to that project and filter out all 21 host-global procedures --
    # exactly the regression the "retag first, flag second" ordering exists to
    # prevent, reintroduced on the surface an agent reaches for most.
    #
    # An EXPLICIT `project=` argument still scopes regardless of the flag: the
    # caller asked for a specific project, and that is also the escape hatch
    # that keeps a quarantined project reachable by name.
    # TODO(LIA-122 P4): this parity with the host hook is a TEMPORARY trade, not
    # a permanent architecture choice. MCP is the broader surface -- any external
    # MCP-capable client can register against it, whereas the hook runs inside
    # one known session and cwd -- so once write-side project tagging ships it
    # should re-diverge toward scoped-BY-DEFAULT rather than following the hook.
    # Today scoping ON would drop the 21 host-global procedure nodes, which is
    # the regression this gating exists to fix, so parity is the correct
    # stopgap. Revisit when the live retag lands; do not let it calcify.
    _scoping_on = os.environ.get("DEUS_PROJECT_SCOPE", "").strip() == "1"
    if project or _scoping_on:
        scope, scope_label = _resolve_scope(project)
    else:
        scope, scope_label = None, (
            "unscoped - DEUS_PROJECT_SCOPE is off, matching the host hook. "
            "Pass project=<repo path or id> to scope this query anyway."
        )

    if scope is None and _scoping_on and (project or "").strip().lower() != _ALL_PROJECTS:
        # FAIL CLOSED. An indeterminate scope must not silently become "search
        # every project": this server is registered as a plain stdio command
        # with an empty env and no pinned cwd, so a client that sets neither
        # DEUS_PROJECT_ROOT nor CLAUDE_PROJECT_DIR would otherwise pull every
        # project's memory into every answer -- exactly the cross-project
        # disclosure the project tagging exists to prevent. Searching
        # everything must be an explicit `project="all"`, never a default that
        # falls out of a missing environment variable.
        return {
            "context": "",
            "paths": [],
            "confidence": 0.0,
            "fell_back": True,
            "scope": scope_label,
            "error": (
                "Project scope could not be determined, so no search was run "
                "(refusing to search every project by default). Pass "
                "project=<repo path or project id>, or project='all' to search "
                "all projects deliberately."
            ),
        }

    result = memory_query.recall(
        query,
        k=k,
        source=source,
        exclude_kinds=exclude_kinds,
        max_context_chars=_MAX_CONTEXT_CHARS,
        project_scope=scope,
    )
    result["scope"] = scope_label
    return result


def _run_mcp_server() -> None:
    """Start the FastMCP stdio server."""
    if not _MCP_AVAILABLE:
        print(
            "ERROR: mcp package not installed. Run: pip install mcp",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp = FastMCP("deus-memory")
    mcp.tool()(memory_recall)
    mcp.run()


if __name__ == "__main__":
    _run_mcp_server()
