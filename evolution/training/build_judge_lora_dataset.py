"""Build a judge-LoRA dataset from Deus's stored judge_dims ground truth.

Input: evolution/storage chat-only interactions with judge_dims JSON.
Output: train.jsonl / valid.jsonl / test.jsonl + manifest.json (mlx-lm chat format).

Plan: ~/.claude/plans/feat-judge-lora-dataset.md (v2 SHIP). One inline deviation:
--max-prompt-chars default is 8000 (not the plan-time estimate of 4500). Empirical
prompt-length p90 is 5866; 4500 dropped 17.3% of records, 8000 drops 3.8%. The
calibration is documented in the argparse help string and this docstring.

Structural approach: single-pass procedural pipeline -- fetch -> filter -> build records
-> stratified split -> write JSONL + manifest. No registry/abstraction; pure utility.

Stratification: composite-score quintile buckets with per-bucket minimum-floor enforcement.
Mid-band buckets (Q2..Q4) get a floor of 8 validation samples + 4 test samples so that
val Pearson measures discrimination on the hardest band, not Q5-tail saturation. Overflow
flows to train. Q5 takes proportional only (no floor) to avoid letting the dominant tail
crowd discrimination measurement.

Storage API surface (verified against evolution/storage/providers/sqlite.py:367):
    get_recent_interactions(*, limit=50, eval_suite='runtime', ...)
We MUST opt out of both defaults:
    - limit=10000  (default 50 truncates the population)
    - eval_suite=None  (default 'runtime' excludes the dominant claude_code suite)

There is no native chat-only filter; we enforce it post-fetch (prompt is non-empty and
not a reflection marker, response is non-empty).
"""
from __future__ import annotations
import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from evolution.judge.criteria import RUBRIC, COMPOSITE_WEIGHTS, compose_score
from evolution.storage import get_storage
from evolution.training._provenance import git_sha, git_dirty, sha256_file

DIM_KEYS = ("quality", "safety", "tool_use", "personalization")


def is_chat_interaction(r: dict) -> bool:
    """Chat-only filter: non-reflection prompt + non-empty response."""
    p = (r.get("prompt") or "").strip()
    resp = (r.get("response") or "").strip()
    return bool(resp) and not p.startswith("<reflections>")


def build_eval_prompt(prompt: str, response: str, tools_used=None, context=None) -> str:
    """Same shape as the production judge eval prompt and the bench runner."""
    parts = [RUBRIC, "\n## Interaction to evaluate\n"]
    if context:
        parts.append(f"**Context:** {context}\n")
    parts.append(f"**User prompt:**\n{prompt}\n")
    if tools_used:
        parts.append(f"**Tools used:** {', '.join(tools_used)}\n")
    parts.append(f"**Agent response:**\n{response}\n")
    return "\n".join(parts)


def parse_dims(raw: str | None) -> dict | None:
    """Parse judge_dims JSON. Returns None on any failure."""
    if not raw:
        return None
    try:
        d = json.loads(raw)
    except Exception:
        return None
    if not all(k in d for k in DIM_KEYS):
        return None
    try:
        return {k: float(d[k]) for k in DIM_KEYS}
    except Exception:
        return None


def composite_bucket(score: float) -> str:
    """Bucket composite score into one of 5 quintile-like bands."""
    if score < 0.2:
        return "Q1"
    if score < 0.4:
        return "Q2"
    if score < 0.6:
        return "Q3"
    if score < 0.8:
        return "Q4"
    return "Q5"


def synth_rationale(dims: dict) -> str:
    """Deterministic short rationale from the dim signature.

    Avoids any external LLM dependency. Pattern: weakest-dim drives the rationale,
    ties broken by COMPOSITE_WEIGHTS priority (so quality wins ties at the same score).
    """
    items = sorted(
        dims.items(),
        key=lambda kv: (kv[1], -COMPOSITE_WEIGHTS.get(kv[0], 0)),
    )
    composite = compose_score(dims)
    if composite >= 0.9:
        return "Response fully meets the rubric across all dimensions."
    if composite >= 0.7:
        return "Response largely meets the rubric with minor gaps."
    weakest_key, weakest_val = items[0]
    if weakest_val == 0.0:
        return f"Response fails on {weakest_key}; other dimensions vary."
    if weakest_val <= 0.5:
        return f"Response is partial on {weakest_key} and uneven elsewhere."
    return "Response is mixed across the rubric dimensions."


def build_record(row: dict, dims: dict) -> dict:
    """Build one mlx-lm chat record: user=eval_prompt, assistant=dims JSON."""
    tools_used = None
    if row.get("tools_used"):
        try:
            tools_used = json.loads(row["tools_used"])
        except Exception:
            tools_used = None
    eval_prompt = build_eval_prompt(
        row.get("prompt", ""),
        row.get("response", ""),
        tools_used=tools_used,
        context=row.get("context") or None,
    )
    target = {
        "quality": round(dims["quality"], 2),
        "safety": round(dims["safety"], 2),
        "tool_use": round(dims["tool_use"], 2),
        "personalization": round(dims["personalization"], 2),
        "rationale": synth_rationale(dims),
    }
    return {
        "messages": [
            {"role": "user", "content": eval_prompt},
            {"role": "assistant", "content": json.dumps(target, ensure_ascii=False)},
        ],
    }


def stratified_split(
    records: list[dict],
    composites: list[float],
    seed: int,
    val_fraction: float,
    test_fraction: float,
    floor_val: int = 8,
    floor_test: int = 4,
):
    """Stratified sampling with per-bucket minimum-floor enforcement.

    Q1..Q4: n_val = max(round(n * val_fraction), min(floor_val, n // 2))
            n_test = max(round(n * test_fraction), min(floor_test, n // 4))
    Q5:     n_val = round(n * val_fraction)
            n_test = round(n * test_fraction)
    Train = residual (n - n_val - n_test).

    The Q5 (composite >= 0.8) bucket is the dominant tail and intentionally takes
    no floor so it does not crowd out mid-band discrimination measurement.
    """
    rng = random.Random(seed)
    buckets: dict[str, list[int]] = defaultdict(list)
    for idx, c in enumerate(composites):
        buckets[composite_bucket(c)].append(idx)

    val_idx: set[int] = set()
    test_idx: set[int] = set()

    for bucket_name in sorted(buckets):
        members = buckets[bucket_name][:]
        rng.shuffle(members)
        n = len(members)
        if n == 0:
            continue
        if bucket_name == "Q5":
            n_val = max(int(round(n * val_fraction)), 0)
            n_test = max(int(round(n * test_fraction)), 0)
        else:
            n_val = max(int(round(n * val_fraction)), min(floor_val, n // 2))
            n_test = max(int(round(n * test_fraction)), min(floor_test, n // 4))
        n_val = min(n_val, n - 1) if n > 1 else 0
        n_test = min(n_test, max(0, n - n_val - 1))
        for i in members[:n_val]:
            val_idx.add(i)
        for i in members[n_val : n_val + n_test]:
            test_idx.add(i)

    train, valid, test = [], [], []
    for i, rec in enumerate(records):
        if i in val_idx:
            valid.append(rec)
        elif i in test_idx:
            test.append(rec)
        else:
            train.append(rec)
    return train, valid, test


def write_jsonl(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_manifest(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def split_bucket_counts(split_records: list[dict]) -> dict:
    """Per-split bucket distribution for manifest + smoke test."""
    counts: dict[str, int] = defaultdict(int)
    for rec in split_records:
        try:
            dims = json.loads(rec["messages"][1]["content"])
            dim_subset = {k: float(dims[k]) for k in COMPOSITE_WEIGHTS}
            counts[composite_bucket(compose_score(dim_subset))] += 1
        except Exception:
            counts["?"] += 1
    return dict(sorted(counts.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--out-dir",
        default=str(PROJECT_ROOT / "finetune/judge-lora-gemma3n"),
        help=(
            "Directory for data/{train,valid,test}.jsonl and manifest.json. "
            "Defaults to <repo>/finetune/judge-lora-gemma3n, matching the "
            "existing finetune/ convention. The path is gitignored."
        ),
    )
    ap.add_argument("--val-fraction", type=float, default=0.10)
    ap.add_argument("--test-fraction", type=float, default=0.05)
    ap.add_argument("--seed", type=int, default=20260517)
    ap.add_argument(
        "--max-prompt-chars",
        type=int,
        default=8000,
        help=(
            "Drop interactions whose eval_prompt exceeds this length. "
            "Default 8000 captures p95 (7045) of the production distribution "
            "while still leaving room for the assistant turn + chat-template "
            "overhead within Gemma-3n's native context. The original plan-time "
            "estimate of 4500 was based on sparse sampling and turned out to "
            "drop 17.3%% of records; 8000 drops only 3.8%%."
        ),
    )
    ap.add_argument(
        "--min-usable",
        type=int,
        default=700,
        help="Smoke test: abort if usable record count below this",
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir).expanduser()
    data_dir = out_dir / "data"

    # Fetch with explicit non-default kwargs (see module docstring)
    rows = get_storage().get_recent_interactions(
        limit=10000,
        eval_suite=None,
        min_score=0.0,
    )
    chat = [r for r in rows if is_chat_interaction(r)]

    records: list[dict] = []
    composites: list[float] = []
    dim_dist: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    skipped_no_dims = 0
    skipped_too_long = 0

    for r in chat:
        dims = parse_dims(r.get("judge_dims"))
        if dims is None:
            skipped_no_dims += 1
            continue
        rec = build_record(r, dims)
        prompt_len = len(rec["messages"][0]["content"])
        if prompt_len > args.max_prompt_chars:
            skipped_too_long += 1
            continue
        records.append(rec)
        composites.append(compose_score(dims))
        for k, v in dims.items():
            dim_dist[k][f"{v:.1f}"] += 1

    if len(records) < args.min_usable:
        print(
            f"ABORT: only {len(records)} usable records, below --min-usable={args.min_usable}",
            file=sys.stderr,
        )
        sys.exit(2)

    train, valid, test = stratified_split(
        records, composites, args.seed, args.val_fraction, args.test_fraction
    )

    # Smoke test: every non-Q5 bucket that EXISTS in the data must appear in valid.
    # Q1 (composite < 0.2) is typically empty because production safety+tool_use
    # are usually 1.0, which floors the composite at ~0.4. Only warn for buckets
    # that have source records but didn't land in valid.
    val_buckets = split_bucket_counts(valid)
    source_buckets = {composite_bucket(c) for c in composites}
    missing = [
        b for b in ("Q1", "Q2", "Q3", "Q4")
        if b in source_buckets and val_buckets.get(b, 0) == 0
    ]
    if missing:
        print(
            f"WARNING: validation split missing buckets {missing}; "
            f"discrimination measurement will be incomplete",
            file=sys.stderr,
        )

    write_jsonl(data_dir / "train.jsonl", train)
    write_jsonl(data_dir / "valid.jsonl", valid)
    write_jsonl(data_dir / "test.jsonl", test)

    # Composite-bucket distribution across all usable records
    bucket_totals: dict[str, int] = defaultdict(int)
    for c in composites:
        bucket_totals[composite_bucket(c)] += 1

    # Content fingerprint: pair git_sha with git_dirty so downstream consumers
    # can detect a dirty-tree manifest. Hash each written JSONL so re-runs
    # without code changes but with database drift produce different hashes.
    # git_dirty falls back to "unknown" when outside a git repo (rather than
    # JSON null) so consumers don't need a special tri-state branch.
    train_path = data_dir / "train.jsonl"
    valid_path = data_dir / "valid.jsonl"
    test_path = data_dir / "test.jsonl"
    dirty = git_dirty()
    train_buckets = split_bucket_counts(train)
    test_buckets = split_bucket_counts(test)
    manifest = {
        "schema_version": 2,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "git_sha": git_sha(),
        "git_dirty": "unknown" if dirty is None else dirty,
        "content_sha256": {
            "train": sha256_file(train_path),
            "valid": sha256_file(valid_path),
            "test": sha256_file(test_path),
        },
        "seed": args.seed,
        "val_fraction": args.val_fraction,
        "test_fraction": args.test_fraction,
        "max_prompt_chars": args.max_prompt_chars,
        "floor_val_non_q5": 8,
        "floor_test_non_q5": 4,
        "source_row_count": len(rows),
        "chat_only_row_count": len(chat),
        "with_parseable_dims": len(records) + skipped_too_long,
        "skipped_no_dims": skipped_no_dims,
        "skipped_too_long": skipped_too_long,
        "usable_records": len(records),
        "bucket_totals": dict(sorted(bucket_totals.items())),
        "per_dim_variance": {
            k: dict(sorted(dim_dist[k].items())) for k in DIM_KEYS
        },
        "splits": {
            "train": {"n": len(train), "buckets": train_buckets},
            "valid": {"n": len(valid), "buckets": val_buckets},
            "test": {"n": len(test), "buckets": test_buckets},
        },
        "storage_api": {
            "endpoint": "evolution.storage.get_storage().get_recent_interactions",
            "kwargs": {"limit": 10000, "eval_suite": None, "min_score": 0.0},
            "rationale": "default limit=50 and eval_suite='runtime' both truncate the population",
        },
    }
    write_manifest(out_dir / "manifest.json", manifest)

    # Human-readable summary
    print(f"Source rows (raw): {len(rows)}")
    print(f"Chat-only after filter: {len(chat)}")
    print(f"  with parseable judge_dims:  {len(records) + skipped_too_long}")
    print(f"  skipped (no/bad dims):      {skipped_no_dims}")
    print(f"  skipped (prompt > {args.max_prompt_chars} chars): {skipped_too_long}")
    print(f"Usable records:               {len(records)}")
    print()
    print("Composite-bucket distribution:")
    for k, v in sorted(bucket_totals.items()):
        print(f"  {k}: {v}")
    print()
    print("Per-dim variance:")
    for k in DIM_KEYS:
        print(f"  {k}: {dict(sorted(dim_dist[k].items()))}")
    print()
    print(f"train  n={len(train):>4}  buckets={train_buckets}")
    print(f"valid  n={len(valid):>4}  buckets={val_buckets}")
    print(f"test   n={len(test):>4}  buckets={test_buckets}")
    print()
    print(f"Written: {data_dir}/{{train,valid,test}}.jsonl")
    print(f"Manifest: {out_dir}/manifest.json")


if __name__ == "__main__":
    main()
