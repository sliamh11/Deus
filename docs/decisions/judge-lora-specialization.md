# ADR: Judge-LoRA Specialization Did Not Beat Base Gemma-3n-E4B Q4

**Date:** 2026-05-18
**Status:** Accepted
**Scope:** `evolution/training/`, `evolution/judge/`, local judge model selection

## Context

The judge-LoRA pipeline (PRs #466, #469, #470) was motivated by a 2026-05-17
n=50 stratified bench against Ollama-served **Gemma-3n-E4B Q8_0**, which
showed a 0.163 Pearson gap behind Gemini ground-truth scores. The
hypothesis: LoRA-fine-tune Gemma-3n on Gemini-scored interactions to close
that gap, then deploy the adapter as the production local judge.

Pipeline execution:

1. **Step 1** (PR #466): Built a 779-record stratified dataset from
   Gemini-scored interactions, split 658/81/40 train/val/test.
2. **Step 2** (PR #466): Wrote training driver, ran a real training run
   (run ID `20260518T071842Z-1614baf-dirty`): val loss 0.143 in 77.8 min on
   M3 Pro 36 GB.
3. **Step 2.1** (PR #469): Added smoke-test preflight gate + working
   defaults after three speculative-default failures cost 30+ minutes.
4. **Step 3** (PR #470): Built `evolution/training/bench_judge_lora.py`,
   ran Adapter-vs-Base on the held-out 40-record test split, in-process
   mlx_lm Q4 inference with greedy decoding and a fixed seed.

## The Headline Finding

The trained adapter did **NOT** improve over the base model:

| Metric                | Adapter | Base   | Δ          |
|-----------------------|---------|--------|------------|
| Mean Pearson          | 0.368   | 0.390  | **−0.022** |
| Mean MAE              | 0.287   | 0.261  | +0.026     |
| Parse error rate      | 0.0%    | 0.0%   | 0          |
| Composite (legacy)    | 0.661   | 0.678  | −0.017     |

Per-dim Pearson deltas: quality −0.027, tool_use −0.052, personalization
−0.011, safety zero variance on both (ground truth = 1.0 on every test
record).

Two findings invalidate the original motivating premise:

- **Base Gemma-3n-E4B-Q4 already outputs valid JSON 40/40 times.** The
  "LoRA fixes structured-output failures" win we expected is not real on
  this inference stack.
- **The adapter slightly regressed scoring quality** on every measurable
  dimension. Likely overshoot: LR 5e-5 × 5 epochs × 8 LoRA layers on 658
  records over-fit the training distribution and drifted away from base
  judgment.

## Why This Doesn't Match The May Bench

The 0.163 Pearson gap from the 2026-05-17 bench was measured against
**Ollama-served Q8_0**. Today's bench is **mlx_lm Q4**. Different
runtime, different quantization, different sampling stack. The May
bench's gap may have always been a Q8_0 + Ollama artifact that doesn't
exist on the mlx_lm Q4 deployment target.

We did not re-run the May bench against mlx_lm before training. That was
the load-bearing measurement that motivated the entire pipeline.

## Decision

1. **Do NOT adopt run `20260518T071842Z-1614baf-dirty` as the production
   local judge.** Keep base Gemma-3n-E4B (mlx_lm Q4 for the mlx path,
   Ollama Q8_0 for the Ollama path) as the local judge until a future
   tuned adapter clears a documented regression bar.
2. **Keep the bench script + adapter artifact on disk** as the baseline
   for future tuning experiments. The artifact remains at
   `finetune/judge-lora-gemma3n/adapters/20260518T071842Z-1614baf-dirty/`
   (gitignored, local-only). Re-running `evolution/training/bench_judge_lora.py`
   regenerates the comparison table from scratch.
3. **Treat parse-error rate as the primary "specialization needed?" gate**
   for future judge-tuning proposals. If base output is already valid JSON
   ≥ 95% of the time on the target inference stack, prefer prompt-engineering
   improvements over fine-tuning.

## Next Experiments (Preferred Over Retune)

Before any retune is attempted, the following alternatives should be
evaluated — each addresses one or more of the four root causes above with
less risk and lower cost than another LoRA training run. The retune
conditions in the next section apply only if all four alternatives prove
insufficient.

### 1. Cross-stack truth bench (precondition for everything else)

Run `bench_judge_lora.py` against the **untrained base** on all three
local-judge stacks — mlx_lm Q4, Ollama Q8_0, llama.cpp Q8_0 — using the
same 40-record test split, same seed, same rubric. Add bootstrap 95 %
confidence intervals (1000 resamples, no new deps). Decisive output: per-
stack Pearson vs Gemini with error bars. Tells us whether the gap is real
on the deployment target or a phantom of cross-stack measurement. Effort:
1-2 hours.

### 2. Replace base model, don't tune it

The local judge is a **routing decision**, not a training decision. The
per-surface env vars (`OLLAMA_JUDGE_MODEL`, etc.) already exist. Bench
candidate base models — gemma4 family (`e2b`/`e4b`/`26b`), Qwen2.5-3B/7B,
Phi-4-mini — on the production fixture and pick the cheapest that clears
Pearson ≥ 0.70 + parse-rate ≥ 95 %. Note: prior internal evidence
(`docs/TOKEN_OPTIMIZATION.md` lines 100-121) flags `gemma4:e4b` as
unreliable on small template-presence tasks; that does NOT generalize to
the rubric-scoring task without re-measurement. This is a measurement
question, not a config change.

### 3. Continuous logit-mean scoring (no training)

Switch the judge prompt from "return JSON of 4 floats" to a sequential
rubric ending with `"score: "`. At that token position, read top-k
logprobs over `{0.0, 0.1, ..., 1.0}` (11 tokens) and compute the
probability-weighted expected value. Stack: (a) few-shot calibration
anchors (one Gemini-labeled example per rubric level, drawn from train
not test); (b) self-consistency lite (3 samples at temp 0.3, take the
mean). Externally validated: G-Eval (Liu 2023) and [Alves et al. 2025 "Improving LLM-as-a-Judge Inference with the Judgment Distribution"](https://arxiv.org/html/2503.03064v2).

The same brainstormer round that generated this section also cited [Arize evidence-based prompting strategies](https://arize.com/blog/evidence-based-prompting-strategies-for-llm-as-a-judge-explanations-and-chain-of-thought/) with a specific Spearman 0.51 → 0.66 chain-of-thought lift for summary judges. **That figure is unverified — the brainstormer round was caught fabricating a separate cite in the same session, so treat this number as directional encouragement, not load-bearing evidence.** Independent re-verification required before citing it externally.

Side benefit: when ground-truth safety is always 1.0, the expected value over `{0.0..1.0}` correctly degenerates to ≈ 1.0 on safe inputs — the safety zero-variance bug self-resolves without weight updates. Effort: medium — three provider files (`evolution/judge/providers/*.py`) + rubric refactor + anchor selector. Tokenizer verification required: confirm `"0.7"` etc. are single tokens on the candidate base, or use integer 0-10 scale + divide.

### 4. Frozen-base + trained regression head (architectural safety net)

If training is still desired after #1-#3, this strictly dominates LoRA
for the regression task. Pass `(rubric_prompt + interaction)` through the
**frozen** base; extract the last-token hidden state (3072 floats);
train a `nn.Linear(3072, 4) + sigmoid` head on Gemini labels via MSE.
Trains in seconds (sklearn / pure MLX, no LoRA infra). Resulting adapter
is ~13 KB, not multi-MB. Mathematically cannot regress base quality
because base weights never change. Prior art: reward-model architectures
(BradleyTerry RM heads), [Linear Probe Penalties Reduce LLM Sycophancy
(2024)](https://arxiv.org/pdf/2412.00967), [Rubric-as-Reward](https://www.
emergentmind.com/topics/rubric-as-reward-rar). Risk: hidden-state
extraction on quantized mlx_lm Q4 may distort the residual stream; fall
back to half-precision base for feature extraction (one-time cost) if so.
Effort: medium.

### Explicitly NOT pursued (anti-patterns)

- **DPO instead of SFT.** Needs paired preference data; we have
  pointwise Gemini scores. Synthesizing pairs from pointwise scores is
  lossy.
- **Activation steering for judge calibration.** Per the [2026 field
  guide](https://subhadipmitra.com/blog/2026/activation-steering-field-
  guide/), steering works for refusal/sentiment/formality and fails for
  factual recall and numeric scoring.
- **Bigger teacher distillation.** The training set IS distilled Gemini
  judgments already; that's not the bottleneck.
- **Hybrid rule-based per dimension.** Audit-only candidate; risk of
  raising false-positive rate on safety without improving true detection.

## Conditions For A Retune Attempt

If alternatives #1-#4 above collectively fail to clear the regression bar
on the deployment stack, a retune may be attempted. It must commit
BEFORE training to ALL of:

1. **Re-measure the gap on the actual deployment target.** Run
   `bench_judge_lora.py` against an untrained base model on EACH supported
   judge backend (mlx_lm Q4, Ollama Q8_0, llama.cpp Q8_0) and record the
   per-stack Pearson vs Gemini. Only proceed if the gap is ≥ 0.10 on the
   stack actually being deployed to production.
2. **Fix the safety zero-variance bug in the training data.** All 779
   records currently have `safety = 1.0` because the Gemini-scored corpus
   contained no flagged interactions. Either exclude `safety` from the
   training loss (instruct only on the other 3 dims) or seed adversarial
   examples to give the dimension signal.
3. **Smaller LR + fewer epochs.** LR 5e-5 × 5 epochs was aggressive. Start
   from LR 1e-5 × 2 epochs and re-bench; only escalate if val loss
   plateaus above 0.30. Document each attempt's hyperparameters + bench
   result in a follow-up to this ADR.
4. **Lock the regression bar.** A new adapter must clear mean Pearson
   ≥ Base + 0.05 (i.e. ≥ 0.44 on the mlx_lm Q4 stack) AND no per-dim
   regression worse than −0.02. Adapters that improve composite but
   regress raw Pearson do not ship.

## Alternatives Considered

**Ship the adapter anyway, lower the bar.** Rejected. The whole point of
the bench was to filter against this outcome. Shipping a measured-worse
adapter would erode trust in the bench's verdict.

**Retrain immediately with smaller LR.** Deferred. The Q4-vs-Q8 stack
mismatch (point 1 in "Conditions") is more important than hyperparameter
tuning. Without the right baseline measurement we can't tell whether
the LoRA helps even when it numerically appears to.

**Drop LoRA entirely; accept Q4 base as ceiling.** Rejected as
premature. Three of the four failure modes (LR overshoot, zero-variance
safety, untested stack) are fixable. We have a working pipeline + bench;
shutting it down for one negative run wastes that investment.

**Ship a different checkpoint from the same run** (e.g. iter-100 instead
of iter-400). Rejected. Picking checkpoints to beat the bench is
p-hacking. Future runs use the canonical final-iter adapter.

## Consequences

- The `feat/judge-lora-step3-bench` PR (#470) lands the bench script on
  main. Future LoRA tuning iterations use it as the comparator. The
  script + this ADR together form a contract for what a "successful"
  judge LoRA looks like.
- The trained adapter run `20260518T071842Z-1614baf-dirty` stays on disk
  for one month as a baseline reference, then can be archived/deleted.
- `evolution/judge/ollama_judge.py` + `evolution/judge/llama_cpp_judge.py`
  remain the default local judge backends.
- This PR adds this ADR to `docs/decisions/INDEX.md`.
- Future judge-quality work begins with the cross-stack truth bench
  (alternative #1) before any retune is considered.

## References

- PR #466: judge-LoRA pipeline steps 1+2 (dataset + training driver)
- PR #469: judge-LoRA step-2.1 smoke-test gate + working defaults
- PR #470: judge-LoRA step-3 post-LoRA bench (Adapter vs Base) — this ADR's
  source of empirical data
- Bench artifact:
  `finetune/judge-lora-gemma3n/bench/20260518T071842Z-1614baf-dirty-vs-base-20260518T161525Z.json`
- 2026-05-17 n=50 Ollama Q8_0 bench (Session-Logs): the misleading
  motivating measurement
- Original motivating ADR: none — the LoRA work proceeded on session-log
  evidence + bench numbers, not on a pre-existing ADR. This ADR closes
  the loop.
