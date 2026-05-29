#!/usr/bin/env python3
"""
Split re-judge diagnostic: validates the ceiling-effect hypothesis.

Re-judges N interactions with the new Likert format, then splits results into
two groups:
  - Group A: old quality score was at bounds (0.0 or 1.0)
  - Group B: old quality score was NOT at bounds (had genuine nuance)

If the ceiling-effect hypothesis is correct, Group B should show significantly
higher Pearson correlation than Group A (which was noise within a compressed band).

Usage:
    python3 -m evolution.diagnostics.split_rejudge_diagnostic [--n 50] [--json]
"""
import argparse
import json
import math
import os
import random
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from evolution.config import EVOLUTION_DB_PATH, JUDGE_MAX_PROMPT_CHARS, JUDGE_MAX_RESPONSE_CHARS
from evolution.judge import make_runtime_judge
from evolution.judge.criteria import compose_score


def pearson_r(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return float("nan")
    return cov / (sx * sy)


def load_old_interactions(db_path: str, n: int, seed: int = 42) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        # Over-fetch 3x to absorb filtering loss (~50% have usable judge_dims)
        rows = conn.execute("""
            SELECT id, prompt, response, tools_used, judge_score, judge_dims
            FROM interactions
            WHERE judge_dims IS NOT NULL
              AND judge_dims != ''
              AND judge_schema_version IS NULL
              AND prompt IS NOT NULL
              AND response IS NOT NULL
              AND LENGTH(prompt) > 20
              AND LENGTH(response) > 20
            ORDER BY RANDOM()
            LIMIT ?
        """, (n * 3,)).fetchall()

    results = []
    for r in rows:
        dims = json.loads(r["judge_dims"])
        q = dims.get("quality")
        if q is None:
            continue
        results.append({
            "id": r["id"],
            "prompt": r["prompt"][:JUDGE_MAX_PROMPT_CHARS],
            "response": r["response"][:JUDGE_MAX_RESPONSE_CHARS],
            "tools_used": json.loads(r["tools_used"]) if r["tools_used"] else None,
            "old_score": r["judge_score"],
            "old_dims": dims,
            "old_quality": q,
            "at_bounds": q in (0.0, 1.0),
        })

    random.seed(seed)
    random.shuffle(results)
    return results[:n]


def rejudge(interactions: list[dict]) -> list[dict]:
    judge = make_runtime_judge(provider="ollama")
    total = len(interactions)
    survived = []
    for i, item in enumerate(interactions):
        t0 = time.time()
        try:
            result = judge.evaluate(
                prompt=item["prompt"],
                response=item["response"],
                tools_used=item["tools_used"],
            )
        except Exception as exc:
            elapsed = time.time() - t0
            print(f"  [{i+1}/{total}] ERROR: {exc} ({int(elapsed*1000)}ms)", file=sys.stderr)
            continue
        elapsed = time.time() - t0
        item["new_score"] = result.score
        item["new_dims"] = {
            "quality": result.quality,
            "safety": result.safety,
            "tool_use": result.tool_use,
            "personalization": result.personalization,
        }
        item["new_quality"] = result.quality
        item["latency_ms"] = int(elapsed * 1000)
        item["parse_error"] = result.is_parse_error
        tag = "[BOUNDS]" if item["at_bounds"] else "[MID]"
        if result.is_parse_error:
            tag += " [PARSE_ERR]"
        print(f"  [{i+1}/{total}] old={item['old_score']:.3f} new={item['new_score']:.3f} "
              f"q:{item['old_quality']:.1f}->{item['new_quality']:.2f} "
              f"{tag} ({item['latency_ms']}ms)", file=sys.stderr)
        survived.append(item)
    return survived


def analyze(interactions: list[dict]) -> dict:
    at_bounds = [i for i in interactions if i["at_bounds"]]
    not_bounds = [i for i in interactions if not i["at_bounds"]]
    parse_errors = sum(1 for i in interactions if i.get("parse_error"))

    def group_stats(group: list[dict], label: str) -> dict:
        if not group:
            return {"label": label, "n": 0, "pearson_r": None}
        old_scores = [i["old_score"] for i in group]
        new_scores = [i["new_score"] for i in group]
        old_q = [i["old_quality"] for i in group]
        new_q = [i["new_quality"] for i in group]
        delta = [n - o for o, n in zip(old_scores, new_scores)]
        return {
            "label": label,
            "n": len(group),
            "pearson_r_composite": round(pearson_r(old_scores, new_scores), 4),
            "pearson_r_quality": round(pearson_r(old_q, new_q), 4),
            "old_mean": round(sum(old_scores) / len(old_scores), 4),
            "new_mean": round(sum(new_scores) / len(new_scores), 4),
            "delta_mean": round(sum(delta) / len(delta), 4),
            "agreement_pct": round(100 * sum(1 for d in delta if abs(d) <= 0.05) / len(delta), 1),
        }

    all_stats = group_stats(interactions, "all")
    bounds_stats = group_stats(at_bounds, "at_bounds")
    mid_stats = group_stats(not_bounds, "not_at_bounds")

    per_dim = {}
    for dim in ["quality", "safety", "tool_use", "personalization"]:
        old_vals = [i["old_dims"].get(dim, 0) for i in interactions]
        new_vals = [i["new_dims"].get(dim, 0) for i in interactions]
        old_at_b = sum(1 for v in old_vals if v in (0.0, 1.0))
        new_at_b = sum(1 for v in new_vals if v in (0.0, 1.0))
        per_dim[dim] = {
            "pearson_r": round(pearson_r(old_vals, new_vals), 4),
            "old_pct_at_bounds": round(100 * old_at_b / len(old_vals), 1),
            "new_pct_at_bounds": round(100 * new_at_b / len(new_vals), 1),
        }

    mid_r = mid_stats.get("pearson_r_composite")
    bounds_r = bounds_stats.get("pearson_r_composite")
    if mid_stats["n"] < 5:
        hypothesis = "INCONCLUSIVE (not_at_bounds group too small)"
    elif mid_r is None or math.isnan(mid_r):
        hypothesis = "INCONCLUSIVE (cannot compute correlation)"
    elif mid_r > 0.3 and (bounds_r is None or math.isnan(bounds_r) or mid_r > bounds_r):
        hypothesis = "CONFIRMED"
    elif mid_r > 0.3:
        hypothesis = "PARTIAL (mid-range correlates but not more than bounds)"
    else:
        hypothesis = "REJECTED (mid-range group also shows low correlation)"

    return {
        "n": len(interactions),
        "parse_errors": parse_errors,
        "all": all_stats,
        "at_bounds": bounds_stats,
        "not_at_bounds": mid_stats,
        "per_dim": per_dim,
        "hypothesis": hypothesis,
    }


def main():
    parser = argparse.ArgumentParser(description="Split re-judge diagnostic")
    parser.add_argument("--n", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print(f"Loading {args.n} old-format interactions...", file=sys.stderr)
    interactions = load_old_interactions(str(EVOLUTION_DB_PATH), args.n, args.seed)
    n_bounds = sum(1 for i in interactions if i["at_bounds"])
    n_mid = len(interactions) - n_bounds
    print(f"Loaded {len(interactions)}: {n_bounds} at-bounds, {n_mid} mid-range", file=sys.stderr)

    print(f"\nRe-judging with Likert format via Ollama...", file=sys.stderr)
    interactions = rejudge(interactions)

    results = analyze(interactions)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"SPLIT RE-JUDGE DIAGNOSTIC (n={results['n']})")
        print(f"{'='*60}")
        print(f"Parse errors: {results['parse_errors']}")
        for group in ["all", "at_bounds", "not_at_bounds"]:
            g = results[group]
            print(f"\n--- {g['label']} (n={g['n']}) ---")
            if g["n"] == 0:
                print("  (no data)")
                continue
            print(f"  Pearson r (composite): {g.get('pearson_r_composite', 'N/A')}")
            print(f"  Pearson r (quality):   {g.get('pearson_r_quality', 'N/A')}")
            print(f"  Old mean: {g['old_mean']:.3f}  New mean: {g['new_mean']:.3f}  Delta: {g['delta_mean']:+.3f}")
            print(f"  Agreement (|delta|<=0.05): {g['agreement_pct']}%")

        print(f"\n--- Per-dimension bounds shift ---")
        for dim, d in results["per_dim"].items():
            print(f"  {dim:20s}  r={d['pearson_r']:+.3f}  bounds: {d['old_pct_at_bounds']}% -> {d['new_pct_at_bounds']}%")

        print(f"\nHypothesis (ceiling decompression): {results['hypothesis']}")


if __name__ == "__main__":
    main()
