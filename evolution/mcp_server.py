"""
Deus Evolution MCP Server (stdio transport).

Exposes the evolution loop as MCP tools so any agent that can register an MCP
server (Claude Code, OpenClaw, NemoClaw) can log interactions and retrieve
reflections without Python knowledge.

Usage:
    python -m evolution.mcp_server   # stdio (for Claude Code settings.json)
    python evolution/mcp_server.py   # direct

Register in ~/.claude/settings.json:
    {
      "mcpServers": {
        "evolution": {
          "command": "python3",
          "args": ["/path/to/deus/evolution/mcp_server.py"],
          "env": {}
        }
      }
    }
"""
import asyncio
import json
import logging
import sys
from typing import Optional

log = logging.getLogger(__name__)

try:
    from mcp.server.fastmcp import FastMCP
    _MCP_AVAILABLE = True
except ImportError:
    _MCP_AVAILABLE = False

from .judge import make_runtime_judge
from .ilog.interaction_log import log_interaction, update_score
from .reflexion.generator import generate_reflection
from .reflexion.retriever import format_reflections_block, get_reflections
from .reflexion.store import increment_helpful, save_reflection


def _run_mcp_server() -> None:
    """Start the FastMCP stdio server."""
    if not _MCP_AVAILABLE:
        print(
            "ERROR: mcp package not installed. Run: pip install mcp",
            file=sys.stderr,
        )
        sys.exit(1)

    mcp = FastMCP("deus-evolution")

    @mcp.tool()
    def log_interaction_tool(
        prompt: str,
        response: str,
        group_folder: str,
        latency_ms: Optional[float] = None,
        tools_used: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        interaction_id: Optional[str] = None,
        metrics: Optional[dict] = None,
    ) -> dict:
        """
        Log one agent interaction.  Triggers async judge evaluation.
        Returns the interaction ID for follow-up feedback.

        metrics: optional flat dict of task outcomes (e.g. tests_passed,
        breaks, confidence, warden_rounds). Values must be scalars or lists
        of scalars. Seen by reflection generation, never by the judge.
        """
        # Invalid metrics raise ValueError out of log_interaction on purpose:
        # MCP callers get a real error signal (unlike the CLI fire-and-forget
        # path, which drops bad metrics and keeps the interaction).
        iid = log_interaction(
            prompt=prompt,
            response=response,
            group_folder=group_folder,
            latency_ms=latency_ms,
            tools_used=tools_used,
            session_id=session_id,
            interaction_id=interaction_id,
            metrics=metrics,
        )
        # Fire-and-forget async judge eval
        asyncio.create_task(_async_judge_and_reflect(
            iid, prompt, response, tools_used, group_folder, metrics=metrics,
        ))
        return {"id": iid, "status": "logged"}

    @mcp.tool()
    def log_metrics_tool(
        interaction_id: str,
        metrics: dict,
        merge: bool = True,
    ) -> dict:
        """
        Attach metrics to an already-logged interaction (post-hoc path).
        With merge=True (default), new keys are merged over stored metrics;
        merge=False replaces the payload wholesale. Returns the final dict.
        """
        from .metrics import update_metrics
        try:
            final = update_metrics(interaction_id, metrics, merge=merge)
        except ValueError as exc:
            return {"status": "error", "error": str(exc)}
        return {"status": "ok", "metrics": final}

    @mcp.tool()
    def get_metrics_summary_tool(
        group_folder: Optional[str] = None,
        days: int = 30,
        key: Optional[str] = None,
    ) -> dict:
        """
        Summarize task metrics over the last N days, optionally filtered by
        group_folder and/or restricted to one metric key. Returns per-key
        numeric stats (count/mean/min/max/sum) or categorical value counts.
        """
        from .metrics import fetch_metrics_rows, summarize_metrics
        rows = fetch_metrics_rows(group_folder=group_folder, days=days)
        return summarize_metrics(rows, key=key)

    @mcp.tool()
    def get_reflections_tool(
        query: str,
        group_folder: Optional[str] = None,
        tools_planned: Optional[list[str]] = None,
        top_k: int = 3,
    ) -> str:
        """
        Retrieve relevant past lessons for the current query.
        Returns a <reflections>...</reflections> block or empty string.
        """
        refs = get_reflections(
            query=query,
            group_folder=group_folder,
            tools_planned=tools_planned,
            top_k=top_k,
        )
        return format_reflections_block(refs, group_folder=group_folder)

    @mcp.tool()
    def get_active_prompt_tool(module: str) -> Optional[str]:
        """
        Return the current DSPy-optimized prompt block for a module, sanitized and
        ready to inject (LIA-152): boundary-tagged, length-capped, and None unless
        there is a non-trivial learned instruction.
        module: qa | tool_selection | summarization
        Returns None if no safe optimized prompt exists yet.
        """
        # Route through the single sanitizing helper so the MCP surface can never
        # hand a caller raw, unbounded artifact content (the trust boundary).
        from .optimizer.artifacts import get_active_prompt_block
        block = get_active_prompt_block(module)
        return block["block"] if block else None

    @mcp.tool()
    def record_feedback_tool(interaction_id: str, positive: bool) -> dict:
        """
        Record user feedback (thumbs up/down) for an interaction.
        Positive feedback increments helpfulness score on retrieved reflections.
        """
        if positive:
            from .storage import get_storage
            store = get_storage()
            refs = store.get_reflections_for_interaction(interaction_id)
            for r in refs:
                increment_helpful(r["id"])
        return {"status": "recorded"}

    mcp.run()


async def _async_judge_and_reflect(
    interaction_id: str,
    prompt: str,
    response: str,
    tools_used: Optional[list[str]],
    group_folder: str,
    metrics: Optional[dict] = None,
) -> None:
    """Judge the interaction and generate reflections if score is low.

    metrics are passed to reflection generation only — the judge stays blind
    to them so self-reported numbers can't inflate scores (anti-gaming).
    """
    from .config import REFLECTION_THRESHOLD, MAX_REFLECTIONS_TO_GENERATE
    from .persona import digest_for_group
    try:
        judge = make_runtime_judge()
        result = await judge.a_evaluate(
            prompt=prompt,
            response=response,
            tools_used=tools_used,
            user_profile=digest_for_group(group_folder),
        )
        dims = {
            "quality": result.quality,
            "safety": result.safety,
            "tool_use": result.tool_use,
            "personalization": result.personalization,
        }
        update_score(interaction_id, result.score, dims, schema_version=result.schema_version)

        if result.score < REFLECTION_THRESHOLD:
            generated_contents: set[str] = set()
            for _ in range(MAX_REFLECTIONS_TO_GENERATE):
                content, category = generate_reflection(
                    prompt=prompt,
                    response=response,
                    score=result.score,
                    dims=dims,
                    rationale=result.rationale,
                    tools_used=tools_used,
                    metrics=metrics,
                )
                if content in generated_contents:
                    break  # LLM returned identical text; stop early
                generated_contents.add(content)
                save_reflection(
                    content=content,
                    category=category,
                    score_at_gen=result.score,
                    interaction_id=interaction_id,
                    group_folder=group_folder,
                )
    except Exception as exc:
        log.error(
            'evolution: async judge failed for interaction %s — %s: %s',
            interaction_id, type(exc).__name__, exc,
            exc_info=True,
        )


if __name__ == "__main__":
    _run_mcp_server()
