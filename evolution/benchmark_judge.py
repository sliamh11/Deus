"""
Benchmark Ollama judge models against Gemini ground-truth scores.

Loads interactions already scored by GeminiRuntimeJudge from the DB, re-scores
them with each specified Ollama model, and compares accuracy, parse error rate,
and latency.

Usage:
    python3 -m evolution.benchmark_judge [--limit N] [--models m1,m2,...]
    python3 -m evolution.benchmark_judge --auto  # auto-detect all gemma4 + qwen models
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

from .db import open_db
from .judge.ollama_judge import (
    OLLAMA_HOST,
    OllamaRuntimeJudge,
    _call_ollama,
    _check_model_pulled,
    _ollama_url,
    is_ollama_available,
)


@dataclass
class EvalDetail:
    """Per-interaction evaluation detail for conflict analysis."""
    interaction_id: str
    prompt_preview: str  # first 80 chars
    ground_truth: float
    model_score: float
    rationale: str

    @property
    def delta(self) -> float:
        return abs(self.model_score - self.ground_truth)


@dataclass
class ModelResult:
    model: str
    scores: list[float] = field(default_factory=list)
    ground_truth: list[float] = field(default_factory=list)
    parse_errors: int = 0
    total: int = 0
    latencies: list[float] = field(default_factory=list)
    details: list[EvalDetail] = field(default_factory=list)

    @property
    def mae(self) -> float:
        if not self.scores:
            return float("inf")
        return statistics.mean(
            abs(s - g) for s, g in zip(self.scores, self.ground_truth)
        )

    @property
    def parse_error_rate(self) -> float:
        return self.parse_errors / self.total if self.total else 0.0

    @property
    def avg_latency(self) -> float:
        return statistics.mean(self.latencies) if self.latencies else 0.0

    @property
    def pearson(self) -> float:
        if len(self.scores) < 3:
            return 0.0
        n = len(self.scores)
        mean_s = statistics.mean(self.scores)
        mean_g = statistics.mean(self.ground_truth)
        num = sum((s - mean_s) * (g - mean_g) for s, g in zip(self.scores, self.ground_truth))
        den_s = sum((s - mean_s) ** 2 for s in self.scores) ** 0.5
        den_g = sum((g - mean_g) ** 2 for g in self.ground_truth) ** 0.5
        if den_s == 0 or den_g == 0:
            return 0.0
        return num / (den_s * den_g)

    @property
    def spearman(self) -> float:
        if len(self.scores) < 3:
            return 0.0
        n = len(self.scores)

        def _rank(vals):
            indexed = sorted(enumerate(vals), key=lambda x: x[1])
            ranks = [0.0] * n
            for rank, (orig_idx, _) in enumerate(indexed, 1):
                ranks[orig_idx] = float(rank)
            return ranks

        r_s = _rank(self.scores)
        r_g = _rank(self.ground_truth)
        d_sq = sum((a - b) ** 2 for a, b in zip(r_s, r_g))
        return 1 - (6 * d_sq) / (n * (n ** 2 - 1))


def _is_noise(prompt: str, response: str) -> bool:
    """Filter out test/setup interactions that don't represent real agent quality."""
    # System callbacks (compact acknowledgments)
    if "Compacted PreCompact" in prompt and "No response requested" in response:
        return True
    # Command no-ops
    if "/compact" in prompt and "No response requested" in response:
        return True
    # Auth errors (not a quality signal — system state issue)
    if "Not logged in" in response and "Please run /login" in response:
        return True
    # Short test messages ("בדיקה") that aren't real conversations
    if "בדיקה" in prompt and len(prompt.strip()) < 200:
        return True
    return False


def _get_scored_interactions(limit: int, clean: bool = False) -> list[dict]:
    """Load interactions with Gemini ground-truth scores from DB.

    Args:
        limit: Max number of interactions to return.
        clean: If True, exclude test/setup noise interactions.
    """
    db = open_db()
    rows = db.execute(
        """
        SELECT id, prompt, response, tools_used, judge_score, judge_dims
        FROM interactions
        WHERE judge_score IS NOT NULL
        ORDER BY timestamp DESC
        """,
    ).fetchall()
    db.close()

    results = []
    for row in rows:
        if clean and _is_noise(row["prompt"], row["response"]):
            continue
        dims = json.loads(row["judge_dims"]) if row["judge_dims"] else {}
        results.append({
            "id": row["id"],
            "prompt": row["prompt"],
            "response": row["response"],
            "tools_used": json.loads(row["tools_used"]) if row["tools_used"] else None,
            "ground_truth_score": row["judge_score"],
            "ground_truth_dims": dims,
        })
        if len(results) >= limit:
            break
    return results


def _list_ollama_models() -> list[str]:
    """List all models available in local Ollama."""
    try:
        req = urllib.request.Request(_ollama_url("/api/tags"))
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        return [m["name"] for m in data.get("models", [])]
    except (urllib.error.URLError, OSError):
        return []


def _auto_detect_models() -> list[str]:
    """Find all gemma4 and qwen variants available locally."""
    all_models = _list_ollama_models()
    return [m for m in all_models if "gemma4" in m or "qwen3.5" in m]


def benchmark(models: list[str], interactions: list[dict], verbose: bool = True) -> list[ModelResult]:
    results = []

    for model_name in models:
        if verbose:
            print(f"\n{'='*60}")
            print(f"Benchmarking: {model_name}")
            print(f"{'='*60}")

        try:
            _check_model_pulled(model_name)
        except RuntimeError as e:
            print(f"  SKIP: {e}")
            continue

        judge = OllamaRuntimeJudge(model=model_name)
        mr = ModelResult(model=model_name)

        for i, interaction in enumerate(interactions):
            mr.total += 1
            t0 = time.monotonic()

            try:
                result = judge.evaluate(
                    prompt=interaction["prompt"],
                    response=interaction["response"],
                    tools_used=interaction["tools_used"],
                )
            except Exception as e:
                if verbose:
                    print(f"  [{i+1}/{len(interactions)}] ERROR: {e}")
                mr.parse_errors += 1
                continue

            elapsed = time.monotonic() - t0
            mr.latencies.append(elapsed)

            if result.raw_response and "Parse error" in result.rationale:
                mr.parse_errors += 1

            mr.scores.append(result.score)
            mr.ground_truth.append(interaction["ground_truth_score"])
            mr.details.append(EvalDetail(
                interaction_id=interaction["id"],
                prompt_preview=interaction["prompt"][:80].replace("\n", " "),
                ground_truth=interaction["ground_truth_score"],
                model_score=result.score,
                rationale=result.rationale[:200] if result.rationale else "",
            ))

            if verbose:
                gt = interaction["ground_truth_score"]
                print(
                    f"  [{i+1}/{len(interactions)}] "
                    f"model={result.score:.2f} gt={gt:.2f} "
                    f"delta={abs(result.score - gt):+.2f} "
                    f"latency={elapsed:.1f}s"
                )

        results.append(mr)

        if verbose and mr.scores:
            print(f"\n  Summary for {model_name}:")
            print(f"    Pearson:     {mr.pearson:.3f}")
            print(f"    Spearman:    {mr.spearman:.3f}")
            print(f"    MAE:         {mr.mae:.3f}")
            print(f"    Parse errs:  {mr.parse_errors}/{mr.total} ({mr.parse_error_rate:.0%})")
            print(f"    Avg latency: {mr.avg_latency:.1f}s")

    return results


def print_comparison(results: list[ModelResult]) -> None:
    if not results:
        print("\nNo results to compare.")
        return

    # Sort by composite: correlation (40%) + inverse MAE (30%) + inverse parse rate (30%)
    def composite(r: ModelResult) -> float:
        corr = max(r.pearson, 0)
        mae_score = max(0, 1 - r.mae)
        parse_score = 1 - r.parse_error_rate
        return 0.4 * corr + 0.3 * mae_score + 0.3 * parse_score

    ranked = sorted(results, key=composite, reverse=True)

    print(f"\n{'='*80}")
    print("BENCHMARK RESULTS (ranked by composite score)")
    print(f"{'='*80}")
    print(
        f"{'Rank':<5} {'Model':<25} {'Pearson':>8} {'Spearman':>9} "
        f"{'MAE':>6} {'ParseErr':>9} {'Latency':>8} {'Composite':>10}"
    )
    print("-" * 80)

    for i, r in enumerate(ranked):
        comp = composite(r)
        marker = " ***" if i == 0 else ""
        print(
            f"{i+1:<5} {r.model:<25} {r.pearson:>8.3f} {r.spearman:>9.3f} "
            f"{r.mae:>6.3f} {r.parse_errors:>4}/{r.total:<4} "
            f"{r.avg_latency:>7.1f}s {comp:>9.3f}{marker}"
        )

    print(f"\n*** Winner: {ranked[0].model}")

    # Hardware-aware recommendation
    _print_hardware_recommendation(ranked[0])


def _detect_hardware() -> dict:
    """Detect system hardware for model recommendations."""
    import platform
    import subprocess

    hw = {"os": platform.system(), "arch": platform.machine()}

    if hw["os"] == "Darwin":
        try:
            ram = int(subprocess.check_output(["sysctl", "-n", "hw.memsize"]).strip())
            hw["ram_gb"] = ram / (1024 ** 3)
        except (subprocess.SubprocessError, ValueError):
            hw["ram_gb"] = 0
        try:
            cores = int(subprocess.check_output(["sysctl", "-n", "hw.ncpu"]).strip())
            hw["cores"] = cores
        except (subprocess.SubprocessError, ValueError):
            hw["cores"] = 0
        # Check for Apple Silicon (unified memory = GPU memory)
        hw["gpu"] = "apple_silicon" if hw["arch"] == "arm64" else "none"
    else:
        try:
            import os as _os
            hw["cores"] = _os.cpu_count() or 0
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        hw["ram_gb"] = int(line.split()[1]) / (1024 ** 2)
                        break
        except (OSError, ValueError):
            hw["ram_gb"] = 0
            hw["cores"] = 0
        hw["gpu"] = "unknown"

    return hw


# Model size estimates (download size in GB)
_MODEL_SIZES = {
    "gemma4:e2b": 7.2,
    "gemma4:e4b": 9.6,
    "gemma4:26b": 18.0,
    "gemma4:31b": 20.0,
    "qwen3.5:4b": 3.4,
}


def _print_hardware_recommendation(winner: "ModelResult") -> None:
    """Print hardware-aware model recommendation."""
    hw = _detect_hardware()
    ram = hw.get("ram_gb", 0)

    print(f"\n{'='*80}")
    print("HARDWARE-AWARE RECOMMENDATION")
    print(f"{'='*80}")
    print(f"  System: {hw.get('os', '?')} {hw.get('arch', '?')} | "
          f"RAM: {ram:.0f} GB | Cores: {hw.get('cores', '?')} | "
          f"GPU: {hw.get('gpu', '?')}")

    # Recommend based on RAM (model needs ~1.2x its size in RAM to run)
    viable = {k: v for k, v in _MODEL_SIZES.items() if v * 1.2 <= ram}
    if viable:
        largest_viable = max(viable, key=viable.get)
        print(f"  Largest viable model for this machine: {largest_viable} "
              f"({viable[largest_viable]:.1f} GB)")
        if winner.model in viable:
            print(f"  Benchmark winner ({winner.model}) fits this machine.")
        else:
            print(f"  Benchmark winner ({winner.model}) may not fit. "
                  f"Best alternative: {largest_viable}")
    else:
        print("  WARNING: RAM too low for any recommended model.")

    print(f"\n  To apply winner: export OLLAMA_MODEL={winner.model}")


def print_conflicts(results: list[ModelResult], threshold: float = 0.2) -> None:
    """Print interactions where models disagree with ground truth by more than threshold."""
    conflicts = []
    for r in results:
        for d in r.details:
            if d.delta > threshold:
                conflicts.append((r.model, d))

    if not conflicts:
        print(f"\nNo conflicts found (threshold: {threshold:.1f})")
        return

    print(f"\n{'='*80}")
    print(f"CONFLICTS (model vs Gemini delta > {threshold:.1f}) — needs human review")
    print(f"{'='*80}")

    # Group by interaction
    by_interaction: dict[str, list[tuple[str, EvalDetail]]] = {}
    for model, d in conflicts:
        by_interaction.setdefault(d.interaction_id, []).append((model, d))

    for iid, entries in by_interaction.items():
        d0 = entries[0][1]
        print(f"\n  Interaction: {iid}")
        print(f"  Prompt: {d0.prompt_preview}...")
        print(f"  Gemini score: {d0.ground_truth:.2f}")
        for model, d in entries:
            print(f"    {model:<25} scored {d.model_score:.2f} (delta {d.delta:+.2f})")
            if d.rationale:
                print(f"      Rationale: {d.rationale[:120]}...")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark Ollama judge models against Gemini ground truth")
    parser.add_argument("--limit", type=int, default=20, help="Number of interactions to benchmark (default: 20)")
    parser.add_argument("--models", type=str, default=None, help="Comma-separated model names (default: auto-detect)")
    parser.add_argument("--auto", action="store_true", help="Auto-detect all gemma4 + qwen models")
    parser.add_argument("--quiet", action="store_true", help="Only show final comparison table")
    parser.add_argument("--conflicts", type=float, default=0.2, metavar="THRESHOLD",
                        help="Show conflicts where delta > threshold (default: 0.2)")
    parser.add_argument("--clean", action="store_true",
                        help="Exclude test/setup noise interactions from benchmark")
    args = parser.parse_args()

    if not is_ollama_available():
        print("ERROR: Ollama is not reachable. Start it with: ollama serve")
        sys.exit(1)

    # Determine models to benchmark
    if args.models:
        models = [m.strip() for m in args.models.split(",")]
    else:
        models = _auto_detect_models()
        if not models:
            print("ERROR: No gemma4 or qwen models found. Pull some first:")
            print("  ollama pull gemma4:e4b")
            print("  ollama pull gemma4:26b")
            sys.exit(1)

    print(f"Models to benchmark: {', '.join(models)}")

    # Load ground-truth interactions
    interactions = _get_scored_interactions(args.limit, clean=args.clean)
    if not interactions:
        print("ERROR: No scored interactions found in DB. Run backfill first:")
        print("  python3 -m evolution.backfill --limit 20")
        sys.exit(1)

    print(f"Loaded {len(interactions)} interactions with Gemini ground-truth scores.\n")

    # Run benchmark
    results = benchmark(models, interactions, verbose=not args.quiet)

    # Print comparison
    print_comparison(results)

    # Print conflicts for human review
    print_conflicts(results, threshold=args.conflicts)


if __name__ == "__main__":
    main()
