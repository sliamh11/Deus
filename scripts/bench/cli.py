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
from .store import get_cases, recent_runs, resolve_run, save_run
from .types import RunResult


def _fmt_row(r: dict) -> str:
    ts = r.get("ts", 0)
    score = r.get("score")
    score_s = f"{score:.3f}" if score is not None else "—"
    n = r.get("n_cases", 0)
    tok_in = r.get("tokens_in", 0)
    lat = r.get("latency_ms", 0)
    git = r.get("git_sha") or "—"
    label = r.get("label") or "—"
    suite = r.get("suite", "")
    return f"| {suite} | {ts} | {score_s} | {n} | {tok_in} | {lat} | {git} | {label} |"


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
            run_id = save_run(result, label=args.label)
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

    header = "| suite | ts | score | n | tokens_in | latency_ms | git_sha | label |"
    sep    = "|-------|-----|-------|---|-----------|------------|---------|-------|"
    print(header)
    print(sep)
    for r in rows:
        print(_fmt_row(r))
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    suite_filter: str | None = args.suite
    tolerance: float = args.tolerance
    growth_threshold: float = args.growth_threshold

    run_a = resolve_run(args.a, suite=suite_filter)
    run_b = resolve_run(args.b, suite=suite_filter)

    missing: list[str] = []
    if run_a is None:
        missing.append(args.a)
    if run_b is None:
        missing.append(args.b)

    if missing:
        for m in missing:
            print(f"error: no run found for {m!r}", file=sys.stderr)
        # Print candidates to help user
        candidates = recent_runs(suite=suite_filter, limit=20)
        if candidates:
            print("available runs (run_id | label | git_sha | suite | ts):", file=sys.stderr)
            for c in candidates:
                rid = c.get("run_id", "")
                lbl = c.get("label") or "—"
                sha = c.get("git_sha") or "—"
                sname = c.get("suite", "")
                ts = c.get("ts", 0)
                print(f"  {rid}  {lbl}  {sha}  {sname}  {ts}", file=sys.stderr)
        return 2

    cases_a = {c["case_id"]: c for c in get_cases(run_a["run_id"])}
    cases_b = {c["case_id"]: c for c in get_cases(run_b["run_id"])}

    all_ids = sorted(set(cases_a) | set(cases_b))
    common_ids = sorted(set(cases_a) & set(cases_b))
    only_a = sorted(set(cases_a) - set(cases_b))
    only_b = sorted(set(cases_b) - set(cases_a))

    rows: list[tuple[str, str, str, str, str]] = []  # case_id, score_a, score_b, delta, status

    has_regression = False

    for cid in common_ids:
        sa = cases_a[cid]["score"] if cases_a[cid]["score"] is not None else 0.0
        sb = cases_b[cid]["score"] if cases_b[cid]["score"] is not None else 0.0
        delta = sb - sa
        if delta < -tolerance:
            status = "-regressed"
            has_regression = True
        elif delta > tolerance:
            status = "+improved"
        else:
            status = "unchanged"
        rows.append((cid, f"{sa:.3f}", f"{sb:.3f}", f"{delta:+.3f}", status))

    for cid in only_a:
        sa = cases_a[cid]["score"] if cases_a[cid]["score"] is not None else 0.0
        rows.append((cid, f"{sa:.3f}", "—", "—", "dropped"))

    for cid in only_b:
        sb = cases_b[cid]["score"] if cases_b[cid]["score"] is not None else 0.0
        rows.append((cid, "—", f"{sb:.3f}", "—", "added"))

    print("| case_id | score_a | score_b | delta | status |")
    print("|---------|---------|---------|-------|--------|")
    for case_id, sa, sb, delta, status in rows:
        print(f"| {case_id} | {sa} | {sb} | {delta} | {status} |")

    score_a = run_a.get("score")
    score_b = run_b.get("score")
    score_a_s = f"{score_a:.3f}" if score_a is not None else "—"
    score_b_s = f"{score_b:.3f}" if score_b is not None else "—"
    if score_a is not None and score_b is not None:
        suite_delta_s = f"{score_b - score_a:+.3f}"
    else:
        suite_delta_s = "—"

    n_improved = sum(1 for *_, s in rows if s == "+improved")
    n_regressed = sum(1 for *_, s in rows if s == "-regressed")
    n_unchanged = sum(1 for *_, s in rows if s == "unchanged")
    n_added = sum(1 for *_, s in rows if s == "added")
    n_dropped = sum(1 for *_, s in rows if s == "dropped")

    suite_name = run_a.get("suite", "")
    print(
        f"\nsuite={suite_name} score_a={score_a_s} score_b={score_b_s} delta={suite_delta_s}"
        f" improved={n_improved} regressed={n_regressed} unchanged={n_unchanged}"
        f" added={n_added} dropped={n_dropped}"
    )

    has_growth_alert = False

    # Per-case growth alerts
    for cid in common_ids:
        ca = cases_a[cid]
        cb = cases_b[cid]
        tok_a = ca.get("tokens_in") or 0
        tok_b = cb.get("tokens_in") or 0
        sa = ca["score"] if ca["score"] is not None else 0.0
        sb = cb["score"] if cb["score"] is not None else 0.0
        score_unchanged = abs(sb - sa) <= tolerance
        if tok_a > 0 and score_unchanged:
            baseline = min(tok_a, tok_b)
            growth = (tok_b - tok_a) / baseline if baseline > 0 else 0.0
            if growth > growth_threshold:
                pct = growth * 100
                print(
                    f"growth alert: {cid} tokens_in grew +{pct:.1f}%"
                    f" (A: {tok_a} → B: {tok_b}) with unchanged score"
                )
                has_growth_alert = True

    # Suite-level growth alert
    suite_tok_a = run_a.get("tokens_in") or 0
    suite_tok_b = run_b.get("tokens_in") or 0
    suite_score_unchanged = (
        score_a is not None
        and score_b is not None
        and abs(score_b - score_a) <= tolerance
    )
    if suite_tok_a > 0 and suite_score_unchanged:
        baseline = min(suite_tok_a, suite_tok_b)
        suite_growth = (suite_tok_b - suite_tok_a) / baseline if baseline > 0 else 0.0
        if suite_growth > growth_threshold:
            pct = suite_growth * 100
            print(
                f"growth alert: <suite> tokens_in grew +{pct:.1f}%"
                f" (A: {suite_tok_a} → B: {suite_tok_b}) with unchanged score"
            )
            has_growth_alert = True

    return 1 if (has_regression or has_growth_alert) else 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bench")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("list", help="list registered suites")

    run_p = sub.add_parser("run", help="run a suite")
    run_p.add_argument("suite", help="suite name or 'all'")
    run_p.add_argument("suite_args", nargs=argparse.REMAINDER)
    # --save and --label are consumed before argparse in main() so they can
    # appear anywhere around the suite name without being eaten by REMAINDER.

    report_p = sub.add_parser("report", help="show recent runs")
    report_p.add_argument("--suite", default=None)
    report_p.add_argument("--since", default="30d", help="7d, 30d, or all")
    report_p.add_argument("--limit", type=int, default=20)

    diff_p = sub.add_parser("diff", help="compare two runs case-by-case")
    diff_p.add_argument("a", help="run_id, label, or git_sha of run A")
    diff_p.add_argument("b", help="run_id, label, or git_sha of run B")
    diff_p.add_argument("--suite", default=None, help="restrict resolution to this suite")
    diff_p.add_argument("--tolerance", type=float, default=0.01,
                        help="min |delta| to count as improved/regressed (default 0.01)")
    diff_p.add_argument("--growth-threshold", type=float, default=0.05,
                        help="tokens_in growth fraction that triggers an alert (default 0.05 = 5%%)")

    return p


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    save = False
    label: str | None = None

    if argv and argv[0] == "run":
        filtered = ["run"]
        i = 1
        while i < len(argv):
            token = argv[i]
            if token == "--save":
                save = True
                i += 1
            elif token == "--label":
                i += 1
                if i < len(argv):
                    label = argv[i]
                i += 1
            elif token.startswith("--label="):
                label = token[len("--label="):]
                i += 1
            else:
                filtered.append(token)
                i += 1
        argv = filtered

    p = _build_parser()
    args = p.parse_args(argv)

    if args.command == "list":
        return cmd_list(args)
    if args.command == "run":
        args.save = save
        args.label = label
        if args.suite_args and args.suite_args[0] == "--":
            args.suite_args = args.suite_args[1:]
        return cmd_run(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "diff":
        return cmd_diff(args)

    p.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
