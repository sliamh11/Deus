# ADR: Gemma 4 12B Did Not Justify Replacing `gemma4:e4b` for Local Tasks

**Date:** 2026-06-05
**Status:** Accepted
**Scope:** Local model selection for `evolution/judge/`, `scripts/memory_indexer.py`
(atom/entity extraction), local reflexion generation, and Ollama model defaults
(`OLLAMA_MODEL`, `DEUS_OLLAMA_ATOM_MODEL`, `DEUS_OLLAMA_ENTITY_MODEL`).

## Context

Deus runs **`gemma4:e4b`** (Ollama) as the default local model across the evolution
judge (scoring production interactions), atom/entity extraction, and reflexion text
generation. Embeddings are a separate model (`embeddinggemma`, 768D) and out of scope.

On 2026-06-03 Google released **Gemma 4 12B** (a new dense size between e4b and 26b),
followed on 2026-06-05 by **QAT (quantization-aware-training) checkpoints** for the whole
family. The motivating hypothesis, driven by the announcement: *12B at q4 is **7.6 GB on
disk — smaller than our current e4b (9.6 GB)** — and "nears 26B-MoE quality," so it may be
a free upgrade.*

Evaluated on the deployment target (Apple M3 Pro, 36 GB unified memory). Pulling the new
artifacts required upgrading Ollama 0.24.0 → 0.30.5 (the new arch + QAT formats 412-reject
on older Ollama; 0.30.5 also ships a `gemma4:12b` FP-exception crash fix).

## Method

Drove **Deus's real local-model code paths** — the production judge RUBRIC + format schema
and the `memory_indexer` atom prompt + schema — at production temp/seed parity, on the
**curated datasets only** (`eval/datasets/{core_qa,safety}.jsonl`; public-repo-safe). Quality
was measured objectively: judge-**ranking** accuracy (does the model score a correct answer
above a mismatched one) + safety classification, plus extraction schema-validity. A
throwaway probe (not committed) was used; this was a recommendation exercise, not new eval
infrastructure.

## The Headline Finding

| Metric (think:false, temp/seed parity) | `gemma4:e4b` (current) | `gemma4:12b` |
|---|---|---|
| Judge ranking acc (15 core_qa, correct>mismatched) | 100% | 93% |
| Safety refusal → `safe=true` (10) | 100% | 100% |
| Throughput (warm) | **~36 tok/s** | ~16 tok/s (~2.25× slower) |
| Resident memory, loaded (`ollama ps`) | **3.3 GB** (MatFormer active sub-model) | 7.7 GB (~2.3×) |
| Disk footprint | 9.6 GB | 7.6 GB |
| Structured output | works as-is | **requires `think:false`** or hangs under JSON grammar |

**No quality advantage was detected for 12B.** Important honesty caveat: this is *absence of
evidence*, not proof of equivalence — the judge-ranking task ceiling'd out (both ≈100%), N is
small (25 cases), and no statistical test was run. The decision does **not** rest on "quality
is equal"; it rests on **"no demonstrated benefit against real, measured costs."**

Two facts reframed the motivating hypothesis as false in practice:

- **`e4b` is a MatFormer "Edge" model** (~4B *active* params despite 9.6 GB on disk) — so it
  loads only **3.3 GB resident** and decodes ~2.25× faster than the dense 12B. 12B's only win
  is *disk size*, which is irrelevant on a 36 GB box.
- **`gemma4:12b`/`12b-it-qat` are reasoning models** (`thinking` capability). With thinking
  *on* (the Ollama default) they hang/loop under constrained decoding (observed 150–300 s
  timeouts). They only behave reliably with `think:false`.

## Decision

1. **Keep `gemma4:e4b` as the default local model.** Do not adopt `gemma4:12b` or
   `12b-it-qat` for the current text tasks (judge / extraction / reflexion).
2. **Generalizable ruling:** local-model selection is **benchmark-gated on the real task and
   the actual deployment stack** — and for these tasks, **model size/swap is not the lever;
   measurement discipline and wiring robustness are.** Third independent data point for this
   pattern (1: [judge-lora-specialization.md](judge-lora-specialization.md); 2: the 2026-06-04
   cross-family-reviewer shelving; 3: this ADR).
3. **Reconsider 12B only when** (a) a genuinely *local multimodal* task appears — 12B is
   vision/audio/tools-capable, unused by today's text-only local work (already-pulled
   `qwen3-vl:8b` / `minicpm-v` are alternatives), or (b) **reflexion** (freeform generation,
   *not measured here*) is shown to have a quality gap that 12B clears — A/B it via the
   existing per-surface override, not a global default switch.
4. **The full QAT matrix was deliberately not run.** If the dense 12B shows no advantage over
   e4b, its smaller/lossier QAT variants will not either (predictable → skipped per
   "predict-before-running").

## Consequences (independent of the model choice → tracked separately)

These surfaced during the evaluation and stand regardless of the Gemma 4 decision:

1. **Silent extraction leak in production (own ticket, live bug today).** `e4b` is
   thinking-capable, production extraction does **not** set `think:false`, and the model was
   observed emitting `text`-less atoms that the defensive filter in `memory_indexer.py`
   silently drops — i.e. real extraction may be quietly under-producing now. Highest priority;
   not a Gemma 4 issue.
2. **`think:false` generalization.** The gemma4 family has thinking **on by default**
   (documented in vault memory `ollama-quirks.md`, 2026-04-16; symptom if missed: empty
   `response` with `done_reason=length` — exactly the hang observed here). The fix is the
   Ollama API request-body key **`"think": false`** — a *different mechanism* from the Qwen
   `/no_think` *prompt suffix* that `ollama_judge.py:59-61` currently applies. Today only
   `qwen` gets its suffix; generalize so gemma4 (and any thinking-capable model) gets
   `think:false` on every structured-output call.
3. **Atom schema bug.** `text`/`category` are marked *optional* in the `memory_indexer.py`
   atom schema — should be `required` so degenerate atoms fail loudly instead of being dropped.
4. **Judge safety gap (needs red-team).** Both models flagged only ~50–60% of compliance-shaped
   responses as unsafe in this probe. This may be a weak test stub or a real rubric gap — the
   data can't distinguish them; security defaults to paranoid → warrants a proper safety eval.
   This is a *judge/rubric* problem, not a model-selection one.

## Alternatives Considered

- **Adopt 12B anyway.** Rejected: it pays ~2.25× latency, ~2.3× resident RAM, and a required
  `think:false` code change, for no measured benefit. You don't replace a working production
  component for a costlier challenger that isn't measurably better.
- **Fine-tune a small local model on our own data (Unsloth / MLX) instead of swapping.**
  **Open question, not closed.** The prior judge-LoRA result ([judge-lora-specialization.md](judge-lora-specialization.md),
  2026-05-18) was a *flawed* negative: trained on a gap that was a Q8_0+Ollama measurement
  artifact (never re-measured on the deployment stack), aggressive hyperparameters (LR 5e-5 ×
  5 epochs), and a broken safety-zero-variance training set — on the *older* Gemma-3n-E4B, not
  today's `gemma4:e4b`. That ADR documents explicit retry preconditions. Separately, an
  earlier and **distinct** Gemma 4 fine-tuning pipeline was built 2026-04-09 to offload Gemini
  *generation* usage (not judge scoring) and parked because volume didn't justify the ~10 h
  train — it is unrelated to the judge-LoRA negative result. On this M3 Pro the training path
  is **MLX (`mlx_lm`/`mlx-tune`)** — which the existing judge-LoRA pipeline already uses;
  **Unsloth itself is CUDA/Triton-bound and not runnable locally on Apple Silicon today**
  (verify — fast-moving). Gated by the judge-LoRA preconditions + ROI (judge volume ≤ ~60/day).
- **Build reusable committed eval infrastructure for model bake-offs.** Not done — the ask was
  a recommendation, and committed infra has no current second caller ("don't build what has no
  caller"). A deliberate future choice if model bake-offs recur.

## References

- [Introducing Gemma 4 12B](https://blog.google/innovation-and-ai/technology/developers-tools/introducing-gemma-4-12b/)
  · [Gemma 4 QAT checkpoints](https://blog.google/innovation-and-ai/technology/developers-tools/quantization-aware-training-gemma-4/)
- Trilogy — same "size isn't the lever / measure-first" pattern:
  - [judge-lora-specialization.md](judge-lora-specialization.md) — fine-tuning the judge did not beat base.
  - `Research/2026-06-04-cross-family-reviewer-model-tier-exhaustion.md` (vault) — local cross-family reviewer shelved; 5 configs / 2 families / 4× size fail identically (local-tier capability gap).
  - `Research/2026-06-04-local-model-runtime-selection-m3pro.md` (vault) — "the task is the bottleneck, not the model."
- Parked generation pipeline: `Session-Logs/2026-04-09/gemma4-finetune-pipeline.md` (vault).
- Full session write-up: `Session-Logs/2026-06-05/gemma4-benchmark-scoping.md` (vault).
