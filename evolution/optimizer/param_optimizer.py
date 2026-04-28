"""
Parameter optimizer for memory retrieval thresholds.

Systematically searches for optimal values of the 5 retrieval parameters
(low_threshold, abstain_threshold, gap_threshold, top_k, rrf_k) using the
labeled benchmark as the objective function.

Extends the heuristic-based `memory_tree.py calibrate` to cover all 5 params
with provider-aware systematic search. Uses the existing benchmark() function
which keeps retrieval recall and abstain accuracy as separate metrics
(per ADR benchmark-regression-gate.md Decision 3).

No external dependencies — uses random search with smart seeding.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"

SEARCH_SPACE = {
    "low_threshold": (0.20, 0.80),
    "abstain_threshold": (0.10, 0.70),
    "gap_threshold": (0.005, 0.10),
    "top_k": (2, 10),
    "rrf_k": (20, 120),
}

INT_PARAMS = {"top_k", "rrf_k"}

BENCH_LABELS = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts" / "tests" / "fixtures" / "memory_tree_queries.jsonl"
)

DEFAULT_TRIALS = 200


def _load_labels(path: Path = BENCH_LABELS) -> list[dict[str, Any]]:
    labels = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                labels.append(json.loads(line))
    return labels


def _sample_params(rng: random.Random) -> dict[str, float | int]:
    params: dict[str, float | int] = {}
    for name, (lo, hi) in SEARCH_SPACE.items():
        if name in INT_PARAMS:
            params[name] = rng.randint(int(lo), int(hi))
        else:
            params[name] = round(rng.uniform(lo, hi), 3)
    return params


def _seed_from_defaults() -> list[dict[str, float | int]]:
    """Include current hardcoded defaults as seed candidates."""
    sys.path.insert(0, str(_SCRIPTS_DIR))
    try:
        from memory_tree import (
            DEFAULT_ABSTAIN_THRESHOLD,
            DEFAULT_LOW_THRESHOLD,
            DEFAULT_RRF_K,
            DEFAULT_SCORE_GAP_THRESHOLD,
            DEFAULT_TOP_K,
        )
        return [{
            "low_threshold": DEFAULT_LOW_THRESHOLD,
            "abstain_threshold": DEFAULT_ABSTAIN_THRESHOLD,
            "gap_threshold": DEFAULT_SCORE_GAP_THRESHOLD,
            "top_k": DEFAULT_TOP_K,
            "rrf_k": DEFAULT_RRF_K,
        }]
    except ImportError:
        return []
    finally:
        if str(_SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(_SCRIPTS_DIR))


def _open_db(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open memory_tree DB with sqlite-vec loaded."""
    sys.path.insert(0, str(_SCRIPTS_DIR))
    try:
        from memory_tree import open_db
        from pathlib import Path as _P
        return open_db(_P(db_path) if db_path else None)
    finally:
        if str(_SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(_SCRIPTS_DIR))


def _precompute_embeddings(queries: list[str]) -> dict[str, list[float]]:
    """Embed all queries once upfront. Avoids re-embedding per trial."""
    sys.path.insert(0, str(_SCRIPTS_DIR))
    try:
        from memory_tree import embed_text
        cache: dict[str, list[float]] = {}
        for q in queries:
            if q not in cache:
                cache[q] = embed_text(q)
        return cache
    finally:
        if str(_SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(_SCRIPTS_DIR))


def _run_trial(
    db: sqlite3.Connection,
    dataset: list[dict[str, Any]],
    params: dict[str, float | int],
    vec_cache: dict[str, list[float]],
) -> dict[str, Any]:
    """Run one trial: retrieve each query with candidate params, compute metrics.

    Uses cached embeddings so only DB lookups + scoring happen per trial.
    Keeps retrieval recall and abstain accuracy separate per ADR Decision 3.
    """
    sys.path.insert(0, str(_SCRIPTS_DIR))
    try:
        from memory_tree import retrieve
    finally:
        if str(_SCRIPTS_DIR) in sys.path:
            sys.path.remove(str(_SCRIPTS_DIR))

    n = len(dataset)
    recall_hits = 0
    mrr_sum = 0.0
    abstain_correct = 0
    abstain_total = 0
    by_tag: dict[str, dict[str, Any]] = {}

    k = int(params["top_k"])

    for item in dataset:
        q = item["query"]
        expected = item.get("expected_paths") or (
            [item["expected_path"]] if item.get("expected_path") else []
        )
        tag = item.get("tag", "abstain" if item.get("abstain") else "single")
        expect_abstain = bool(item.get("abstain"))

        bucket = by_tag.setdefault(tag, {"n": 0, "hits": 0, "mrr": 0.0,
                                         "abstain_correct": 0})
        bucket["n"] += 1

        result = retrieve(
            db, q,
            k=k,
            low_threshold=float(params["low_threshold"]),
            abstain_threshold=float(params["abstain_threshold"]),
            gap_threshold=float(params["gap_threshold"]),
            rrf_k=int(params["rrf_k"]),
            query_vec=vec_cache.get(q),
            use_fts=True,
        )

        returned = [r["path"] for r in result["results"]]
        hit = any(p in returned for p in expected) if expected else False

        if hit:
            recall_hits += 1
            bucket["hits"] += 1
            for idx, r in enumerate(result["results"]):
                if r["path"] in expected:
                    reciprocal = 1.0 / (idx + 1)
                    mrr_sum += reciprocal
                    bucket["mrr"] += reciprocal
                    break

        if expect_abstain:
            abstain_total += 1
            if result["fell_back"]:
                abstain_correct += 1
                bucket["abstain_correct"] += 1

    tag_report = {}
    for tag, s in by_tag.items():
        entry: dict[str, Any] = {"n": s["n"]}
        if s["n"] > 0 and not tag.startswith("abstain"):
            entry["recall_at_k"] = round(s["hits"] / s["n"], 3)
            entry["mrr_at_k"] = round(s["mrr"] / s["n"], 3)
        elif tag.startswith("abstain"):
            entry["abstain_accuracy"] = round(s["abstain_correct"] / s["n"], 3) if s["n"] else None
        tag_report[tag] = entry

    return {
        "n": n,
        "recall_at_k": round(recall_hits / n, 3) if n else 0,
        "mrr_at_k": round(mrr_sum / n, 3) if n else 0,
        "abstain_accuracy": round(abstain_correct / abstain_total, 3) if abstain_total else None,
        "by_tag": tag_report,
    }


def _score_result(result: dict[str, Any], min_abstain: float = 0.8) -> float | None:
    """Score a benchmark result. Returns None if abstain accuracy regressed.

    Per ADR benchmark-regression-gate.md Decision 3, retrieval recall and
    abstain accuracy are kept separate. We optimize for recall but constrain
    abstain accuracy to not regress below min_abstain.
    """
    if "error" in result:
        return None
    abstain_acc = result.get("abstain_accuracy")
    if abstain_acc is not None and abstain_acc < min_abstain:
        return None
    recall = result.get("recall_at_k", 0.0)
    mrr = result.get("mrr_at_k", 0.0)
    return recall * 0.8 + mrr * 0.2


def optimize_params(
    db_path: Optional[str] = None,
    trials: int = DEFAULT_TRIALS,
    seed: int = 42,
    min_abstain: float = 0.8,
    verbose: bool = True,
) -> Optional[dict[str, Any]]:
    """Run parameter optimization against the labeled benchmark.

    Returns dict with best params, scores, and metadata. None if benchmark
    data is missing or no valid trial found.
    """
    if not BENCH_LABELS.exists():
        print(f"[param-optimizer] Benchmark labels not found: {BENCH_LABELS}")
        return None

    dataset = _load_labels()
    if not dataset:
        print("[param-optimizer] No benchmark labels loaded")
        return None

    db = _open_db(db_path)

    # Pre-embed all queries once (the expensive step)
    queries = list({item["query"] for item in dataset})
    if verbose:
        print(f"[param-optimizer] Pre-embedding {len(queries)} unique queries...")
    t0 = time.monotonic()
    vec_cache = _precompute_embeddings(queries)
    if verbose:
        print(f"[param-optimizer] Embeddings cached in {time.monotonic() - t0:.1f}s")

    rng = random.Random(seed)

    candidates = _seed_from_defaults()
    for _ in range(trials - len(candidates)):
        candidates.append(_sample_params(rng))

    best_score: float = -1.0
    best_params: dict[str, float | int] = {}
    best_result: dict[str, Any] = {}
    baseline_score: float = -1.0

    t_start = time.monotonic()
    for i, params in enumerate(candidates):
        result = _run_trial(db, dataset, params, vec_cache)
        score = _score_result(result, min_abstain=min_abstain)

        if i == 0:
            recall = result.get("recall_at_k", 0.0)
            mrr = result.get("mrr_at_k", 0.0)
            baseline_score = recall * 0.8 + mrr * 0.2
            if verbose:
                print(
                    f"[param-optimizer] Baseline: recall={recall:.3f} "
                    f"abstain={result.get('abstain_accuracy', 'N/A')} "
                    f"score={baseline_score:.4f}"
                )

        if score is not None and score > best_score:
            best_score = score
            best_params = params
            best_result = result
            if verbose and i > 0:
                print(
                    f"[param-optimizer] Trial {i}/{len(candidates)}: "
                    f"new best score={score:.4f} "
                    f"recall={result.get('recall_at_k', 0):.3f}"
                )

        if verbose and (i + 1) % 50 == 0:
            elapsed = time.monotonic() - t_start
            print(f"[param-optimizer] Progress: {i + 1}/{len(candidates)} trials ({elapsed:.1f}s)")

    db.close()

    if best_score < 0:
        print("[param-optimizer] No valid trial found")
        return None

    delta = best_score - baseline_score
    total_time = time.monotonic() - t_start

    if verbose:
        print(
            f"\n[param-optimizer] Done in {total_time:.1f}s. "
            f"baseline={baseline_score:.4f} → best={best_score:.4f} "
            f"({'+'if delta >= 0 else ''}{delta:.4f})"
        )
        print(f"[param-optimizer] Best params: {json.dumps(best_params, indent=2)}")
        print(f"[param-optimizer] Recall@K: {best_result.get('recall_at_k', 0):.3f}")
        print(f"[param-optimizer] Abstain accuracy: {best_result.get('abstain_accuracy', 'N/A')}")

    return {
        "params": best_params,
        "score": best_score,
        "baseline_score": baseline_score,
        "delta": delta,
        "recall_at_k": best_result.get("recall_at_k"),
        "abstain_accuracy": best_result.get("abstain_accuracy"),
        "mrr_at_k": best_result.get("mrr_at_k"),
        "trials": len(candidates),
        "by_tag": best_result.get("by_tag"),
    }


def optimize_and_save(
    db_path: Optional[str] = None,
    trials: int = DEFAULT_TRIALS,
    provider: Optional[str] = None,
    verbose: bool = True,
    force: bool = False,
) -> Optional[str]:
    """Run optimization and save result as an evolution artifact.

    Refuses to save if optimized score is worse than baseline (unless force=True).
    Returns artifact ID on success, None on failure or regression.
    """
    result = optimize_params(db_path=db_path, trials=trials, verbose=verbose)
    if result is None:
        return None

    if result["delta"] < 0 and not force:
        if verbose:
            print(
                f"[param-optimizer] No improvement found "
                f"(delta={result['delta']:.4f}). Not saving artifact. "
                f"Use --force to save anyway."
            )
        return None

    from .artifacts import save_artifact

    provider_tag = provider or _detect_provider()
    module = f"memory_retrieval_{provider_tag}"

    aid = save_artifact(
        module=module,
        content=json.dumps(result["params"], indent=2),
        baseline_score=result["baseline_score"],
        optimized_score=result["score"],
        sample_count=result["trials"],
    )

    if verbose:
        print(f"[param-optimizer] Saved artifact {aid[:8]} for module={module}")

    return aid


def _detect_provider() -> str:
    import os
    provider = os.environ.get("EMBEDDING_PROVIDER", "auto").lower()
    if provider in ("gemini", "ollama"):
        return provider
    if provider == "auto":
        if not os.environ.get("OLLAMA_HOST") and os.environ.get("GEMINI_API_KEY"):
            return "gemini"
    return "ollama"
