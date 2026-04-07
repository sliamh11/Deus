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
    from .storage import get_storage

    store = get_storage()

    # Score trend
    trend = score_trend(group_folder=group_folder, days=30, domain=domain)
    header = "Score Trend (last 30 days)"
    if domain:
        header += f" — domain: {domain}"
    print(f"\n=== {header} ===")
    if trend:
        for row in trend[-10:]:
            bar = "��" * int(row["avg_score"] * 20)
            print(f"  {row['day']}  {bar:<20}  {row['avg_score']:.3f}  ({row['count']} interactions)")
    else:
        print("  No scored interactions yet.")

    # Compare: with-preset vs without-preset scores
    if compare and domain:
        comp = store.domain_comparison(domain)
        print(f"\n=== Comparison: {domain} ===")
        print(f"  With preset:    {comp['with_avg']:.3f}  (n={comp['with_n']})")
        print(f"  Without preset: {comp['without_avg']:.3f}  (n={comp['without_n']})")
        if comp['with_n'] > 0 and comp['without_n'] > 0:
            delta = comp['with_avg'] - comp['without_avg']
            print(f"  Delta: {'+'if delta>=0 else ''}{delta:.3f}")

    # Reflection count
    total_refs = store.count_reflections()
    helpful_refs = store.count_helpful_reflections()
    cat_rows = store.reflections_by_category()
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
    ref_ids = [r["id"] for r in refs]
    print(json.dumps({"reflections_block": block, "count": len(refs), "reflection_ids": ref_ids}))


def _maybe_auto_extract_principles(domain_presets: Optional[list] = None) -> None:
    """Auto-trigger principles extraction if enough new data exists."""
    from .config import PRINCIPLES_COOLDOWN_HOURS
    from .reflexion.principles import extract_principles
    from .storage import get_storage
    from datetime import datetime, timezone, timedelta

    store = get_storage()
    domains_to_check = list(domain_presets or []) + [None]  # domain-specific + cross-domain
    for domain in domains_to_check:
        domain_key = domain or "cross-domain"
        # Cooldown check
        last = store.get_last_extraction(domain_key)
        if last:
            last_dt = datetime.fromisoformat(last["extracted_at"])
            if datetime.now(timezone.utc) - last_dt < timedelta(hours=PRINCIPLES_COOLDOWN_HOURS):
                continue
        # Data-count check + extraction (extract_principles handles its own min_new gate)
        try:
            extract_principles(domain=domain)
        except Exception:
            pass  # Non-fatal — principles are supplementary


def _maybe_auto_optimize(domain_presets: Optional[list] = None) -> None:
    """Auto-trigger DSPy optimization if enough new scored interactions exist."""
    from .config import AUTO_OPTIMIZE_THRESHOLD
    if AUTO_OPTIMIZE_THRESHOLD <= 0:
        return

    from .storage import get_storage

    store = get_storage()
    last_ts = store.get_latest_artifact_timestamp() or "1970-01-01"
    scored_since = store.count_scored_since(last_ts)

    if scored_since < AUTO_OPTIMIZE_THRESHOLD:
        return

    try:
        from .optimizer.dspy_optimizer import optimize
        optimize(module="qa")
        # Domain-specific optimization if enough domain data
        for domain in (domain_presets or []):
            optimize(module="qa", domain=domain)
    except Exception:
        pass  # Non-fatal


def _maybe_batch_judge(domain_presets: Optional[list] = None) -> None:
    """Check if enough unjudged interactions exist to trigger a batch judge run."""
    from .config import JUDGE_BATCH_SIZE
    from .storage import get_storage

    store = get_storage()
    unjudged = store.get_unjudged_interactions(limit=JUDGE_BATCH_SIZE)
    if len(unjudged) < JUDGE_BATCH_SIZE:
        return

    from .maintenance import judge_pending_interactions
    judge_pending_interactions()

    # Auto-trigger: principles extraction (post-batch hook)
    _maybe_auto_extract_principles(domain_presets)
    # Auto-trigger: DSPy optimization
    _maybe_auto_optimize(domain_presets)


def cmd_log_interaction(json_str: str) -> None:
    """Fire-and-forget logging + deferred batch judge, called by Node.js host."""
    try:
        params = json.loads(json_str)
    except json.JSONDecodeError:
        print(json.dumps({"error": "Invalid JSON"}))
        return

    from .ilog.interaction_log import log_interaction

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

    # Batch judge: check if we've accumulated enough unjudged interactions
    try:
        _maybe_batch_judge(domain_presets)
    except Exception:
        pass  # Non-fatal — judging will catch up during maintenance

    # Post-interaction maintenance check (non-blocking, best-effort).
    try:
        from .maintenance import run_maintenance
        run_maintenance()
    except Exception:
        pass  # Non-fatal — maintenance is supplementary

    print(json.dumps({"id": iid, "status": "ok"}))


def cmd_reflect(interaction_id: str) -> None:
    """Manually trigger reflection generation for an interaction."""
    import asyncio
    from .storage import get_storage
    from .reflexion.generator import generate_reflection
    from .reflexion.store import save_reflection

    store = get_storage()
    row = store.get_interaction(interaction_id)

    if not row:
        print(f"Interaction {interaction_id} not found.")
        sys.exit(1)
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


def cmd_archive_reflections(days: int = 30, dry_run: bool = False) -> None:
    from .reflexion.store import archive_stale_reflections
    count = archive_stale_reflections(days=days, dry_run=dry_run)
    prefix = "[dry-run] Would archive" if dry_run else "Archived"
    print(f"{prefix} {count} stale reflections (threshold: {days} days)")


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

    # archive-reflections
    p_archive = sub.add_parser("archive-reflections", help="Archive stale reflections (soft-delete)")
    p_archive.add_argument("--days", type=int, default=30, help="Age threshold in days (default: 30)")
    p_archive.add_argument("--dry-run", action="store_true", help="Preview without archiving")

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

    # cc-backfill
    p_cc = sub.add_parser("cc-backfill", help="Backfill Claude Code host sessions into evolution loop")
    p_cc.add_argument("--sessions-dir", type=Path,
                      help="Path to ~/.claude/projects (default: auto-detected)")
    p_cc.add_argument("--project", type=str, default=None,
                      help="Filter by project name (e.g. 'deus')")
    p_cc.add_argument("--dry-run", action="store_true",
                      help="Preview pairs without writing to DB")
    p_cc.add_argument("--limit", type=int, default=None,
                      help="Process at most N pairs")
    p_cc.add_argument("--status", action="store_true",
                      help="Print CC backfill status and exit")
    p_cc.add_argument("--quiet", action="store_true")

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
    elif args.cmd == "archive-reflections":
        cmd_archive_reflections(days=args.days, dry_run=args.dry_run)
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
    elif args.cmd == "cc-backfill":
        from .cc_backfill import run_cc_backfill, print_status as cc_print_status, CC_SESSIONS_DIR
        if args.status:
            cc_print_status()
        else:
            sessions_dir = Path(args.sessions_dir) if args.sessions_dir else CC_SESSIONS_DIR
            run_cc_backfill(
                sessions_dir=sessions_dir,
                project_filter=args.project,
                dry_run=args.dry_run,
                limit=args.limit,
                verbose=not args.quiet,
            )


if __name__ == "__main__":
    main()
