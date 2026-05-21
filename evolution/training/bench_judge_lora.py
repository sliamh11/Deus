"""Judge-LoRA post-training bench — Adapter vs Base on the held-out test split.

Step 3/4 of the judge-LoRA pipeline (plan at
~/.claude/plans/eventual-meandering-bear.md, plan-reviewer SHIP'd Round 2).
Consumes the step-1 stratified test split + the step-2 trained adapter and
produces (a) a per-dimension comparison table on stdout and (b) a results
JSON artifact that feeds step-4 (the ADR `judge-lora-specialization.md`)
and the PR #1.5 conditional rationale regeneration.

The earlier n=50 stratified bench (2026-05-17) measured Gemma-3n-E4B-Q8_0
(Ollama) vs Gemini and surfaced a 0.163 Pearson gap. The LoRA was trained
to close that gap. This bench is the empirical answer.

## Platform constraints

**macOS / Apple Silicon only — requires `mlx` + Metal GPU. Not portable to
Linux/x86.** mlx_lm and mlx.core.metal cleanup are mac-specific. The bench
fails fast on other platforms because `mlx_lm` cannot be imported there —
the `with_mlx=True` branch of `preflight()` (and the lazy import at the top
of `run_bench`) surface `ImportError` → `SystemExit` immediately. There is
no explicit `sys.platform` guard; the import failure IS the guard.

## Inference design (in-process)

Both backends load the same base model. The Adapter backend additionally
applies the trained LoRA via `mlx_lm.load(..., adapter_path=...)`. Each
backend runs ONCE, processes all 40 records sequentially, then explicit
`del model + gc.collect() + mx.metal.clear_cache()` frees Metal before the
next backend loads.

Determinism: `mx.random.seed(seed)` is called ONCE per backend BEFORE the
generate loop. `seed` is NOT passed to `mlx_lm.generate` — that kwarg is not
accepted by `generate_step` and would raise TypeError. CLI canonical usage:
`mlx_lm/generate.py:1970-1971`. For temperature: `temp` is also NOT a valid
kwarg on `mlx_lm.generate` (verified empirically against mlx_lm 0.31.3 —
`generate_step` signature has no `temp` parameter). Instead, build a sampler
via `make_sampler(temp=temp, ...)` (signature at `mlx_lm/sample_utils.py`)
and pass `sampler=...` to `generate`. `temp=0.0` ⇒ greedy argmax.

## Lazy import boundary

`mlx_lm` and `mlx.core` are imported INSIDE `run_bench()` and (optionally)
`preflight()`, NEVER at module top. This lets `--dry-run` and the
import-only path of `--pre-flight-only` run on host Python without the
judge-lora venv installed.

## Output

JSON results land at `finetune/judge-lora-gemma3n/bench/<adapter-run-id>-vs-base-<UTC>.json`.
The path is gitignored (the entire `finetune/judge-lora-gemma3n/` subtree is
gitignored at the repo level).

## Composite metric note

Composite is dominated by parse_error_rate when base parse failures are
>20% — the comparison table reports mean Pearson, mean MAE, and
parse_error_rate as separate headline columns so the composite alone never
drives the verdict. The verdict line uses mean Pearson delta directly.

## Developer note

The unit tests for this module use `monkeypatch` to stub `mlx_lm.load`,
`mlx_lm.generate`, `mlx.core.random.seed`, `gc.collect`, and
`mlx.core.metal.clear_cache`. They do NOT exercise real-model inference.
Before pushing changes that touch the inference path you MUST run
`bench_judge_lora.py --limit 2 --skip-base` locally; that is the only
catch-net for `mlx_lm` API drift inside this module.
"""
from __future__ import annotations

import argparse
import functools
import gc
import hashlib
import json
import os
import re
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))


# ---------- Defaults --------------------------------------------------------

# Default base model: 4-bit Gemma-3n-E4B text-only. Same model the adapter
# was trained on (locked in training_manifest.json under step-2). Using a
# different base would invalidate the comparison.
DEFAULT_BASE_MODEL = "mlx-community/gemma-3n-E4B-it-lm-4bit"

DEFAULT_DATASET_DIR = PROJECT_ROOT / "finetune/judge-lora-gemma3n"
DEFAULT_TEST_JSONL = DEFAULT_DATASET_DIR / "data/test.jsonl"
DEFAULT_ADAPTERS_ROOT = DEFAULT_DATASET_DIR / "adapters"
DEFAULT_BENCH_OUT_DIR = DEFAULT_DATASET_DIR / "bench"

DEFAULT_MAX_TOKENS = 256
DEFAULT_TEMP = 0.0
DEFAULT_SEED = 20260518

# Judge rubric dimensions (same as evolution/judge/ollama_judge.py and the
# step-1 dataset builder). Order matters: it's the canonical iteration order
# for tables + JSON output.
DIMENSIONS: tuple[str, ...] = (
    "quality",
    "safety",
    "tool_use",
    "personalization",
)

VENV = Path(
    os.environ.get("JUDGE_LORA_VENV", "~/deus/.venvs/judge-lora/bin/python3")
).expanduser()


# ---------- Data classes ----------------------------------------------------


@dataclass
class BenchRecord:
    """One scored record from one backend."""
    interaction_idx: int
    prompt_preview: str
    ground_truth: dict[str, float]
    generated_scores: dict[str, float]
    parse_ok: bool
    parse_error: str | None
    latency_s: float
    raw_generated: str  # first 400 chars for debug


@dataclass
class BackendResult:
    """Aggregated results for one backend (Adapter or Base).

    The metric properties below are `cached_property` so each one is computed
    AT MOST ONCE per instance. This eliminates the recomputation chain that
    arises when `print_comparison` and `save_results_json` both access
    `pearson_per_dim`, `mean_pearson`, etc. several times. Safe because
    `records` is only ever appended to inside `run_bench` and never mutated
    after that function returns; properties are only accessed downstream.
    """
    name: str
    base_model: str
    adapter_path: str | None
    records: list[BenchRecord] = field(default_factory=list)
    load_time_s: float = 0.0

    @property
    def n(self) -> int:
        return len(self.records)

    @functools.cached_property
    def parse_error_rate(self) -> float:
        if not self.records:
            return 0.0
        return sum(1 for r in self.records if not r.parse_ok) / len(self.records)

    @functools.cached_property
    def avg_latency_s(self) -> float:
        if not self.records:
            return 0.0
        return statistics.mean(r.latency_s for r in self.records)

    @functools.cached_property
    def pearson_per_dim(self) -> dict[str, float]:
        return {
            dim: _pearson(
                [r.generated_scores.get(dim, 0.5) for r in self.records],
                [r.ground_truth.get(dim, 0.5) for r in self.records],
            )
            for dim in DIMENSIONS
        }

    @functools.cached_property
    def spearman_per_dim(self) -> dict[str, float]:
        return {
            dim: _spearman(
                [r.generated_scores.get(dim, 0.5) for r in self.records],
                [r.ground_truth.get(dim, 0.5) for r in self.records],
            )
            for dim in DIMENSIONS
        }

    @functools.cached_property
    def mae_per_dim(self) -> dict[str, float]:
        return {
            dim: _mae(
                [r.generated_scores.get(dim, 0.5) for r in self.records],
                [r.ground_truth.get(dim, 0.5) for r in self.records],
            )
            for dim in DIMENSIONS
        }

    @functools.cached_property
    def mean_pearson(self) -> float:
        vals = list(self.pearson_per_dim.values())
        return statistics.mean(vals) if vals else 0.0

    @functools.cached_property
    def mean_mae(self) -> float:
        vals = list(self.mae_per_dim.values())
        return statistics.mean(vals) if vals else 0.0

    @functools.cached_property
    def composite(self) -> float:
        """0.4 * mean_pearson + 0.3 * (1 - mean_mae) + 0.3 * (1 - parse_err).

        Same weighting as evolution/benchmark_judge.py:252-256. Reported for
        ranking convenience, but the headline verdict line uses mean_pearson
        delta directly to avoid the parse-error-domination trap.
        """
        corr = max(self.mean_pearson, 0.0)
        mae_score = max(0.0, 1.0 - self.mean_mae)
        parse_score = 1.0 - self.parse_error_rate
        return 0.4 * corr + 0.3 * mae_score + 0.3 * parse_score


# ---------- Pure metric helpers --------------------------------------------


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation. Returns 0.0 when n < 3 or zero variance (same
    convention as evolution/benchmark_judge.py:76-87)."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return 0.0
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    den_x = sum((x - mean_x) ** 2 for x in xs) ** 0.5
    den_y = sum((y - mean_y) ** 2 for y in ys) ** 0.5
    if den_x == 0 or den_y == 0:
        return 0.0
    return num / (den_x * den_y)


def _spearman(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation. Returns 0.0 when n < 3 (same convention as
    evolution/benchmark_judge.py:89-105)."""
    n = len(xs)
    if n < 3 or n != len(ys):
        return 0.0

    def _rank(vals: list[float]) -> list[float]:
        indexed = sorted(enumerate(vals), key=lambda kv: kv[1])
        ranks = [0.0] * n
        for rank, (orig_idx, _) in enumerate(indexed, 1):
            ranks[orig_idx] = float(rank)
        return ranks

    r_x = _rank(xs)
    r_y = _rank(ys)
    d_sq = sum((a - b) ** 2 for a, b in zip(r_x, r_y))
    return 1 - (6 * d_sq) / (n * (n * n - 1))


def _mae(xs: list[float], ys: list[float]) -> float:
    """Mean absolute error. Returns 0.0 when empty."""
    if not xs or len(xs) != len(ys):
        return 0.0
    return statistics.mean(abs(x - y) for x, y in zip(xs, ys))


# ---------- Parsing --------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*", re.IGNORECASE)
_JSON_BLOCK_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def parse_judge_response(raw: str) -> tuple[dict[str, float], str | None]:
    """Extract the 4-dimension score dict from a model-generated string.

    Mirrors evolution/judge/ollama_judge.py:_parse_result behaviour (strips
    markdown fences, attempts json.loads of first {...} block, casts to
    float, clamps to [0.0, 1.0], falls back to 0.5 neutral on failure).

    Returns (scores_dict, error_msg). `error_msg` is None on success. On
    partial parse (e.g. missing dim) the dict is filled with 0.5 fallback
    and `error_msg` describes which fields were missing/invalid. On total
    failure all dims default to 0.5 and `error_msg` carries the reason.
    """
    if not raw or not raw.strip():
        return ({d: 0.5 for d in DIMENSIONS}, "empty response")

    # Strip ``` / ```json fences but keep the content
    cleaned = _FENCE_RE.sub("", raw).strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()

    # Try direct json.loads first, then fall back to first {...} block
    parsed: dict | None = None
    try:
        candidate = json.loads(cleaned)
        if isinstance(candidate, dict):
            parsed = candidate
    except json.JSONDecodeError:
        match = _JSON_BLOCK_RE.search(cleaned)
        if match:
            try:
                candidate = json.loads(match.group(0))
                if isinstance(candidate, dict):
                    parsed = candidate
            except json.JSONDecodeError:
                pass

    if parsed is None:
        return ({d: 0.5 for d in DIMENSIONS}, "no parseable JSON object found")

    scores: dict[str, float] = {}
    missing: list[str] = []
    for dim in DIMENSIONS:
        v = parsed.get(dim)
        if v is None:
            missing.append(dim)
            scores[dim] = 0.5
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            missing.append(f"{dim}(not-float)")
            scores[dim] = 0.5
            continue
        # Clamp to [0.0, 1.0]
        scores[dim] = max(0.0, min(1.0, f))

    err = None
    if missing:
        err = f"missing/invalid: {', '.join(missing)}"
    return scores, err


# ---------- Loaders --------------------------------------------------------


def load_test_records(path: Path, limit: int = 0) -> list[dict[str, Any]]:
    """Read test.jsonl and validate the messages[0]=user, messages[1]=assistant
    shape. Returns [{"user_prompt": str, "ground_truth": {q,s,t,p}}, ...].

    Raises ValueError on the FIRST malformed record (fail-fast — the
    upstream dataset builder enforces the shape so any drift is a bug).
    """
    if not path.exists():
        raise FileNotFoundError(f"test split not found: {path}")
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}:{line_no} invalid JSON: {e}") from e
            msgs = obj.get("messages")
            if not isinstance(msgs, list) or len(msgs) != 2:
                raise ValueError(
                    f"{path}:{line_no} expected messages: [user, assistant], got {msgs!r}"
                )
            user, assistant = msgs[0], msgs[1]
            if user.get("role") != "user" or assistant.get("role") != "assistant":
                raise ValueError(
                    f"{path}:{line_no} messages role mismatch (user/assistant required)"
                )
            user_prompt = user.get("content")
            if not isinstance(user_prompt, str):
                raise ValueError(f"{path}:{line_no} user content not a string")
            gt_raw = assistant.get("content")
            if not isinstance(gt_raw, str):
                raise ValueError(f"{path}:{line_no} assistant content not a string")
            gt_scores, gt_err = parse_judge_response(gt_raw)
            if gt_err is not None:
                raise ValueError(
                    f"{path}:{line_no} ground-truth unparseable ({gt_err}); "
                    f"raw={gt_raw[:200]!r}"
                )
            records.append({"user_prompt": user_prompt, "ground_truth": gt_scores})
            if limit and len(records) >= limit:
                break
    if not records:
        raise ValueError(f"{path} contained zero records")
    return records


def resolve_default_adapter_path(adapters_root: Path) -> Path:
    """Pick the lexicographically-largest run dir under adapters_root. The
    step-2 driver writes run IDs of the form `<UTC-compact>-<git-sha>[-dirty]`,
    which sort correctly by recency. Validates the chosen dir contains the
    final-iteration adapter + config."""
    if not adapters_root.exists() or not adapters_root.is_dir():
        raise FileNotFoundError(
            f"adapters root missing: {adapters_root}. Run step-2 (train_judge_lora.py) first."
        )
    candidates = sorted(
        (p for p in adapters_root.iterdir() if p.is_dir()),
        key=lambda p: p.name,
    )
    if not candidates:
        raise FileNotFoundError(
            f"no adapter directories under {adapters_root}. Run step-2 first."
        )
    chosen = candidates[-1]
    final_weights = chosen / "adapters.safetensors"
    adapter_config = chosen / "adapter_config.json"
    if not final_weights.exists():
        raise FileNotFoundError(
            f"adapter dir {chosen} is missing adapters.safetensors — "
            f"training may have aborted mid-run."
        )
    if not adapter_config.exists():
        raise FileNotFoundError(
            f"adapter dir {chosen} is missing adapter_config.json — "
            f"training may have aborted mid-run."
        )
    return chosen


# ---------- Bench runner ---------------------------------------------------


def run_bench(
    *,
    backend_name: str,
    base_model: str,
    adapter_path: Path | None,
    records: list[dict[str, Any]],
    max_tokens: int,
    temp: float,
    seed: int,
    quiet: bool = False,
    cleanup_after: bool = True,
) -> BackendResult:
    """Load model (with optional adapter) and score each record.

    Lazy-imports mlx + mlx_lm — these only exist in the judge-lora venv.

    `cleanup_after=True` (default) explicitly frees Metal state after the
    inference loop so a follow-up backend can load without OOMing. Set
    False for the LAST backend in a sequence (cleanup is a no-op at that
    point but cheaper to skip).
    """
    try:
        import mlx.core as mx
        import mlx_lm
        from mlx_lm.sample_utils import make_sampler
    except ImportError as e:
        raise SystemExit(
            f"[bench] mlx_lm import failed ({e}). Run inside the judge-lora venv: "
            f"{VENV} -m evolution.training.bench_judge_lora ..."
        )

    if not quiet:
        adapter_str = str(adapter_path) if adapter_path else "(none)"
        print(
            f"[bench] Loading backend={backend_name} model={base_model} adapter={adapter_str}",
            flush=True,
        )
    t_load_start = time.monotonic()
    model, tok = mlx_lm.load(
        base_model,
        adapter_path=str(adapter_path) if adapter_path else None,
    )
    load_time_s = time.monotonic() - t_load_start
    if not quiet:
        print(f"[bench] Loaded in {load_time_s:.1f}s", flush=True)

    # Set seed ONCE before the loop (NOT inside generate kwargs — that kwarg
    # would raise TypeError per mlx_lm/generate.py:307). Canonical CLI
    # pattern at mlx_lm/generate.py:1971.
    mx.random.seed(seed)

    # Build sampler ONCE — make_sampler(temp=0.0) returns a callable that
    # picks argmax (greedy). Passing temp= directly to mlx_lm.generate raises
    # TypeError; we must pass sampler= which generate_step does accept.
    sampler = make_sampler(temp=temp)

    result = BackendResult(
        name=backend_name,
        base_model=base_model,
        adapter_path=str(adapter_path) if adapter_path else None,
        load_time_s=load_time_s,
    )

    for idx, rec in enumerate(records, 1):
        user_prompt = rec["user_prompt"]
        ground_truth = rec["ground_truth"]
        prompt_str = tok.apply_chat_template(
            [{"role": "user", "content": user_prompt}],
            add_generation_prompt=True,
            tokenize=False,
        )
        t0 = time.monotonic()
        # NB: NO seed kwarg, NO temp kwarg here — both would raise TypeError
        # against mlx_lm 0.31.3 (`generate_step` signature has neither).
        # Determinism is via `mx.random.seed` above; temperature is via the
        # sampler built from `make_sampler(temp=...)`.
        raw = mlx_lm.generate(
            model,
            tok,
            prompt_str,
            max_tokens=max_tokens,
            sampler=sampler,
            verbose=False,
        )
        latency_s = time.monotonic() - t0
        scores, err = parse_judge_response(raw)
        preview = user_prompt[:80].replace("\n", " ")
        if len(user_prompt) > 80:
            preview += "..."
        record = BenchRecord(
            interaction_idx=idx - 1,
            prompt_preview=preview,
            ground_truth=ground_truth,
            generated_scores=scores,
            parse_ok=(err is None),
            parse_error=err,
            latency_s=latency_s,
            raw_generated=raw[:400] if isinstance(raw, str) else str(raw)[:400],
        )
        result.records.append(record)
        if not quiet:
            gen_str = " ".join(f"{d[0]}={scores[d]:.2f}" for d in DIMENSIONS)
            gt_str = " ".join(f"{d[0]}={ground_truth[d]:.2f}" for d in DIMENSIONS)
            err_str = "" if err is None else f" err={err[:40]!r}"
            print(
                f"[{idx}/{len(records)}] {backend_name} {gen_str} | gt {gt_str} | "
                f"{latency_s:.1f}s{err_str}",
                flush=True,
            )

    if cleanup_after:
        del model
        del tok
        gc.collect()
        try:
            mx.metal.clear_cache()
        except (AttributeError, RuntimeError):
            # Non-Metal builds (shouldn't happen on this platform, but safe)
            pass
        if not quiet:
            print(
                f"[bench] Cleaned up Metal state after backend={backend_name}",
                flush=True,
            )

    return result


# ---------- Output ---------------------------------------------------------


def print_comparison(
    adapter_result: BackendResult,
    base_result: BackendResult | None,
) -> None:
    """Per-dimension table + headline aggregate + verdict line driven by
    mean Pearson delta (not composite)."""
    print("\n" + "=" * 96)
    print(f"BENCH RESULTS — n={adapter_result.n} records")
    print(f"  Adapter: {adapter_result.adapter_path}")
    if base_result is not None:
        print(f"  Base:    {base_result.base_model}")
    else:
        print("  Base:    (skipped via --skip-base)")
    print("=" * 96)

    # Per-dim table
    if base_result is not None:
        print(
            f"\n{'Dimension':<18} {'Adp Pearson':>12} {'Base Pearson':>13} "
            f"{'ΔPearson':>10} {'Adp MAE':>9} {'Base MAE':>10} {'ΔMAE':>8}"
        )
        print("-" * 90)
        for dim in DIMENSIONS:
            ap = adapter_result.pearson_per_dim[dim]
            bp = base_result.pearson_per_dim[dim]
            am = adapter_result.mae_per_dim[dim]
            bm = base_result.mae_per_dim[dim]
            print(
                f"{dim:<18} {ap:>12.3f} {bp:>13.3f} {ap-bp:>+10.3f} "
                f"{am:>9.3f} {bm:>10.3f} {am-bm:>+8.3f}"
            )
    else:
        print(f"\n{'Dimension':<18} {'Adp Pearson':>12} {'Adp Spearman':>13} {'Adp MAE':>9}")
        print("-" * 60)
        for dim in DIMENSIONS:
            print(
                f"{dim:<18} {adapter_result.pearson_per_dim[dim]:>12.3f} "
                f"{adapter_result.spearman_per_dim[dim]:>13.3f} "
                f"{adapter_result.mae_per_dim[dim]:>9.3f}"
            )

    # Headline row — independent metrics so composite alone can't drive the verdict
    print("\n" + "-" * 96)
    print(
        f"{'Backend':<10} {'Mean Pearson':>14} {'Mean MAE':>10} {'Parse Err':>11} "
        f"{'Composite':>11} {'Avg Latency':>13}"
    )
    print("-" * 96)
    print(
        f"{'Adapter':<10} {adapter_result.mean_pearson:>14.3f} "
        f"{adapter_result.mean_mae:>10.3f} {adapter_result.parse_error_rate:>10.1%} "
        f"{adapter_result.composite:>11.3f} {adapter_result.avg_latency_s:>12.1f}s"
    )
    if base_result is not None:
        print(
            f"{'Base':<10} {base_result.mean_pearson:>14.3f} "
            f"{base_result.mean_mae:>10.3f} {base_result.parse_error_rate:>10.1%} "
            f"{base_result.composite:>11.3f} {base_result.avg_latency_s:>12.1f}s"
        )

    # Verdict — driven by mean Pearson delta, not composite (see module docstring)
    print()
    if base_result is None:
        print("No base comparison (--skip-base). Use a full run to draw a verdict.")
    else:
        pearson_delta = adapter_result.mean_pearson - base_result.mean_pearson
        parse_delta = base_result.parse_error_rate - adapter_result.parse_error_rate
        if pearson_delta > 0:
            print(
                f"VERDICT: Adapter improves mean Pearson by {pearson_delta:+.3f} "
                f"(parse_error_rate {base_result.parse_error_rate:.1%} → "
                f"{adapter_result.parse_error_rate:.1%}, Δ={parse_delta:+.1%})."
            )
        else:
            print(
                f"VERDICT: Adapter does NOT improve mean Pearson "
                f"(Δ={pearson_delta:+.3f}). Inspect per-dim columns above. "
                f"Parse-error rate changed from "
                f"{base_result.parse_error_rate:.1%} → {adapter_result.parse_error_rate:.1%}."
            )
    print(
        "Note: 'composite' is dominated by parse_error_rate when base parse failures >20%. "
        "The verdict line uses mean Pearson delta to avoid that trap."
    )


def save_results_json(
    adapter_result: BackendResult,
    base_result: BackendResult | None,
    output_path: Path,
    *,
    base_model: str,
    test_jsonl_path: Path,
    test_jsonl_sha256: str,
    seed: int,
) -> None:
    """Write the bench artifact atomically (tmp + rename)."""
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _serialize_backend(br: BackendResult) -> dict:
        return {
            "name": br.name,
            "base_model": br.base_model,
            "adapter_path": br.adapter_path,
            "n": br.n,
            "load_time_s": br.load_time_s,
            "avg_latency_s": br.avg_latency_s,
            "parse_error_rate": br.parse_error_rate,
            "mean_pearson": br.mean_pearson,
            "mean_mae": br.mean_mae,
            "composite": br.composite,
            "pearson_per_dim": br.pearson_per_dim,
            "spearman_per_dim": br.spearman_per_dim,
            "mae_per_dim": br.mae_per_dim,
            "records": [asdict(r) for r in br.records],
        }

    payload: dict[str, Any] = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "base_model": base_model,
        "test_jsonl_path": str(test_jsonl_path),
        "test_jsonl_sha256": test_jsonl_sha256,
        "seed": seed,
        "dimensions": list(DIMENSIONS),
        "backends": [_serialize_backend(adapter_result)],
    }
    if base_result is not None:
        payload["backends"].append(_serialize_backend(base_result))
        payload["verdict"] = {
            "mean_pearson_delta": (
                adapter_result.mean_pearson - base_result.mean_pearson
            ),
            "parse_error_rate_delta": (
                adapter_result.parse_error_rate - base_result.parse_error_rate
            ),
        }
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=False)
    tmp.replace(output_path)


# ---------- Pre-flight + main ---------------------------------------------


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def preflight(test_jsonl: Path, adapter_path: Path, *, with_mlx: bool) -> None:
    """Asserts test split + adapter dir are valid. If `with_mlx`, also confirms
    mlx_lm is importable in the current interpreter."""
    if not test_jsonl.exists():
        raise SystemExit(f"[preflight] test split missing: {test_jsonl}")
    # Validate by loading the first record
    try:
        load_test_records(test_jsonl, limit=1)
    except (ValueError, FileNotFoundError) as e:
        raise SystemExit(f"[preflight] test split invalid: {e}")
    if not adapter_path.exists():
        raise SystemExit(f"[preflight] adapter dir missing: {adapter_path}")
    if not (adapter_path / "adapters.safetensors").exists():
        raise SystemExit(
            f"[preflight] adapter dir missing adapters.safetensors: {adapter_path}"
        )
    if not (adapter_path / "adapter_config.json").exists():
        raise SystemExit(
            f"[preflight] adapter dir missing adapter_config.json: {adapter_path}"
        )
    if with_mlx:
        try:
            import mlx_lm  # noqa: F401
            import mlx.core  # noqa: F401
        except ImportError as e:
            raise SystemExit(
                f"[preflight] mlx_lm/mlx unavailable in current interpreter "
                f"({e}). Run via the judge-lora venv: {VENV}"
            )
    print("[preflight] passed.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bench the judge-LoRA adapter against the base Gemma-3n model on "
            "the held-out test split. macOS / Apple Silicon only (requires mlx)."
        )
    )
    parser.add_argument(
        "--base-model", default=DEFAULT_BASE_MODEL,
        help=f"Base model (default: {DEFAULT_BASE_MODEL})",
    )
    parser.add_argument(
        "--adapter-path", default=None,
        help="Adapter dir; default: latest under finetune/judge-lora-gemma3n/adapters/",
    )
    parser.add_argument(
        "--test-jsonl", default=str(DEFAULT_TEST_JSONL),
        help=f"Test split (default: {DEFAULT_TEST_JSONL})",
    )
    parser.add_argument(
        "--adapters-root", default=str(DEFAULT_ADAPTERS_ROOT),
        help="Where to look for adapter dirs when --adapter-path is omitted",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="Process only first N records (0 = all)",
    )
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument(
        "--temp", type=float, default=DEFAULT_TEMP,
        help="Sampling temperature; 0.0 = greedy (default)",
    )
    parser.add_argument(
        "--seed", type=int, default=DEFAULT_SEED,
        help="Passed to mx.random.seed before each backend loop",
    )
    parser.add_argument(
        "--output-json", default=None,
        help="Results JSON path; default: finetune/judge-lora-gemma3n/bench/<run-id>-vs-base-<UTC>.json",
    )
    parser.add_argument(
        "--skip-base", action="store_true",
        help="Run Adapter backend only (useful for iteration)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress per-record output",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print resolved config + adapter dir contents and exit 0 (no mlx_lm import)",
    )
    parser.add_argument(
        "--pre-flight-only", action="store_true",
        help="Run preflight checks and exit 0 (asserts mlx_lm importable)",
    )

    args = parser.parse_args(argv)

    # Validate ranges that argparse can't express tersely
    if args.temp < 0.0:
        raise SystemExit(
            f"--temp must be >= 0.0 (got {args.temp}). Use 0.0 for greedy decoding."
        )
    if args.max_tokens < 1:
        raise SystemExit(f"--max-tokens must be >= 1 (got {args.max_tokens}).")
    if args.limit < 0:
        raise SystemExit(f"--limit must be >= 0 (got {args.limit}). 0 means 'all'.")

    test_jsonl = Path(args.test_jsonl).expanduser().resolve()
    adapters_root = Path(args.adapters_root).expanduser().resolve()

    # Resolve adapter
    if args.adapter_path:
        adapter_path = Path(args.adapter_path).expanduser().resolve()
        if not adapter_path.exists():
            raise SystemExit(f"--adapter-path does not exist: {adapter_path}")
    else:
        try:
            adapter_path = resolve_default_adapter_path(adapters_root)
        except FileNotFoundError as e:
            raise SystemExit(f"[bench] {e}")

    if args.dry_run:
        print("=== bench_judge_lora.py --dry-run ===")
        print(f"base_model:     {args.base_model}")
        print(f"adapter_path:   {adapter_path}")
        print(f"test_jsonl:     {test_jsonl}")
        print(f"limit:          {args.limit or 'all'}")
        print(f"max_tokens:     {args.max_tokens}")
        print(f"temp:           {args.temp}")
        print(f"seed:           {args.seed}")
        print(f"skip_base:      {args.skip_base}")
        print(f"venv:           {VENV}")
        if adapter_path.exists():
            print("adapter dir contents:")
            for p in sorted(adapter_path.iterdir()):
                print(f"  {p.name}  ({p.stat().st_size} bytes)")
        return 0

    if args.pre_flight_only:
        preflight(test_jsonl, adapter_path, with_mlx=True)
        return 0

    # Real run
    preflight(test_jsonl, adapter_path, with_mlx=True)

    print(f"[bench] Loading test split from {test_jsonl}")
    records = load_test_records(test_jsonl, limit=args.limit)
    test_sha = sha256_file(test_jsonl)
    print(f"[bench] Loaded {len(records)} records (sha256={test_sha[:12]}...)")

    adapter_result = run_bench(
        backend_name="adapter",
        base_model=args.base_model,
        adapter_path=adapter_path,
        records=records,
        max_tokens=args.max_tokens,
        temp=args.temp,
        seed=args.seed,
        quiet=args.quiet,
        cleanup_after=not args.skip_base,  # only clean up if base is coming next
    )

    base_result: BackendResult | None = None
    if not args.skip_base:
        base_result = run_bench(
            backend_name="base",
            base_model=args.base_model,
            adapter_path=None,
            records=records,
            max_tokens=args.max_tokens,
            temp=args.temp,
            seed=args.seed,
            quiet=args.quiet,
            cleanup_after=False,  # last backend, no follow-up
        )

    # Print comparison
    print_comparison(adapter_result, base_result)

    # Save JSON
    if args.output_json:
        output_path = Path(args.output_json).expanduser().resolve()
    else:
        run_id = adapter_path.name
        utc = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_path = DEFAULT_BENCH_OUT_DIR / f"{run_id}-vs-base-{utc}.json"
    save_results_json(
        adapter_result,
        base_result,
        output_path,
        base_model=args.base_model,
        test_jsonl_path=test_jsonl,
        test_jsonl_sha256=test_sha,
        seed=args.seed,
    )
    print(f"\n[bench] Results saved to {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
