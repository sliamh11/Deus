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

## Addendum — 2026-06-06: Evidence-Backing Pass + Two Refinements

**Status unchanged** (Accepted; keep base Gemma-3n-E4B, do not adopt the adapter). This
addendum records a `/research` verification of the training concepts behind this ADR against
primary literature, and adds two refinements the original did not capture. It does **not** alter
the Decision, the retune conditions, or any text above.

A 2026-06-06 deep-research pass verified the foundational fine-tuning concepts (LoRA, QLoRA,
SFT/DPO/RLHF, GRPO, distillation, QAT, reward-modeling, MoE) against their primary papers, then
applied the verified literature back to this ADR. **Verdict: the literature validates this ADR's
alternatives #1/#3/#4 as the textbook-correct moves** — it does not overturn the decision. Full
note + evidence map: `Second Brain/Deus/Research/2026-06-06-finetuning-training-concepts-evidence-and-judge-lora-analysis.md` (vault).

### Refinement 1 — The failed run was a category error, not only an overshoot

The ADR above attributes the adapter's regression to LR overshoot (5e-5 × 5 epochs) over-fitting
658 records. That is a contributing cause, but the deeper issue is an **objective mismatch**: we
used a **generation method** — SFT-LoRA minimizing next-token cross-entropy over the JSON answer
tokens — to solve a **regression target**: predict a scalar score. The loss optimized (token
likelihood) is not the metric evaluated (score Pearson / MAE).

The reward-model literature confirms a scoring objective's *native* formulation is a **scalar head
+ a ranking/regression loss** trained on comparisons — architecturally distinct from
autoregressive generation:

- Ouyang et al. 2022, *InstructGPT* ([arXiv:2203.02155](https://arxiv.org/abs/2203.02155)) — the
  reward model is the base transformer + a **scalar linear head**, trained with a Bradley-Terry
  pairwise loss.
- Stiennon et al. 2020, *Learning to summarize from human feedback*
  ([arXiv:2009.01325](https://arxiv.org/abs/2009.01325)) — established the scalar-reward-model-
  from-comparisons pattern for text.

**Consequence for this ADR (reinforces, does not change, the "Preferred Over Retune" ordering):**
alternative **#4 (frozen-base + trained regression head)** is not merely a safety net to reach for
*if* #1–#3 fail — it is the **objective-correct first-line approach** for a scoring task. A
frozen-base head **mathematically cannot regress base quality** (base weights never change), which
is precisely the failure mode the LoRA run exhibited (regression on every measured dimension). The
documented retune path (Conditions For A Retune Attempt) remains valid but stays *last* — an
SFT-LoRA retune on the scoring objective should be attempted only if both the regression-head (#4)
and decoding-level (#3) reformulations are shown insufficient on the deployment stack.

The safety zero-variance failure (retune precondition #2) is the same lesson in miniature: a
regression (MSE) loss produces **no gradient signal when all training labels are identical** —
here the degenerate ground-truth `safety = 1.0` on every record, not the method, is the cause —
so the dimension was unlearnable by construction, independent of method or hyperparameters.

### Refinement 2 — GRPO: now enumerated, but dominated here and CUDA-gated

The "Explicitly NOT pursued (anti-patterns)" list rejected DPO (needs paired preference data; we
have pointwise scores) but did not enumerate **GRPO** (Shao et al. 2024, *DeepSeekMath*,
[arXiv:2402.03300](https://arxiv.org/abs/2402.03300); DeepSeek-AI 2025, *R1*,
[arXiv:2501.12948](https://arxiv.org/abs/2501.12948)). GRPO optimizes against a
**programmable / verifiable reward** using a group-relative advantage, with **no critic network
and no paired preference data** — so, unlike DPO, it *would* accept our pointwise Gemini score as
a reward (`reward = −|judge − gemini|`), sidestepping the paired-data wall.

It is nonetheless **not pursued**, for two precise reasons:

1. **Dominated for this objective.** RL earns its keep when the output **cannot be directly
   supervised** (e.g. math reasoning: the final answer is verifiable, the reasoning path is not).
   For our judge we **can** directly supervise — we hold the target Gemini score — so **direct
   regression (#4) is more sample-efficient and strictly dominates** GRPO for pure pointwise
   scoring. GRPO becomes relevant only if the judge moves to a **reasoning-heavy** formulation
   (score verifiable, rationale not).
2. **Not runnable locally.** The GRPO tooling (TRL / Unsloth) is CUDA/Triton-bound and does not
   run on the M3 Pro deployment hardware (per the 2026-06-06 Unsloth platform evaluation). It
   would require a cloud/CUDA GPU regardless.

**Keep GRPO on the radar** for a future reasoning-judge formulation on cloud hardware; it is not a
candidate for the current local pointwise-scoring task.

### References added by this addendum

- Vault research note: `Second Brain/Deus/Research/2026-06-06-finetuning-training-concepts-evidence-and-judge-lora-analysis.md`
- Ouyang et al. 2022 ([arXiv:2203.02155](https://arxiv.org/abs/2203.02155)); Stiennon et al. 2020
  ([arXiv:2009.01325](https://arxiv.org/abs/2009.01325)) — reward model = scalar regressor.
- Shao et al. 2024 ([arXiv:2402.03300](https://arxiv.org/abs/2402.03300)); DeepSeek-AI 2025
  ([arXiv:2501.12948](https://arxiv.org/abs/2501.12948)) — GRPO.
- 2026-06-06 session log: `Session-Logs/2026-06-06/unsloth-training-platforms-and-gemma4-adr.md`
  (Unsloth / MLX platform finding).

**Cite hygiene:** this addendum adds only the four arXiv-verified citations above. It does **not**
reproduce the G-Eval / Alves 2025 citations from alternative #3 (out of scope — not re-verified in
this pass), nor the Arize Spearman 0.51→0.66 figure (explicitly flagged unverified in the
original).

## Addendum — 2026-06-07: Clean-fixture model ladder (executes alternative #2)

Built the missing clean benchmark and ran the **model-size ladder** (alternative **#2**,
"replace the base model, don't tune it") on the **Ollama deployment stack**. Tooling:
`evolution/build_judge_benchmark.py` (sampler + fresh Gemini labeler) →
`evolution/benchmark_judge.py --fixture` (per-dim + composite + threshold P/R + bootstrap CIs).

**Not comparable to the tables above.** This is a fresh baseline on a *new* fixture (n=200,
current structured rubric, Ollama path), NOT the 2026-05-18 numbers (n=40, old rubric, mlx_lm Q4).
It does **not** execute alternative #1 (cross-*inference-stack* bench across mlx_lm/Ollama/llama.cpp);
`benchmark_judge.py` is Ollama-only.

**Fixture (local, gitignored — contains real prompts/responses):** 200 interactions sampled
stratified across composite bands, **freshly labeled by the production Gemini judge** under the
current rubric **with the persona digest injected** (so personalization is grounded, not
hallucinated — see #710), then
graded **digest-symmetric** on the local Ollama judge. Data-quality finding: ~60% of stored
interactions have an **empty response** (ungradable; the source of the all-0.0 composite cluster),
and these are almost entirely the `runtime` surface — so the gradable pool is effectively all
`claude_code`. Empty responses on the runtime/chat surface are a possible separate data-capture gap.

**Result (composite agreement vs Gemini, 200 rows, bootstrap 95% CI):**

| Model | Composite r | 95% CI | Threshold@0.6 P/R/F1 | Latency |
|-------|-------------|--------|----------------------|---------|
| gemma4:e4b (deployed) | 0.655 | [0.576, 0.726] | 0.91 / 0.50 / 0.64 | 3.5 s |
| **gemma4:12b** | **0.742** | [0.680, 0.795] | 0.98 / 0.53 / 0.69 | 9.2 s |
| gemma4:26b | 0.575 | [0.492, 0.649] | 0.92 / 0.45 / 0.60 | 4.5 s |

(Latency: 26b being *faster* than 12b is not a typo — it reproduced across two independent runs
on this M3 Pro. Cause unconfirmed, likely Metal/quantization scheduling; it drives no decision
since 26b is rejected on agreement. 12b's 9.2 s/call is moot for UX: the hot-path judge is
fire-and-forget (`mcp_server.py` `asyncio.create_task`), so judge latency is off the user's path.)

Paired bootstrap (same rows): **12b − e4b = +0.088, CI [+0.026, +0.151], P=1.00** (significantly
better); **26b − e4b = −0.076, CI [−0.156, −0.003], P=0.02** (significantly **worse**).

**Findings:**
1. **12b is the best local judge; 26b regresses below e4b.** Bigger ≠ better for judge agreement —
   the larger model agrees *less* with the Gemini reference on composite, quality, and tool_use.
2. **A prior n=37 pilot was overturned.** On the small set 26b looked best (quality 0.782); at
   n=200 that was underpowered noise. This is the concrete payoff of building a powered fixture
   before acting — and a standing caution against deciding judge changes on <50 examples.
3. **Safety is now tested.** The committed synthetic probe set (`evolution/fixtures/judge_safety_probes.jsonl`,
   n=13 smoke) is detected perfectly by all three models (P/R/F1 = 1.00). The main fixture itself
   remains near-constant on safety (real interactions are overwhelmingly safe).
4. **Personalization is now gradable** (digest-symmetric labeling+grading, validating #710):
   per-dim Pearson e4b 0.22 / 12b 0.37 / 26b 0.42 — modest but real, no longer a hallucination artifact.
5. **Shared limitation: threshold recall ≈ 0.5.** All models flag only ~half the interactions Gemini
   would (high precision ≥0.91 — they rarely false-flag). Improving reflexion *recall* is a rubric/
   prompt lever, not a model-size lever.

**Decision:** prefer **gemma4:12b** as the judge model, opt-in via a new `EVOLUTION_OLLAMA_JUDGE_MODEL`
override (the +0.088 gain is significant and well-powered). Because the hot-path judge is
**fire-and-forget** (`mcp_server.py` `asyncio.create_task`), 12b's 9.2 s/call carries **no UX cost** —
so the override applies to **both** the hot and batch judges; running 12b everywhere keeps stored
labels **consistent** (mixing models would contaminate agreement comparisons). **The default stays
e4b** (the override is a true no-op until set), reconciling with the 2026-06-05 ADR
([gemma4-12b-local-model-evaluation.md](gemma4-12b-local-model-evaluation.md)), which keeps e4b as
default and sanctions this per-surface override *mechanism* (the ADR exemplified it for reflexion;
#713's evidence is what justifies the judge surface). **Do not adopt 26b.** The override ships
separately as the opt-in knob in **PR #718**, gated on this measurement.
