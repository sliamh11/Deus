"""Judge-LoRA training driver — wraps mlx_lm.lora as a streaming subprocess.

Step 2/4 of the judge-LoRA pipeline (plan at ~/.claude/plans/eventual-meandering-bear.md,
plan-reviewer SHIP'd Round 2). Consumes the step-1 stratified dataset
(commit 5ca45f0) and produces a LoRA adapter for Gemma-3n-E4B on Apple Metal.

Why subprocess rather than library import: the mlx_lm CLI is the well-tested
entry point; the subprocess boundary gives us a clean exact-reproducer command
in the manifest, streams stdout live (capture_output=True would black out the
30-60min run), and decouples us from internal mlx_lm API churn.

Apple Metal only. On non-Apple platforms `mlx.metal.is_available()` returns
False and pre-flight aborts.

Host venv: ~/deus/.venvs/judge-lora/bin/python3 (override via the
JUDGE_LORA_VENV env var).

Defaults are calibrated for the 658-record step-1 dataset:
- LoRA r=16 / alpha=32 (scale=2.0), dropout=0.05
- num-layers=8 (last 8 of 35 Gemma-3n-E4B blocks ≈ 23%, standard for small SFT)
- batch=2 × grad-accum=4 = effective 8; 5 epochs → 412 iters
- LR 5e-5 with 10-step linear warmup + cosine decay to 10% peak
  (mlx-lm has NO gradient clipping; lower LR + warmup is the only safe mitigation)
- --mask-prompt on (loss only on JSON answer, not the ~500-token rubric)

Dry-run / pre-flight-only modes for cheap inspection without triggering training.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from evolution.training._provenance import git_sha, git_dirty, sha256_file
from evolution.training._smoke import LORA_KEYS_GEMMA3N

# Default venv path follows Deus install convention (`project: Deus | path: ~/deus`
# per ~/deus/CLAUDE.md). Override via JUDGE_LORA_VENV env var for non-standard layouts.
VENV = Path(
    os.environ.get("JUDGE_LORA_VENV", "~/deus/.venvs/judge-lora/bin/python3")
).expanduser()
DATASET_DIR = PROJECT_ROOT / "finetune/judge-lora-gemma3n"
ADAPTER_ROOT = DATASET_DIR / "adapters"
MLX_LM_EXPECTED_VERSION = "0.31.3"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_MODEL = "mlx-community/gemma-3n-E4B-it-lm-4bit"
# Model variant selection (2026-05-18 debug session):
# - `gemma-3n-E4B-it-bf16` (multimodal): HF-transformers weight layout
#   (`language_model.model.X` vs mlx-lm's `model.language_model.X`) crashes
#   mlx_lm 0.31.3's `gemma3n.sanitize()` with KeyError: 'model'. REJECTED.
# - `gemma-3n-E4B-it-lm-bf16` (text-only): correct layout, loads fine, but
#   at ~16GB base + bf16 activations the M3 Pro 36GB margin is thin and
#   subject to OOM under memory pressure. REJECTED as default.
# - `gemma-3n-E4B-it-lm-4bit` (text-only, Q4): QLoRA (LoRALinear wraps
#   nn.QuantizedLinear per mlx_lm/tuner/lora.py:22). ~2.5GB base. 2x faster
#   forward+backward than bf16. Smoke v3 confirms peak 8.87GB (representative)
#   to 29.13GB (worst-case longest batch) on M3 Pro 36GB — comfortable.
#   SELECTED as default.
# Override via `--model mlx-community/gemma-3n-E4B-it-lm-bf16` if higher
# precision ceiling is needed and memory pressure is low.


@dataclass
class RunSpec:
    model: str
    lora_rank: int
    lora_alpha: int
    lora_dropout: float
    num_layers: int
    batch_size: int
    grad_accum: int
    epochs: int
    learning_rate: float
    mask_prompt: bool
    max_seq_length: int
    optimizer: str
    seed: int
    save_every: int
    steps_per_report: int
    steps_per_eval: int
    val_batches: int
    grad_checkpoint: bool
    yes: bool
    dry_run: bool
    pre_flight_only: bool
    skip_smoke_test: bool
    smoke_test_only: bool


def iters_from_epochs(epochs: int, n_train: int, batch: int, accum: int) -> int:
    """iters = ceil(epochs × n_train / (batch × accum)). Verified empirically:
    epochs=5, n=658, batch=2, accum=4 → ceil(411.25) = 412."""
    return math.ceil(epochs * n_train / (batch * accum))


def scale_from_alpha_rank(alpha: int, rank: int) -> float:
    """peft convention: scale = alpha / rank. mlx_lm uses `scale` directly in its
    config YAML (see mlx_lm/tuner/lora.py:98). For alpha=32, rank=16 → scale=2.0."""
    return alpha / rank


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now_iso_compact() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def count_jsonl_records(path: Path) -> int:
    with open(path, "r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def emit_yaml_config(spec: RunSpec, iters: int, peak_lr: float) -> str:
    """Generate the per-run mlx_lm YAML config from spec + computed iters/peak_lr.

    The `lora_parameters.keys` list comes from the canonical LORA_KEYS_GEMMA3N
    tuple in `evolution.training._smoke`; both the smoke gate and the real
    training run see the same allow-list. Single source of truth.
    """
    scale = scale_from_alpha_rank(spec.lora_alpha, spec.lora_rank)
    decay_steps = max(iters - 10, 1)  # iters - warmup_steps
    end_lr = peak_lr * 0.1
    keys_lines = "".join(f"    - {k}\n" for k in LORA_KEYS_GEMMA3N)
    return (
        f"# Generated by evolution/training/train_judge_lora.py\n"
        f"# scale = lora_alpha / lora_rank (peft convention; mlx_lm uses scale directly)\n"
        f"lora_parameters:\n"
        f"  rank: {spec.lora_rank}\n"
        f"  scale: {scale}\n"
        f"  dropout: {spec.lora_dropout}\n"
        f"  # Standard attention + MLP only — see evolution.training._smoke\n"
        f"  # for why altup/laurel/per_layer are excluded.\n"
        f"  keys:\n"
        f"{keys_lines}"
        f"\n"
        f"# 10-step linear warmup + cosine decay to 10% of peak LR.\n"
        f"# mlx-lm has no gradient clipping; warmup absorbs early instability.\n"
        f"lr_schedule:\n"
        f"  name: cosine_decay\n"
        f"  warmup: 10\n"
        f"  warmup_init: 0.0\n"
        f"  arguments: [{peak_lr}, {decay_steps}, {end_lr}]\n"
    )


def assemble_command(
    spec: RunSpec,
    data_dir: Path,
    adapter_dir: Path,
    config_path: Path,
    iters: int,
) -> list[str]:
    cmd = [
        str(VENV), "-m", "mlx_lm", "lora",
        "--train",
        "--model", spec.model,
        "--data", str(data_dir),
        "--fine-tune-type", "lora",
        "--config", str(config_path),
        "--adapter-path", str(adapter_dir),
        "--batch-size", str(spec.batch_size),
        "--grad-accumulation-steps", str(spec.grad_accum),
        "--iters", str(iters),
        "--learning-rate", str(spec.learning_rate),
        "--max-seq-length", str(spec.max_seq_length),
        "--num-layers", str(spec.num_layers),
        "--optimizer", spec.optimizer,
        "--save-every", str(spec.save_every),
        "--steps-per-report", str(spec.steps_per_report),
        "--steps-per-eval", str(spec.steps_per_eval),
        "--val-batches", str(spec.val_batches),
        "--seed", str(spec.seed),
    ]
    if spec.mask_prompt:
        cmd.append("--mask-prompt")
    if spec.grad_checkpoint:
        cmd.append("--grad-checkpoint")
    return cmd


def preflight(spec: RunSpec, dataset_dir: Path) -> None:
    """Six checks before training: venv, dataset files, content drift, mlx.metal,
    HF reachability, mlx_lm version. Aborts on any blocking failure."""
    if not VENV.exists():
        raise SystemExit(f"[preflight] venv binary missing: {VENV}")
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.exists():
        raise SystemExit(f"[preflight] dataset manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data_subdir = dataset_dir / "data"
    expected = manifest.get("content_sha256", {})
    for split in ("train", "valid", "test"):
        split_path = data_subdir / f"{split}.jsonl"
        if not split_path.exists():
            raise SystemExit(f"[preflight] split file missing: {split_path}")
        actual = sha256_file(split_path)
        if expected.get(split) != actual:
            raise SystemExit(
                f"[preflight] content drift on {split}: manifest={expected.get(split)} "
                f"actual={actual}. Re-run build_judge_lora_dataset.py or restore the file."
            )
    metal_check = subprocess.run(
        [str(VENV), "-c", "import mlx.core as mx; assert mx.metal.is_available()"],
        capture_output=True, text=True,
    )
    if metal_check.returncode != 0:
        raise SystemExit(f"[preflight] mlx.metal not available: {metal_check.stderr.strip()}")
    hf_check = subprocess.run(
        [str(VENV), "-c",
         f"from huggingface_hub import hf_hub_download; "
         f"hf_hub_download({spec.model!r}, filename='config.json')"],
        capture_output=True, text=True,
    )
    if hf_check.returncode != 0:
        raise SystemExit(
            f"[preflight] HF model unreachable: {spec.model}\n"
            f"  stderr: {hf_check.stderr.strip()}\n"
            f"  Note: google/gemma-3n-E4B-it requires huggingface-cli login + license accept (gated)."
        )
    ver_check = subprocess.run(
        [str(VENV), "-c", "import mlx_lm; print(mlx_lm.__version__)"],
        capture_output=True, text=True,
    )
    actual_ver = ver_check.stdout.strip()
    if actual_ver != MLX_LM_EXPECTED_VERSION:
        print(
            f"[preflight] WARN mlx_lm version drift: expected {MLX_LM_EXPECTED_VERSION}, "
            f"got {actual_ver}. Proceeding; recorded in manifest.",
            file=sys.stderr,
        )


def run_preflight_smoke(spec: RunSpec, dataset_dir: Path) -> dict[str, Any]:
    """Preflight check #7 (smoke test).

    Invokes evolution.training._smoke via the venv as a subprocess — separate
    process is intentional: `mlx_lm.tuner.trainer.grad_checkpoint(layer)`
    patches `type(layer).__call__` at the class level. Running smoke in-process
    would persist the patch across the smoke→training boundary; subprocess
    isolation guarantees the real training starts from a clean class state.

    Loads the model + applies LoRA wrap + runs ONE forward+backward+optimizer
    step on a representative batch (~10-30s on warm cache, ~3 min on cold).
    Catches the three classes of failure that wasted multi-minute runs in the
    2026-05-18 debug session: model_load (sanitize KeyError), forbidden_wrap
    (altup/laurel/per_layer Linears), and OOM at the train step.

    Returns the parsed SmokeResult dict on success. Raises SystemExit with a
    specific `reason` + diagnostic on failure.
    """
    # Guard VENV existence here too — the main training path checks this in
    # preflight(), but `--smoke-test-only` bypasses preflight if combined with
    # --skip-smoke-test, and direct callers (e.g., tests, future scripts) may
    # not have run preflight. Without this guard, a missing VENV surfaces as
    # an unhandled FileNotFoundError from subprocess.run with no diagnosis.
    if not VENV.exists():
        raise SystemExit(f"[smoke] venv binary missing: {VENV}")
    train_jsonl = dataset_dir / "data" / "train.jsonl"
    scale = scale_from_alpha_rank(spec.lora_alpha, spec.lora_rank)
    cmd = [
        str(VENV), "-m", "evolution.training._smoke",
        "--model", spec.model,
        "--train-jsonl", str(train_jsonl),
        "--batch-size", str(spec.batch_size),
        "--max-seq-length", str(spec.max_seq_length),
        "--num-layers", str(spec.num_layers),
        "--lora-rank", str(spec.lora_rank),
        "--lora-scale", str(scale),
        "--lora-dropout", str(spec.lora_dropout),
        "--learning-rate", str(spec.learning_rate),
    ]
    if spec.grad_checkpoint:
        cmd.append("--grad-checkpoint")
    # Inherit PYTHONPATH so `python -m evolution.training._smoke` resolves
    # the package from the worktree root.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    print(f"[smoke] running (~10-30s on warm cache)...", file=sys.stderr)
    t_start = datetime.now(timezone.utc)
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    elapsed_s = (datetime.now(timezone.utc) - t_start).total_seconds()

    # Last NON-BLANK line of stdout is the JSON result; everything else is
    # diagnostic. The blank-line filter is load-bearing — if a future smoke
    # invocation emits a trailing print() without explicit newline removal,
    # the result must still be at the end of the meaningful output.
    stdout_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    last_line = stdout_lines[-1] if stdout_lines else ""
    try:
        payload = json.loads(last_line)
    except json.JSONDecodeError:
        raise SystemExit(
            f"[smoke] FAIL — could not parse JSON from subprocess stdout (exit {proc.returncode}).\n"
            f"  last stdout line: {last_line[:200]!r}\n"
            f"  stderr tail: {proc.stderr[-500:]!r}"
        )
    if not payload.get("ok"):
        reason = payload.get("reason", "unknown")
        diagnostic = payload.get("diagnostic", {})
        raise SystemExit(
            f"[smoke] FAIL at {reason} (after {elapsed_s:.0f}s)\n"
            f"  diagnostic: {json.dumps(diagnostic, indent=2)}\n"
            f"  stderr tail: {proc.stderr[-500:]!r}"
        )
    print(
        f"[smoke] OK in {elapsed_s:.0f}s — loss {payload['loss']:.4f}, "
        f"peak {payload['peak_memory_gb']:.2f} GB, "
        f"{payload['wrapped_modules']} modules wrapped, "
        f"effective_seq={payload['effective_seq_length']}",
        file=sys.stderr,
    )
    return payload


def write_training_manifest(
    adapter_dir: Path,
    spec: RunSpec,
    command: list[str],
    dataset_manifest: dict[str, Any],
    started_at: str,
    finished_at: str,
    exit_code: int,
    stdout_tail: str,
    checkpoint_paths: list[str],
    iters: int,
    mlx_lm_version: str,
    run_id: str,
    captured_git_sha: str,
    captured_git_dirty: bool,
) -> None:
    """Write training_manifest.json.

    run_id / captured_git_sha / captured_git_dirty are passed in (captured at
    main() entry, NOT re-derived here) so they match the adapter_dir name and
    can't flip mid-run if the working tree changes during a 30-60min run.

    stdout_tail keeps the last 200 lines for post-hoc debugging — useful even
    when the run succeeds (loss trajectory, val Pearson, eval logs). stderr is
    merged into stdout via subprocess.STDOUT, so no separate stderr field.

    Dataset split paths are repo-relative for manifest portability.
    """
    payload = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "exit_code": exit_code,
        "git_sha": captured_git_sha,
        "git_dirty": captured_git_dirty,
        "dataset_manifest_path": "finetune/judge-lora-gemma3n/manifest.json",
        "dataset_split_paths": {
            split: f"finetune/judge-lora-gemma3n/data/{split}.jsonl"
            for split in ("train", "valid", "test")
        },
        "dataset_content_sha256": dataset_manifest.get("content_sha256", {}),
        "num_train_records": dataset_manifest.get("splits", {}).get("train", {}).get("n", 0),
        "effective_batch_size": spec.batch_size * spec.grad_accum,
        "iters": iters,
        "model": spec.model,
        "hyperparameters": asdict(spec),
        "lr_schedule": {
            "name": "cosine_decay",
            "warmup": 10,
            "warmup_init": 0.0,
            "arguments": [
                spec.learning_rate,
                max(iters - 10, 1),
                spec.learning_rate * 0.1,
            ],
        },
        "mlx_lm_version": mlx_lm_version,
        # Glob pattern validated against mlx_lm 0.31.3 output naming.
        # Empty list indicates either: training failed before any save, or
        # the upstream naming convention changed (revisit on version bump).
        "checkpoint_paths": checkpoint_paths,
        "command": command,
        "stdout_tail": stdout_tail,
    }
    (adapter_dir / "training_manifest.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def make_run_id(captured_sha: str | None = None, captured_dirty: bool | None = None) -> str:
    """Build a run-id from captured git state. Defaults to live state when args
    are None (used in tests + make_run_id() called bare). main() captures once
    and threads through to keep run_id stable across a long training run."""
    sha = captured_sha if captured_sha is not None else (git_sha() or "nogit")
    dirty = captured_dirty if captured_dirty is not None else bool(git_dirty())
    suffix = "-dirty" if dirty else ""
    return f"{utc_now_iso_compact()}-{sha[:7] if sha != 'nogit' else 'nogit'}{suffix}"


def parse_args(argv: list[str] | None = None) -> RunSpec:
    p = argparse.ArgumentParser(
        description="Judge-LoRA training driver (Gemma-3n-E4B + mlx_lm.lora on Apple Metal).",
    )
    p.add_argument("--model", default=DEFAULT_MODEL,
                   help=f"HF repo id (default: {DEFAULT_MODEL}; verified public). "
                        f"google/gemma-3n-E4B-it requires huggingface-cli login.")
    p.add_argument("--lora-rank", type=int, default=16)
    p.add_argument("--lora-alpha", type=int, default=32)
    p.add_argument("--lora-dropout", type=float, default=0.05)
    p.add_argument("--num-layers", type=int, default=8,
                   help="Last N transformer blocks (default 8 of 35 in Gemma-3n-E4B).")
    p.add_argument("--batch-size", type=int, default=2)
    p.add_argument("--grad-accum", type=int, default=4,
                   help="Gradient accumulation steps (effective batch = batch_size * grad_accum).")
    p.add_argument("--epochs", type=int, default=5)
    p.add_argument("--learning-rate", type=float, default=5e-5,
                   help="Defensive default; mlx-lm has no gradient clipping. Warmup absorbs early instability.")
    p.add_argument("--no-mask-prompt", action="store_true",
                   help="Train on the full prompt (default: mask, loss only on assistant turn).")
    p.add_argument("--max-seq-length", type=int, default=4096)
    p.add_argument("--optimizer", default="adamw", choices=("adam", "adamw", "muon", "sgd", "adafactor"))
    p.add_argument("--seed", type=int, default=20260517,
                   help="Defaults to step-1 dataset seed for end-to-end reproducibility.")
    p.add_argument("--save-every", type=int, default=50)
    p.add_argument("--steps-per-report", type=int, default=10)
    p.add_argument("--steps-per-eval", type=int, default=50)
    p.add_argument("--val-batches", type=int, default=41,
                   help="ceil(81/2) = full valid split at default batch size.")
    # grad_checkpoint default on: 2026-05-18 OOM debug showed peak Metal
    # memory hit 29GB on worst-case batches at our M3 Pro 36GB budget.
    # grad_checkpoint trades ~5-10% wall-clock for ~30-40% memory reduction
    # by recomputing forward activations during backward (see
    # mlx_lm/tuner/trainer.py:25 `grad_checkpoint`; one call patches the
    # decoder layer's class so ALL layers checkpoint). Pass
    # `--no-grad-checkpoint` to disable.
    p.add_argument("--grad-checkpoint", dest="grad_checkpoint", action="store_true",
                   default=True, help="Enable activation recomputation (default: on).")
    p.add_argument("--no-grad-checkpoint", dest="grad_checkpoint", action="store_false",
                   help="Disable grad_checkpoint (faster but higher memory peak).")
    p.add_argument("--yes", action="store_true", help="Skip the y/N prompt before training.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print generated config + assembled command; do not invoke training.")
    p.add_argument("--pre-flight-only", action="store_true",
                   help="Run pre-flight checks and exit (no training).")
    # Smoke-test gate (preflight check #7). Default-on after the 2026-05-18 debug
    # session: three speculative-default failures wasted ~30 min of training time
    # that a 10-30s smoke would have caught. The escape hatch exists for power
    # users who have already validated the exact config (e.g., re-running after
    # an aborted training with identical flags).
    p.add_argument("--skip-smoke-test", action="store_true",
                   help="Bypass the smoke gate. Use ONLY when you've already validated "
                        "the exact same (model, batch, max_seq, num_layers, grad_checkpoint) "
                        "combination — e.g. re-running an aborted training.")
    p.add_argument("--smoke-test-only", action="store_true",
                   help="Run pre-flight + smoke and exit (no training). Mirrors "
                        "--pre-flight-only but deeper — exercises the actual forward+backward path.")
    args = p.parse_args(argv)
    return RunSpec(
        model=args.model,
        lora_rank=args.lora_rank,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        num_layers=args.num_layers,
        batch_size=args.batch_size,
        grad_accum=args.grad_accum,
        epochs=args.epochs,
        learning_rate=args.learning_rate,
        mask_prompt=not args.no_mask_prompt,
        max_seq_length=args.max_seq_length,
        optimizer=args.optimizer,
        seed=args.seed,
        save_every=args.save_every,
        steps_per_report=args.steps_per_report,
        steps_per_eval=args.steps_per_eval,
        val_batches=args.val_batches,
        grad_checkpoint=args.grad_checkpoint,
        yes=args.yes,
        dry_run=args.dry_run,
        pre_flight_only=args.pre_flight_only,
        skip_smoke_test=args.skip_smoke_test,
        smoke_test_only=args.smoke_test_only,
    )


def main(argv: list[str] | None = None) -> int:
    spec = parse_args(argv)
    data_dir = DATASET_DIR / "data"
    train_jsonl = data_dir / "train.jsonl"
    if train_jsonl.exists():
        n_train = count_jsonl_records(train_jsonl)
    else:
        # Fallback to the step-1 known count so --dry-run still emits a sensible
        # command. Pre-flight aborts on missing dataset before real training runs.
        n_train = 658
        print(
            f"[warn] {train_jsonl} not found; using fallback n_train=658 from step-1.\n"
            f"       Pre-flight will abort the actual training run.",
            file=sys.stderr,
        )
    iters = iters_from_epochs(spec.epochs, n_train, spec.batch_size, spec.grad_accum)
    # Capture git state ONCE here; thread through to keep run_id and manifest
    # git_sha/git_dirty consistent even if the working tree changes during the
    # 30-60min training run.
    captured_git_sha = git_sha() or "nogit"
    captured_git_dirty = bool(git_dirty())
    run_id = make_run_id(captured_sha=captured_git_sha, captured_dirty=captured_git_dirty)
    adapter_dir = ADAPTER_ROOT / run_id
    config_path = adapter_dir / "run_config.yaml"

    if spec.dry_run:
        print(emit_yaml_config(spec, iters, spec.learning_rate))
        print()
        print(shlex.join(assemble_command(spec, data_dir, adapter_dir, config_path, iters)))
        return 0

    preflight(spec, DATASET_DIR)
    if spec.pre_flight_only:
        print("Pre-flight passed.")
        return 0

    # Smoke gate (preflight check #7). Runs the actual forward+backward path
    # against a representative batch in a separate process. Default-on; opt-out
    # via --skip-smoke-test. The cost (~10-30s warm cache, ~3 min cold) is
    # trivial vs. the 30-90 min wall-clock cost of finding a training-time
    # failure after model download + iter 0 validation.
    if spec.smoke_test_only and spec.skip_smoke_test:
        # Contradictory flags. Warn + short-circuit so future readers don't
        # need to trace control flow to understand the no-op nature.
        print(
            "[warn] --smoke-test-only AND --skip-smoke-test passed together — "
            "the run does nothing beyond preflight. Exiting.",
            file=sys.stderr,
        )
        print("Skipped (--skip-smoke-test); preflight passed.")
        return 0
    if not spec.skip_smoke_test:
        run_preflight_smoke(spec, DATASET_DIR)
    if spec.smoke_test_only:
        print("Smoke test passed.")
        return 0

    if not spec.yes:
        print(f"Run summary:\n  model={spec.model}\n  iters={iters} (epochs={spec.epochs}, "
              f"effective_batch={spec.batch_size * spec.grad_accum})\n  adapter_dir={adapter_dir}")
        resp = input("Proceed? [y/N] ").strip().lower()
        if resp != "y":
            print("Aborted.")
            return 1

    adapter_dir.mkdir(parents=True, exist_ok=True)
    config_path.write_text(emit_yaml_config(spec, iters, spec.learning_rate))
    command = assemble_command(spec, data_dir, adapter_dir, config_path, iters)
    dataset_manifest = json.loads((DATASET_DIR / "manifest.json").read_text(encoding="utf-8"))
    mlx_lm_version_proc = subprocess.run(
        [str(VENV), "-c", "import mlx_lm; print(mlx_lm.__version__)"],
        capture_output=True, text=True,
    )
    mlx_lm_version = mlx_lm_version_proc.stdout.strip() or "unknown"

    started_at = utc_now_iso()

    # Stream live (subprocess.run buffers for the full 30-60min run).
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,  # merge stderr → stdout for temporal ordering
        text=True,
        bufsize=1,
    )
    stdout_lines: list[str] = []
    if proc.stdout is None:
        # Should never happen because we passed stdout=subprocess.PIPE, but a
        # bare 'assert' would be stripped under python -O. Make it explicit.
        raise RuntimeError("subprocess.Popen stdout is None despite stdout=PIPE")
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        stdout_lines.append(line)
    proc.wait()
    finished_at = utc_now_iso()
    stdout_tail = "".join(stdout_lines[-200:])

    checkpoint_paths = sorted(str(p) for p in adapter_dir.glob("*adapter*.safetensors"))

    write_training_manifest(
        adapter_dir=adapter_dir,
        spec=spec,
        command=command,
        dataset_manifest=dataset_manifest,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=proc.returncode,
        stdout_tail=stdout_tail,
        checkpoint_paths=checkpoint_paths,
        iters=iters,
        mlx_lm_version=mlx_lm_version,
        run_id=run_id,
        captured_git_sha=captured_git_sha,
        captured_git_dirty=captured_git_dirty,
    )
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
