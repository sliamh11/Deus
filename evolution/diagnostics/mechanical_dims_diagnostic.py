"""
Diagnostic for mechanical judge dimensions (tool_economy, gate_audit).

Queries the evolution DB and reports:
  1. Distribution: histogram of scores per mechanical dim
  2. Discrimination: do low te/ga scores correlate with different sessions?
  3. Composite impact: how much do mechanical dims shift the composite?
  4. Correlation: Pearson r between mechanical dims and LLM-judged dims
  5. Weight sensitivity: what-if analysis for alternative weight allocations

Usage:
    python3 -m evolution.diagnostics.mechanical_dims_diagnostic
    python3 -m evolution.diagnostics.mechanical_dims_diagnostic --rescore
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from evolution.judge.criteria import COMPOSITE_WEIGHTS as CURRENT_WEIGHTS, DIM_DEFAULTS


DB_PATH = Path(os.environ.get("DEUS_EVOLUTION_DB", os.path.expanduser("~/.deus/evolution.db")))

LLM_DIMS = ("quality", "safety", "tool_use", "personalization")
MECH_DIMS = ("tool_economy", "gate_audit", "completion_honesty")

# Pre-mechanical baseline: original 4-dim LLM-only weights (for composite impact comparison).
LLM_ONLY_WEIGHTS = {"quality": 0.45, "safety": 0.25, "tool_use": 0.15, "personalization": 0.15}


def _compose(dims: dict, weights: dict | None = None) -> float:
    w = weights or CURRENT_WEIGHTS
    return sum(w[k] * dims.get(k, DIM_DEFAULTS[k]) for k in w)


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return float("nan")
    return cov / (sx * sy)


def _bucket(score: float) -> str:
    if score >= 0.95:
        return "1.0"
    if score >= 0.75:
        return "0.75-0.94"
    if score >= 0.50:
        return "0.50-0.74"
    if score >= 0.25:
        return "0.25-0.49"
    return "0.00-0.24"


def load_rows(db_path: Path) -> list[dict]:
    db = sqlite3.connect(str(db_path))
    try:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT id, judge_score, judge_dims, session_id, eval_suite "
            "FROM interactions WHERE judge_dims IS NOT NULL"
        ).fetchall()
    finally:
        db.close()

    result = []
    for r in rows:
        try:
            dims = json.loads(r["judge_dims"])
        except (json.JSONDecodeError, TypeError):
            continue
        result.append({
            "id": r["id"],
            "judge_score": r["judge_score"],
            "dims": dims,
            "session_id": r["session_id"] or "unknown",
            "eval_suite": r["eval_suite"] or "unknown",
        })
    return result


def report_distribution(rows: list[dict]) -> None:
    print("\n=== 1. Score Distribution ===")
    for dim in MECH_DIMS:
        scores = [r["dims"].get(dim) for r in rows if dim in r["dims"]]
        if not scores:
            print(f"\n  {dim}: no data (dim not yet backfilled)")
            continue
        buckets = Counter(_bucket(s) for s in scores)
        total = len(scores)
        avg = sum(scores) / total
        non_perfect = sum(1 for s in scores if s < 1.0)
        print(f"\n  {dim} (n={total}, avg={avg:.3f}, non-1.0={non_perfect} ({100*non_perfect/total:.1f}%))")
        for b in ["1.0", "0.75-0.94", "0.50-0.74", "0.25-0.49", "0.00-0.24"]:
            count = buckets.get(b, 0)
            bar = "#" * (count * 40 // max(total, 1))
            print(f"    {b:>10s}: {count:4d} ({100*count/total:5.1f}%) {bar}")


def report_discrimination(rows: list[dict]) -> None:
    print("\n=== 2. Session-Level Discrimination ===")
    for dim in MECH_DIMS:
        session_scores: dict[str, list[float]] = defaultdict(list)
        for r in rows:
            if dim in r["dims"]:
                session_scores[r["session_id"]].append(r["dims"][dim])
        if not session_scores:
            print(f"\n  {dim}: no data")
            continue

        session_avgs = {sid: sum(s) / len(s) for sid, s in session_scores.items()}
        flagged = {sid: avg for sid, avg in session_avgs.items() if avg < 0.9}
        clean = {sid: avg for sid, avg in session_avgs.items() if avg >= 0.9}

        print(f"\n  {dim}: {len(session_avgs)} sessions, {len(flagged)} flagged (<0.9 avg), {len(clean)} clean")
        if flagged:
            worst = sorted(flagged.items(), key=lambda x: x[1])[:5]
            for sid, avg in worst:
                n = len(session_scores[sid])
                print(f"    {sid[:12]}... avg={avg:.3f} (n={n})")


def report_composite_impact(rows: list[dict]) -> None:
    print("\n=== 3. Composite Impact ===")
    rows_with_mech = [r for r in rows if any(d in r["dims"] for d in MECH_DIMS)]
    if not rows_with_mech:
        print("  No rows with mechanical dims. Run --rescore first.")
        return

    deltas = []
    for r in rows_with_mech:
        old_composite = sum(LLM_ONLY_WEIGHTS.get(k, 0) * r["dims"].get(k, 0) for k in LLM_ONLY_WEIGHTS)
        new_composite = _compose(r["dims"])
        deltas.append(new_composite - old_composite)

    avg_delta = sum(deltas) / len(deltas)
    max_drop = min(deltas)
    max_boost = max(deltas)
    negative = sum(1 for d in deltas if d < -0.01)
    print(f"  Rows analyzed: {len(deltas)}")
    print(f"  Avg composite shift: {avg_delta:+.4f}")
    print(f"  Max drop: {max_drop:+.4f}  Max boost: {max_boost:+.4f}")
    print(f"  Rows with >0.01 drop: {negative} ({100*negative/len(deltas):.1f}%)")


def report_correlation(rows: list[dict]) -> None:
    print("\n=== 4. Correlation (Pearson r) ===")
    print("  Measures whether mechanical dims are redundant with LLM dims or orthogonal.")
    print("  |r| < 0.2 = orthogonal (good: measures something new)")
    print("  |r| > 0.5 = redundant (bad: duplicates existing signal)\n")

    for mech in MECH_DIMS:
        mech_scores = [r["dims"].get(mech) for r in rows if mech in r["dims"]]
        if len(mech_scores) < 10:
            print(f"  {mech}: insufficient data (n={len(mech_scores)})")
            continue

        relevant_rows = [r for r in rows if mech in r["dims"]]
        for llm in LLM_DIMS:
            xs = [r["dims"].get(mech, DIM_DEFAULTS[mech]) for r in relevant_rows]
            ys = [r["dims"].get(llm, DIM_DEFAULTS[llm]) for r in relevant_rows]
            r_val = _pearson(xs, ys)
            tag = "orthogonal" if abs(r_val) < 0.2 else "moderate" if abs(r_val) < 0.5 else "REDUNDANT"
            print(f"  {mech:>15s} x {llm:<20s} r={r_val:+.3f}  [{tag}]")
        print()


def report_weight_sensitivity(rows: list[dict]) -> None:
    print("\n=== 5. Weight Sensitivity ===")
    rows_with_mech = [r for r in rows if any(d in r["dims"] for d in MECH_DIMS)]
    if not rows_with_mech:
        print("  No rows with mechanical dims.")
        return

    scenarios = [
        ("current",  CURRENT_WEIGHTS),
        ("te=0.15",  {**CURRENT_WEIGHTS, "quality": 0.25, "tool_economy": 0.15}),
        ("ga=0.10",  {**CURRENT_WEIGHTS, "quality": 0.25, "gate_audit": 0.10}),
        ("no-mech",  {**LLM_ONLY_WEIGHTS, **{k: 0.0 for k in MECH_DIMS}}),
    ]

    print(f"  {'scenario':>10s}  {'avg':>6s}  {'stddev':>6s}  {'<0.5':>5s}  {'<0.3':>5s}")
    for name, weights in scenarios:
        composites = [_compose(r["dims"], weights) for r in rows_with_mech]
        avg = sum(composites) / len(composites)
        stddev = math.sqrt(sum((c - avg) ** 2 for c in composites) / len(composites))
        below_50 = sum(1 for c in composites if c < 0.5)
        below_30 = sum(1 for c in composites if c < 0.3)
        print(f"  {name:>10s}  {avg:6.3f}  {stddev:6.3f}  {below_50:5d}  {below_30:5d}")


def rescore_mechanical(db_path: Path) -> int:
    """Re-score all rows with mechanical dims without re-running LLM judge."""
    from evolution.judge.mechanical import score_tool_economy, score_gate_audit, score_completion_honesty
    from evolution.judge.criteria import compose_score
    from evolution.cc_backfill import collect_pairs, CC_SESSIONS_DIR

    pairs = collect_pairs(CC_SESSIONS_DIR)
    pair_map = {p["interaction_id"]: p for p in pairs}

    db = sqlite3.connect(str(db_path))
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT id, judge_dims FROM interactions WHERE judge_dims IS NOT NULL"
    ).fetchall()

    updated = 0
    try:
        for r in rows:
            iid = r["id"]
            try:
                dims = json.loads(r["judge_dims"])
            except (json.JSONDecodeError, TypeError):
                continue

            pair = pair_map.get(iid)
            tool_calls = pair.get("tool_calls", []) if pair else []
            response_text = pair.get("response", "") if pair else ""

            te_score, _ = score_tool_economy(tool_calls)
            ga_score, _ = score_gate_audit(tool_calls)
            ch_score, _ = score_completion_honesty(tool_calls, response_text)

            dims["tool_economy"] = te_score
            dims["gate_audit"] = ga_score
            dims["completion_honesty"] = ch_score
            composite = compose_score(dims)

            db.execute(
                "UPDATE interactions SET judge_dims = ?, judge_score = ? WHERE id = ?",
                (json.dumps(dims), composite, iid),
            )
            updated += 1
        db.commit()
    finally:
        db.close()
    return updated


def main() -> None:
    parser = argparse.ArgumentParser(description="Mechanical dims diagnostic")
    parser.add_argument("--rescore", action="store_true",
                        help="Re-score all rows with mechanical dims (no LLM re-judge)")
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help=f"Path to evolution.db (default: {DB_PATH})")
    args = parser.parse_args()

    if args.rescore:
        print(f"Re-scoring mechanical dims in {args.db}...")
        n = rescore_mechanical(args.db)
        print(f"Updated {n} rows.\n")

    rows = load_rows(args.db)
    print(f"Loaded {len(rows)} scored interactions from {args.db}")

    report_distribution(rows)
    report_discrimination(rows)
    report_composite_impact(rows)
    report_correlation(rows)
    report_weight_sensitivity(rows)


if __name__ == "__main__":
    main()
