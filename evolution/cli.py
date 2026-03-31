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


def cmd_status(group_folder: Optional[str] = None, domain: Optional[str] = None, compare: bool = False) -> None:
    from .ilog.interaction_log import get_recent, score_trend
    from .optimizer.artifacts import list_artifacts
    from .db import open_db

    # Score trend
    trend = score_trend(group_folder=group_folder, days=30, domain=domain)
    header = "Score Trend (last 30 days)"
    if domain:
        header += f" — domain: {domain}"
    print(f"\n=== {header} ===")
    if trend:
        for row in trend[-10:]:
            bar = "█" * int(row["avg_score"] * 20)
            print(f"  {row['day']}  {bar:<20}  {row['avg_score']:.3f}  ({row['count']} interactions)")
    else:
        print("  No scored interactions yet.")

    # Compare: with-preset vs without-preset scores
    if compare and domain:
        db = open_db()
        with_preset = db.execute(
            "SELECT AVG(judge_score) AS avg, COUNT(*) AS n FROM interactions "
            "WHERE judge_score IS NOT NULL AND domain_presets LIKE ?",
            (f'%"{domain}"%',),
        ).fetchone()
        without_preset = db.execute(
            "SELECT AVG(judge_score) AS avg, COUNT(*) AS n FROM interactions "
            "WHERE judge_score IS NOT NULL AND (domain_presets IS NULL OR domain_presets NOT LIKE ?)",
            (f'%"{domain}"%',),
        ).fetchone()
        db.close()

        print(f"\n=== Comparison: {domain} ===")
        w_avg = with_preset["avg"] or 0
        w_n = with_preset["n"] or 0
        wo_avg = without_preset["avg"] or 0
        wo_n = without_preset["n"] or 0
        print(f"  With preset:    {w_avg:.3f}  (n={w_n})")
        print(f"  Without preset: {wo_avg:.3f}  (n={wo_n})")
        if w_n > 0 and wo_n > 0:
            delta = w_avg - wo_avg
            print(f"  Delta: {'+'if delta>=0 else ''}{delta:.3f}")

    # Reflection count
    db = open_db()
    total_refs = db.execute("SELECT COUNT(*) FROM reflections").fetchone()[0]
    helpful_refs = db.execute(
        "SELECT COUNT(*) FROM reflections WHERE times_helpful > 0"
    ).fetchone()[0]
    # Per-category breakdown
    cat_rows = db.execute(
        "SELECT category, COUNT(*) AS n FROM reflections GROUP BY category ORDER BY n DESC"
    ).fetchall()
    db.close()
    print(f"\n=== Reflections ===")
    print(f"  Total: {total_refs} | Helpful: {helpful_refs}")
    if cat_rows:
        cats = ", ".join(f"{r['category']}={r['n']}" for r in cat_rows)
        print(f"  By category: {cats}")

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
    from .config import REFLECTION_THRESHOLD, POSITIVE_THRESHOLD

    domain_presets = params.get("domain_presets") or None
    user_signal = params.get("user_signal") or None

    iid = log_interaction(
        prompt=params.get("prompt", ""),
        response=params.get("response"),
        group_folder=params.get("group_folder", "unknown"),
        latency_ms=params.get("latency_ms"),
        tools_used=params.get("tools_used"),
        session_id=params.get("session_id"),
        interaction_id=params.get("id"),
        domain_presets=domain_presets if isinstance(domain_presets, list) else None,
        user_signal=user_signal,
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
            elif result.score >= POSITIVE_THRESHOLD:
                from .reflexion.generator import generate_positive_reflection
                content, category = generate_positive_reflection(
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

            # User signal: generate a reflection for the *previous* interaction
            if user_signal and params.get("session_id"):
                from .ilog.interaction_log import get_previous_in_session
                prev = get_previous_in_session(params["session_id"], iid)
                if prev:
                    if user_signal == "positive":
                        from .reflexion.generator import generate_positive_reflection
                        content, category = generate_positive_reflection(
                            prompt=prev["prompt"],
                            response=prev.get("response") or "",
                            score=prev.get("judge_score") or 0.8,
                            rationale=f"User explicitly praised this response",
                        )
                    else:
                        content, category = generate_reflection(
                            prompt=prev["prompt"],
                            response=prev.get("response") or "",
                            score=prev.get("judge_score") or 0.4,
                            rationale=f"User explicitly rejected this response",
                        )
                    save_reflection(
                        content=content,
                        category=category,
                        score_at_gen=prev.get("judge_score") or 0.5,
                        interaction_id=prev["id"],
                        group_folder=prev.get("group_folder"),
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


def cmd_optimize(module: str = "all", domain: Optional[str] = None) -> None:
    from .optimizer.dspy_optimizer import optimize
    from .optimizer.modules import MODULE_REGISTRY

    modules = list(MODULE_REGISTRY.keys()) if module == "all" else [module]
    for m in modules:
        label = f"{m}:{domain}" if domain else m
        print(f"\nOptimizing module: {label}")
        aid = optimize(module=m, domain=domain)
        if aid:
            print(f"  Artifact saved: {aid}")
        else:
            print(f"  Skipped (insufficient samples or error)")


def cmd_principles(
    domain: Optional[str] = None,
    top_k: int = 5,
    min_new: int = 5,
    force: bool = False,
) -> None:
    from .reflexion.principles import extract_principles
    result = extract_principles(domain=domain, top_k=top_k, min_new=min_new, force=force)
    if result:
        print(result)
    else:
        print("Not enough scored interactions to extract principles.")


def cmd_serve() -> None:
    from .mcp_server import _run_mcp_server
    _run_mcp_server()


def main() -> None:
    parser = argparse.ArgumentParser(prog="evolution")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # status
    p_status = sub.add_parser("status", help="Show score trends and reflection stats")
    p_status.add_argument("--group", help="Filter by group folder")
    p_status.add_argument("--domain", help="Filter by domain preset (e.g., marketing)")
    p_status.add_argument("--compare", action="store_true", help="Compare with-preset vs without-preset scores")

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
    p_opt.add_argument("--domain", help="Optimize for a specific domain preset")

    # principles
    p_princ = sub.add_parser("principles", help="Extract top principles from scored interactions")
    p_princ.add_argument("--domain", help="Filter by domain preset")
    p_princ.add_argument("--top-k", type=int, default=5, help="Number of best/worst interactions to analyze")
    p_princ.add_argument("--min-new", type=int, default=5, help="Min new scored interactions to trigger extraction (default: 5)")
    p_princ.add_argument("--force", action="store_true", help="Bypass data-count check and extract immediately")

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
        cmd_status(group_folder=args.group, domain=args.domain, compare=args.compare)
    elif args.cmd == "get_reflections":
        cmd_get_reflections(args.query_json)
    elif args.cmd == "log_interaction":
        cmd_log_interaction(args.json_str)
    elif args.cmd == "reflect":
        cmd_reflect(args.interaction_id)
    elif args.cmd == "optimize":
        cmd_optimize(args.module, domain=args.domain)
    elif args.cmd == "principles":
        cmd_principles(domain=args.domain, top_k=args.top_k, min_new=args.min_new, force=args.force)
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
