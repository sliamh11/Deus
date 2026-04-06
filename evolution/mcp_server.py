"""
Deus Evolution MCP Server (stdio transport).

Exposes the evolution loop as MCP tools so any agent that can register an MCP
server (Claude Code, OpenClaw, NemoClaw) can log interactions and retrieve
reflections without Python knowledge.

Usage:
    python -m evolution mcp_server   # stdio (for Claude Code settings.json)
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
import sys
from typing import Optional

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
    ) -> dict:
        """
        Log one agent interaction.  Triggers async judge evaluation.
        Returns the interaction ID for follow-up feedback.
        """
        iid = log_interaction(
            prompt=prompt,
            response=response,
            group_folder=group_folder,
            latency_ms=latency_ms,
            tools_used=tools_used,
            session_id=session_id,
            interaction_id=interaction_id,
        )
        # Fire-and-forget async judge eval
        asyncio.create_task(_async_judge_and_reflect(
            iid, prompt, response, tools_used, group_folder
        ))
        return {"id": iid, "status": "logged"}

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
        return format_reflections_block(refs)

    @mcp.tool()
    def get_active_prompt_tool(module: str) -> Optional[str]:
        """
        Return the current DSPy-optimized prompt for a module.
        module: qa | tool_selection | summarization
        Returns None if no artifact exists yet.
        """
        from .optimizer.artifacts import get_active
        artifact = get_active(module)
        return artifact["content"] if artifact else None

    @mcp.tool()
    def record_feedback_tool(interaction_id: str, positive: bool) -> dict:
        """
        Record user feedback (thumbs up/down) for an interaction.
        Positive feedback increments helpfulness score on retrieved reflections.
        """
        if positive:
            # Bump helpful count on reflections that referenced this interaction
            from ..db import open_db
            db = open_db()
            refs = db.execute(
                "SELECT id FROM reflections WHERE interaction_id = ?",
                [interaction_id],
            ).fetchall()
            db.close()
            for r in refs:
                increment_helpful(r[0])
        return {"status": "recorded"}

    mcp.run()


async def _async_judge_and_reflect(
    interaction_id: str,
    prompt: str,
    response: str,
    tools_used: Optional[list[str]],
    group_folder: str,
) -> None:
    """Judge the interaction and generate a reflection if score is low."""
    from .config import REFLECTION_THRESHOLD
    try:
        judge = make_runtime_judge()
        result = await judge.a_evaluate(
            prompt=prompt,
            response=response,
            tools_used=tools_used,
        )
        dims = {
            "quality": result.quality,
            "safety": result.safety,
            "tool_use": result.tool_use,
            "personalization": result.personalization,
        }
        update_score(interaction_id, result.score, dims)

        if result.score < REFLECTION_THRESHOLD:
            content, category = generate_reflection(
                prompt=prompt,
                response=response,
                score=result.score,
                dims=dims,
                rationale=result.rationale,
                tools_used=tools_used,
            )
            save_reflection(
                content=content,
                category=category,
                score_at_gen=result.score,
                interaction_id=interaction_id,
                group_folder=group_folder,
            )
    except Exception as exc:
        import traceback
        traceback.print_exc(file=sys.stderr)


if __name__ == "__main__":
    _run_mcp_server()
