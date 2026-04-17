#!/usr/bin/env python3
import argparse
import sys
import time
from pathlib import Path

if __name__ == "__main__" and __package__ in (None, ""):
    _scripts_dir = str(Path(__file__).resolve().parent.parent)
    if _scripts_dir not in sys.path:
        sys.path.insert(0, _scripts_dir)
    __package__ = "bench"

from . import suites as _suites_pkg  # noqa: F401 — triggers suite registration
from .registry import get as get_suite, list_names
from .store import recent_runs, save_run
from .types import RunResult


def _fmt_row(r: dict) -> str:
    ts = r.get("ts", 0)
    score = r.get("score")
    score_s = f"{score:.3f}" if score is not None else "—"
    n = r.get("n_cases", 0)
    tok_in = r.get("tokens_in", 0)
    lat = r.get("latency_ms", 0)
    git = r.get("git_sha") or "—"
    suite = r.get("suite", "")
    return f"| {suite} | {ts} | {score_s} | {n} | {tok_in} | {lat} | {git} |"


def cmd_list(_args: argparse.Namespace) -> int:
    for name in list_names():
        print(name)
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    suite_name: str = args.suite
    suite_argv: list[str] = args.suite_args or []

    names = list_names()
    if suite_name == "all":
        targets = names
    else:
        if suite_name not in names:
            print(f"error: unknown suite {suite_name!r}; known: {', '.join(names) or '(none)'}", file=sys.stderr)
            return 1
        targets = [suite_name]

    for name in targets:
        fn = get_suite(name)
        t0 = time.monotonic()
        try:
            result: RunResult = fn(suite_argv)
        except Exception as exc:
            print(f"error: suite {name!r} failed: {exc}", file=sys.stderr)
            return 2

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        tok_in = result.tokens_in
        tok_out = result.tokens_out
        lat = result.latency_ms or elapsed_ms

        print(
            f"suite={name} score={result.score:.3f} n_cases={len(result.cases)}"
            f" tokens_in={tok_in} tokens_out={tok_out} latency_ms={lat}"
        )

        if args.save:
            run_id = save_run(result)
            print(f"saved run_id={run_id}")

    return 0


def cmd_report(args: argparse.Namespace) -> int:
    since_ts: int | None = None
    if args.since and args.since != "all":
        days_str = args.since.rstrip("d")
        try:
            days = int(days_str)
        except ValueError:
            print(f"error: --since must be 7d, 30d, or all (got {args.since!r})", file=sys.stderr)
            return 1
        import time as _time
        since_ts = int(_time.time()) - days * 86400

    rows = recent_runs(suite=args.suite, limit=args.limit, since_ts=since_ts)
    if not rows:
        print("no runs found")
        return 0

    header = "| suite | ts | score | n | tokens_in | latency_ms | git_sha |"
    sep    = "|-------|-----|-------|---|-----------|------------|---------|"
    print(header)
    print(sep)
    for r in rows:
        print(_fmt_row(r))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("list", help="list registered suites")

    run_p = sub.add_parser("run", help="run a suite")
    run_p.add_argument("suite", help="suite name or 'all'")
    run_p.add_argument("suite_args", nargs=argparse.REMAINDER)
    # --save is consumed before argparse in main() so it can appear anywhere
    # around the suite name without being eaten by REMAINDER.

    report_p = sub.add_parser("report", help="show recent runs")
    report_p.add_argument("--suite", default=None)
    report_p.add_argument("--since", default="30d", help="7d, 30d, or all")
    report_p.add_argument("--limit", type=int, default=20)

    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    save = False
    if argv and argv[0] == "run":
        filtered = ["run"]
        for token in argv[1:]:
            if token == "--save":
                save = True
            else:
                filtered.append(token)
        argv = filtered

    p = _build_parser()
    args = p.parse_args(argv)

    if args.command == "list":
        return cmd_list(args)
    if args.command == "run":
        args.save = save
        if args.suite_args and args.suite_args[0] == "--":
            args.suite_args = args.suite_args[1:]
        return cmd_run(args)
    if args.command == "report":
        return cmd_report(args)

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
