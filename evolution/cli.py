#!/usr/bin/env python3
"""
Deus Evolution CLI.

Usage:
    python evolution/cli.py status [--group <folder>]
    python evolution/cli.py get_reflections <query_json>
    python evolution/cli.py log_interaction <json>
    python evolution/cli.py reflect <interaction_id>
    python evolution/cli.py optimize [--module qa|tool_selection|summarization|all]
    python evolution/cli.py serve
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Optional

# Allow running as a script (python evolution/cli.py) or module (-m evolution.cli)
if __name__ == "__main__" and __package__ is None:
    _project_root = str(Path(__file__).parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)
    __package__ = "evolution"  # type: ignore


def cmd_status(group_folder: Optional[str] = None) -> None:
    from .ilog.interaction_log import get_recent, score_trend
    from .optimizer.artifacts import list_artifacts
    from .db import open_db

    # Score trend
    trend = score_trend(group_folder=group_folder, days=30)
    print("\n=== Score Trend (last 30 days) ===")
    if trend:
        for row in trend[-10:]:
            bar = "█" * int(row["avg_score"] * 20)
            print(f"  {row['day']}  {bar:<20}  {row['avg_score']:.3f}  ({row['count']} interactions)")
    else:
        print("  No scored interactions yet.")

    # Reflection count
    db = open_db()
    total_refs = db.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
    helpful_refs = db.execute(
        "SELECT COUNT(*) FROM reflections WHERE times_helpful > 0"
    ).fetchone()[0]
    db.close()
    print(f"\n=== Reflections ===")
    print(f"  Total: {total_refs} | Helpful: {helpful_refs}")

    # Active artifacts
    artifacts = list_artifacts(limit=5)
    print("\n=== Active Prompt Artifacts ===")
    active = [a for a in artifacts if a["active"]]
    if active:
        for a in active:
            delta = (a["optimized_score"] or 0) - (a["baseline_score"] or 0)
            print(
                f"  [{a['module']}] {a['created_at'][:10]} "
                f"baseline={a['baseline_score']:.3f} "
                f"→ {a['optimized_score']:.3f} "
                f"({'+'if delta>=0 else ''}{delta:.3f}) "
                f"n={a['sample_count']}"
            )
    else:
        print("  No active artifacts. Run `optimize` to generate one.")
    print()


def cmd_get_reflections(query_json: str) -> None:
    """Used by Node.js host: reads JSON, writes reflection block to stdout."""
    try:
        params = json.loads(query_json)
    except json.JSONDecodeError:
        params = {"query": query_json}

    from .reflexion.retriever import format_reflections_block, get_reflections

    refs = get_reflections(
        query=params.get("query", ""),
        group_folder=params.get("group_folder"),
        tools_planned=params.get("tools_planned"),
        top_k=params.get("top_k", 3),
    )
    block = format_reflections_block(refs)
    print(json.dumps({"reflections_block": block, "count": len(refs)}))


def cmd_log_interaction(json_str: str) -> None:
    """Fire-and-forget logging + async judge eval called by Node.js host."""
    import asyncio
    try:
        params = json.loads(json_str)
    except json.JSONDecodeError:
        print(json.dumps({"error": "Invalid JSON"}))
        return

    from .ilog.interaction_log import log_interaction
    from .judge.gemini_judge import make_runtime_judge
    from .ilog.interaction_log import update_score
    from .reflexion.generator import generate_reflection
    from .reflexion.store import save_reflection
    from .config import REFLECTION_THRESHOLD

    iid = log_interaction(
        prompt=params.get("prompt", ""),
        response=params.get("response"),
        group_folder=params.get("group_folder", "unknown"),
        latency_ms=params.get("latency_ms"),
        tools_used=params.get("tools_used"),
        session_id=params.get("session_id"),
        interaction_id=params.get("id"),
    )

    async def _judge_and_reflect():
        try:
            judge = make_runtime_judge()
            result = await judge.a_evaluate(
                prompt=params.get("prompt", ""),
                response=params.get("response") or "",
                tools_used=params.get("tools_used"),
            )
            dims = {
                "quality": result.quality,
                "safety": result.safety,
                "tool_use": result.tool_use,
                "personalization": result.personalization,
            }
            update_score(iid, result.score, dims)

            if result.score < REFLECTION_THRESHOLD:
                content, category = generate_reflection(
                    prompt=params.get("prompt", ""),
                    response=params.get("response") or "",
                    score=result.score,
                    dims=dims,
                    rationale=result.rationale,
                    tools_used=params.get("tools_used"),
                )
                save_reflection(
                    content=content,
                    category=category,
                    score_at_gen=result.score,
                    interaction_id=iid,
                    group_folder=params.get("group_folder"),
                )
        except Exception as exc:
            import traceback
            traceback.print_exc(file=sys.stderr)

    asyncio.run(_judge_and_reflect())
    print(json.dumps({"id": iid, "status": "ok"}))


def cmd_reflect(interaction_id: str) -> None:
    """Manually trigger reflection generation for an interaction."""
    import asyncio
    from .db import open_db
    from .reflexion.generator import generate_reflection
    from .reflexion.store import save_reflection

    db = open_db()
    row = db.execute(
        "SELECT * FROM interactions WHERE id = ?", [interaction_id]
    ).fetchone()
    db.close()

    if not row:
        print(f"Interaction {interaction_id} not found.")
        sys.exit(1)

    row = dict(row)
    content, category = generate_reflection(
        prompt=row["prompt"],
        response=row["response"] or "",
        score=row.get("judge_score") or 0.5,
        rationale="manually triggered",
        tools_used=json.loads(row.get("tools_used") or "[]"),
    )
    rid = save_reflection(
        content=content,
        category=category,
        score_at_gen=row.get("judge_score") or 0.5,
        interaction_id=interaction_id,
        group_folder=row.get("group_folder"),
    )
    print(f"Reflection saved: {rid}")
    print(f"Category: {category}")
    print(f"\n{content}")


def cmd_optimize(module: str = "all") -> None:
    from .optimizer.dspy_optimizer import optimize
    from .optimizer.modules import MODULE_REGISTRY

    modules = list(MODULE_REGISTRY.keys()) if module == "all" else [module]
    for m in modules:
        print(f"\nOptimizing module: {m}")
        aid = optimize(module=m)
        if aid:
            print(f"  Artifact saved: {aid}")
        else:
            print(f"  Skipped (insufficient samples or error)")


def cmd_serve() -> None:
    from .mcp_server import _run_mcp_server
    _run_mcp_server()


def main() -> None:
    parser = argparse.ArgumentParser(prog="evolution")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # status
    p_status = sub.add_parser("status", help="Show score trends and reflection stats")
    p_status.add_argument("--group", help="Filter by group folder")

    # get_reflections
    p_refs = sub.add_parser("get_reflections", help="Retrieve relevant reflections (JSON)")
    p_refs.add_argument("query_json", help='JSON: {"query": "...", "group_folder": "...", ...}')

    # log_interaction
    p_log = sub.add_parser("log_interaction", help="Log an interaction and run judge")
    p_log.add_argument("json_str", help="JSON interaction payload")

    # reflect
    p_reflect = sub.add_parser("reflect", help="Manually generate reflection for interaction")
    p_reflect.add_argument("interaction_id")

    # optimize
    p_opt = sub.add_parser("optimize", help="Run DSPy optimizer")
    p_opt.add_argument("--module", default="all",
                       choices=["all", "qa", "tool_selection", "summarization"])

    # serve
    sub.add_parser("serve", help="Start MCP stdio server")

    # backfill
    p_backfill = sub.add_parser("backfill", help="Backfill historical sessions into evolution loop")
    p_backfill.add_argument("--sessions-dir", type=Path,
                            help="Path to data/sessions (default: auto-detected)")
    p_backfill.add_argument("--dry-run", action="store_true",
                            help="Preview pairs without writing to DB")
    p_backfill.add_argument("--limit", type=int, default=None,
                            help="Process at most N pairs")
    p_backfill.add_argument("--status", action="store_true",
                            help="Print backfill status and exit")
    p_backfill.add_argument("--quiet", action="store_true")

    args = parser.parse_args()

    if args.cmd == "status":
        cmd_status(group_folder=args.group)
    elif args.cmd == "get_reflections":
        cmd_get_reflections(args.query_json)
    elif args.cmd == "log_interaction":
        cmd_log_interaction(args.json_str)
    elif args.cmd == "reflect":
        cmd_reflect(args.interaction_id)
    elif args.cmd == "optimize":
        cmd_optimize(args.module)
    elif args.cmd == "serve":
        cmd_serve()
    elif args.cmd == "backfill":
        from .backfill import run_backfill, print_status, SESSIONS_DIR
        if args.status:
            print_status()
        else:
            sessions_dir = Path(args.sessions_dir) if args.sessions_dir else SESSIONS_DIR
            run_backfill(
                sessions_dir=sessions_dir,
                dry_run=args.dry_run,
                limit=args.limit,
                verbose=not args.quiet,
            )


if __name__ == "__main__":
    main()
