"""LoRA smoke-test gate for the judge-LoRA training driver.

Purpose: catch invalid (model, LoRA keys, batch, grad_checkpoint) combinations
in <60s by exercising the EXACT code path that real training uses — model
load + LoRA wrap + ONE forward+backward+optimizer step — on a representative
batch. Catches three failure modes that surfaced during the 2026-05-18 debug
session and each cost 3-15 min of wasted training time:

1. mlx_lm 0.31.3 `gemma3n.Model.sanitize()` (gemma3n.py:602-606) raises
   `KeyError: 'model'` on multimodal weight layouts. Text-only `-lm-*`
   variants ship with the correct layout. Smoke catches this in model_load.

2. Default LoRA discovery (`linear_to_lora_layers` in tuner/utils.py:85-110)
   auto-wraps ALL `nn.Linear` modules including gemma-3n specials
   (`altup.prediction_coefs`, `laurel.*`, `per_layer_*`) that the model
   mutates mid-forward — wrapping breaks the in-place write. Smoke catches
   this by asserting NONE of the wrapped paths contain those substrings.

3. OOM at first training step due to grad_checkpoint default off + bf16
   base + worst-case batch. Smoke runs the forward+backward path with the
   configured `grad_checkpoint` setting and reports peak Metal memory; a
   smoke pass is strong evidence the real run will fit.

## Invocation

Two surfaces:

  - Python API (unit-testable):
      from evolution.training._smoke import run_smoke
      result = run_smoke(model=..., train_jsonl=..., ...)
      # → SmokeResult(loss, step_ms, peak_memory_gb, wrapped_modules, mode)
      # Raises SmokeError(reason, diagnostic) on any failure.

  - CLI subprocess (called by train_judge_lora.py — INTERNAL only, not an
    agent-native surface per docs/decisions/printing-press-adoption.md):
      python -m evolution.training._smoke \\
          --model NAME --train-jsonl PATH --batch-size N --max-seq-length N \\
          --num-layers N --lora-rank N --lora-scale F --lora-dropout F \\
          [--grad-checkpoint] [--stress]
      Exit 0 on success; exit 1 on any SmokeError. The CLI ALWAYS emits a
      one-line JSON result as the last stdout line: {"ok": true, ...} or
      {"ok": false, "reason": ..., "diagnostic": {...}}. Diagnostic
      progress lines go to stderr.

## Constants

`LORA_KEYS_GEMMA3N` is the canonical 7-key allow-list for Gemma-3n decoder
layers (standard attention + MLP projections, excluding altup/laurel/
per_layer specials). Keys are Gemma-3n-E4B specific. For other base models,
pass an explicit `--lora-keys` list or extend this tuple.

## Developer note

The unit test for this module covers function-signature and dataclass shape
only — it does NOT exercise the real `mlx_lm.load` path (cost ~3 min on cold
cache, unsuitable for CI). If you edit this module, you MUST run
`train_judge_lora.py --smoke-test-only` locally before pushing so that
regressions in `run_smoke` result fields surface before they reach main.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence


# Canonical LoRA target paths for Gemma-3n-E4B decoder layers. These match
# the per-layer Linear module paths discovered by named_modules() (verified
# 2026-05-18 against mlx-community/gemma-3n-E4B-it-lm-bf16, 35 decoder
# layers each containing exactly these 7 paths plus the altup/laurel/
# per_layer specials we intentionally skip).
LORA_KEYS_GEMMA3N: tuple[str, ...] = (
    "self_attn.q_proj",
    "self_attn.k_proj",
    "self_attn.v_proj",
    "self_attn.o_proj",
    "mlp.gate_proj",
    "mlp.up_proj",
    "mlp.down_proj",
)

# Substrings that, if present in a wrapped module path, indicate a
# gemma-3n architectural special that would crash during forward.
GEMMA3N_FORBIDDEN_WRAP_SUBSTRINGS: tuple[str, ...] = (
    "altup",
    "laurel",
    "per_layer",
)


@dataclass
class SmokeResult:
    """Outcome of a successful smoke run."""
    loss: float
    step_ms: float
    peak_memory_gb: float
    wrapped_modules: int
    mode: str  # "representative" or "stress"
    effective_seq_length: int


class SmokeError(RuntimeError):
    """Raised on any smoke-stage failure. `reason` is one of:
    model_load, lora_wrap, forbidden_wrap, forward, backward, oom, dataset.
    `diagnostic` carries a JSON-safe dict with structured detail.
    """
    def __init__(self, reason: str, diagnostic: dict):
        super().__init__(f"smoke failed at {reason}: {diagnostic}")
        self.reason = reason
        self.diagnostic = diagnostic


def _select_batch(
    tokenized: list[list[int]],
    batch_size: int,
    stress: bool,
) -> list[list[int]]:
    """Pick a batch from a length-sorted list of tokenized records.

    `representative` (default): median-band pair (matches average step memory
        and time of real training, since iterate_batches sorts by length and
        most batches cluster around the median).
    `stress`: longest N records (worst-case batch — useful for ad-hoc OOM
        validation; not exposed by default since real training only hits
        this one batch out of ~82).
    """
    if len(tokenized) < batch_size:
        raise SmokeError("dataset", {"n_records": len(tokenized), "batch_size": batch_size})
    if stress:
        return tokenized[-batch_size:]
    mid = len(tokenized) // 2
    return tokenized[mid : mid + batch_size]


def _pad_batch_realistic(
    batch: list[list[int]],
    max_seq_length: int,
    pad_id: int,
) -> tuple[int, list[list[int]]]:
    """Pad batch matching mlx_lm/tuner/trainer.py:iterate_batches (lines 156-159).

    Returns (effective_seq_length, padded_batch).
    """
    lengths = [len(r) for r in batch]
    pad_to = 32
    eff = 1 + pad_to * ((max(lengths) + pad_to - 1) // pad_to)
    eff = min(eff, max_seq_length)
    padded = []
    for ids in batch:
        truncated = ids[:eff]
        padded.append(truncated + [pad_id] * (eff - len(truncated)))
    return eff, padded


def run_smoke(
    *,
    model: str,
    train_jsonl: Path,
    batch_size: int,
    max_seq_length: int,
    num_layers: int,
    lora_rank: int,
    lora_scale: float,
    lora_dropout: float,
    lora_keys: Sequence[str] = LORA_KEYS_GEMMA3N,
    grad_checkpoint: bool = True,
    learning_rate: float = 5e-5,
    stress: bool = False,
) -> SmokeResult:
    """Run a one-step smoke test exercising the real training code path.

    Lazy-imports mlx (only loaded when called) so this module is cheap to
    import from a host process that doesn't have mlx installed.

    Raises SmokeError on any failure with a structured `reason` + diagnostic.
    """
    # Lazy imports — these are only available in the dedicated venv.
    try:
        import mlx.core as mx
        import mlx.nn as nn
        import mlx.optimizers as optim
        from mlx_lm import load as mlx_load
        from mlx_lm.tuner.lora import LoRALinear
        from mlx_lm.tuner.utils import linear_to_lora_layers
    except ImportError as e:
        raise SmokeError("import", {"missing": str(e)})

    # 1. Load model
    try:
        mlx_model, tok = mlx_load(model)
    except Exception as e:
        raise SmokeError("model_load", {"model": model, "error": str(e), "error_class": type(e).__name__})

    # 2. Apply LoRA wrapping
    try:
        config = {
            "rank": lora_rank,
            "scale": lora_scale,
            "dropout": lora_dropout,
            "keys": list(lora_keys),
        }
        linear_to_lora_layers(mlx_model, num_layers, config=config, use_dora=False)
    except Exception as e:
        raise SmokeError("lora_wrap", {"keys": list(lora_keys), "num_layers": num_layers,
                                       "error": str(e), "error_class": type(e).__name__})

    # 3. Inspect wrap outcome — must wrap >0 modules AND not wrap forbidden specials
    wrapped_paths = [k for k, m in mlx_model.named_modules() if isinstance(m, LoRALinear)]
    if not wrapped_paths:
        raise SmokeError("lora_wrap", {"reason": "no_modules_wrapped",
                                       "keys": list(lora_keys), "num_layers": num_layers})
    forbidden_hit = [p for p in wrapped_paths
                     if any(sub in p for sub in GEMMA3N_FORBIDDEN_WRAP_SUBSTRINGS)]
    if forbidden_hit:
        raise SmokeError("forbidden_wrap", {"forbidden_paths": forbidden_hit[:5],
                                            "n_forbidden": len(forbidden_hit),
                                            "keys": list(lora_keys)})

    # 4. Apply grad_checkpoint (patches the decoder layer's class — one call
    # affects all instances of the layer class). The attribute chain
    # `mlx_model.model.language_model.layers[0]` is the canonical access path
    # for `mlx_lm/models/gemma3n.py:Model.model.language_model.layers` and
    # works identically for the `-lm-bf16` (full precision) and `-lm-4bit`
    # (quantized) text-only variants — both share the same module hierarchy,
    # only the inner Linear layers differ (nn.Linear vs nn.QuantizedLinear).
    if grad_checkpoint:
        try:
            from mlx_lm.tuner.trainer import grad_checkpoint as _gc
            _gc(mlx_model.model.language_model.layers[0])
        except Exception as e:
            raise SmokeError("grad_checkpoint", {"error": str(e), "error_class": type(e).__name__})

    # 5. Load + tokenize dataset (just enough records to find the median band)
    try:
        with open(train_jsonl) as f:
            raw_records = [json.loads(line) for line in f]
        tokenized = []
        for rec in raw_records:
            text = tok.apply_chat_template(rec["messages"], tokenize=False)
            tokenized.append(tok.encode(text))
        tokenized.sort(key=len)
    except Exception as e:
        raise SmokeError("dataset", {"path": str(train_jsonl), "error": str(e), "error_class": type(e).__name__})

    # 6. Build batch with realistic padding
    selected = _select_batch(tokenized, batch_size, stress)
    pad_id = tok.pad_token_id if tok.pad_token_id is not None else 0
    eff_seq, padded = _pad_batch_realistic(selected, max_seq_length, pad_id)
    inputs = mx.array(padded)
    # Targets identical to inputs: smoke validates the COMPUTE GRAPH (shapes,
    # dtypes, memory, gradient flow), not the linguistic correctness of the
    # loss. The reported loss value (~23 for an untrained adapter on a random
    # alignment) is a sanity signal that backprop produces a finite number,
    # NOT a calibrated training-loss estimate.
    targets = inputs
    lengths_mx = mx.array([len(r) for r in selected])

    # 7. Build optimizer + reset peak memory counter
    optimizer = optim.AdamW(learning_rate=learning_rate)
    mx.reset_peak_memory()

    # 8. One forward + backward + optimizer step (the path that OOMed before)
    def _loss_fn(m, inp, tgt, lens):
        logits = m(inp).astype(mx.float32)
        length_mask = mx.arange(inp.shape[1])[None, :] < lens[:, None]
        ce = nn.losses.cross_entropy(logits, tgt) * length_mask
        return ce.sum() / length_mask.sum()

    t_step = time.time()
    try:
        loss_and_grad = nn.value_and_grad(mlx_model, _loss_fn)
        loss_val, grads = loss_and_grad(mlx_model, inputs, targets, lengths_mx)
        optimizer.update(mlx_model, grads)
        mx.eval(mlx_model.parameters(), optimizer.state, loss_val)
    except RuntimeError as e:
        # mlx surfaces Metal OOM as a runtime_error with "Insufficient Memory" in the message
        msg = str(e)
        if "Insufficient Memory" in msg or "OutOfMemory" in msg.lower():
            raise SmokeError("oom", {"error": msg, "effective_seq_length": eff_seq})
        raise SmokeError("forward", {"error": msg, "error_class": "RuntimeError"})
    except Exception as e:
        raise SmokeError("forward", {"error": str(e), "error_class": type(e).__name__})

    step_ms = (time.time() - t_step) * 1000.0
    peak_gb = mx.get_peak_memory() / (1024**3)

    return SmokeResult(
        loss=float(loss_val.item()),
        step_ms=step_ms,
        peak_memory_gb=peak_gb,
        wrapped_modules=len(wrapped_paths),
        mode="stress" if stress else "representative",
        effective_seq_length=eff_seq,
    )


def _parse_keys_csv(s: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in s.split(",") if p.strip())


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint — invoked as subprocess by train_judge_lora.py.

    Last line of stdout is one-line JSON: {"ok": true, ...} or
    {"ok": false, "reason": ..., "diagnostic": {...}}. Diagnostic info
    (progress lines, warnings) goes to stderr.

    NOT an agent-native CLI: no typed exit-code taxonomy, no --select/--compact,
    not in scripts/. Internal-only protocol between this module and the
    training driver's `run_preflight_smoke` function.
    """
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else "")
    p.add_argument("--model", required=True)
    p.add_argument("--train-jsonl", required=True, type=Path)
    p.add_argument("--batch-size", required=True, type=int)
    p.add_argument("--max-seq-length", required=True, type=int)
    p.add_argument("--num-layers", required=True, type=int)
    p.add_argument("--lora-rank", required=True, type=int)
    p.add_argument("--lora-scale", required=True, type=float)
    p.add_argument("--lora-dropout", required=True, type=float)
    p.add_argument("--lora-keys", type=_parse_keys_csv, default=LORA_KEYS_GEMMA3N,
                   help="Comma-separated module-path keys. Default: gemma-3n 7-key set.")
    p.add_argument("--learning-rate", type=float, default=5e-5)
    p.add_argument("--grad-checkpoint", action="store_true")
    p.add_argument("--stress", action="store_true",
                   help="Use longest records (worst-case batch) instead of median band.")
    # The CLI always emits a JSON result on the last stdout line (the format
    # contract is unconditional). No --json flag — there is no other mode.
    args = p.parse_args(argv)

    print(f"[smoke] model={args.model} batch={args.batch_size} "
          f"max_seq={args.max_seq_length} num_layers={args.num_layers} "
          f"grad_checkpoint={args.grad_checkpoint} stress={args.stress}",
          file=sys.stderr)

    try:
        result = run_smoke(
            model=args.model,
            train_jsonl=args.train_jsonl,
            batch_size=args.batch_size,
            max_seq_length=args.max_seq_length,
            num_layers=args.num_layers,
            lora_rank=args.lora_rank,
            lora_scale=args.lora_scale,
            lora_dropout=args.lora_dropout,
            lora_keys=args.lora_keys,
            grad_checkpoint=args.grad_checkpoint,
            learning_rate=args.learning_rate,
            stress=args.stress,
        )
    except SmokeError as e:
        # JSON failure result on last stdout line (parsed by caller)
        print(json.dumps({"ok": False, "reason": e.reason, "diagnostic": e.diagnostic}))
        return 1

    payload = {"ok": True, **asdict(result)}
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
