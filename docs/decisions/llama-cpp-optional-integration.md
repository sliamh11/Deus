# ADR: llama.cpp as Optional Integration, Not Default Replacement

**Date:** 2026-04-26
**Status:** Accepted
**Scope:** `.claude/skills/add-llama-cpp/`, memory embeddings, local generation/judging

## Context

Deus evaluated `llama.cpp` as a possible replacement for Ollama after a local
GGUF setup and an isolated embedding parity benchmark. The benchmark used a
temporary memory-tree environment and did not modify production embedding
databases.

The result was mixed:

- `llama.cpp` embedding calls were much faster than Ollama.
- The `llama.cpp` and Ollama embedding vector spaces were incompatible despite
  both returning 768-dimensional vectors.
- Existing Ollama-backed vectors cannot be reused by a `llama.cpp` embedding
  provider.
- After provider-specific threshold calibration, `llama.cpp` embeddings were
  viable but not clearly safer than the existing Ollama default.

Generation and judge use may still be valuable, but that is a separate
decision from memory embeddings.

## Decision

Keep Ollama as the default memory embedding and local judge path.

Ship `llama.cpp` only as an optional host integration skill:
`/add-llama-cpp`.

The skill may install and verify a local `llama-server` endpoint and document
how to connect it to Deus if the current checkout contains optional
`llama_cpp` provider wiring. It must not silently change the default memory,
judge, or generation providers.

## Required Guardrails

- A `llama.cpp` embedding switch requires a full provider-specific re-embed.
- The switch must include calibrated thresholds for that provider and model.
- The switch must include a benchmark snapshot showing recall, MRR, OOD
  abstain, wrong-confident rate, and latency.
- Runtime/provider source changes must be shipped separately from the skill PR.
- Existing Ollama vectors must be treated as provider-specific data, not as a
  portable embedding cache.

## Alternatives Considered

**Replace Ollama with llama.cpp by default.** Rejected. The benchmark did not
justify a product-wide default change, and embedding-vector incompatibility
would make this a migration with data consequences.

**Drop llama.cpp entirely.** Rejected. Local GGUF serving is still useful for
experiments and may become a better generation or judge path later.

**Merge all provider wiring now.** Rejected for this change. Optional provider
code should be evaluated independently and should not be bundled with the
install skill.

## Consequences

- Deus remains stable for existing users and keeps Ollama-backed memory
  behavior unchanged.
- Users who want local GGUF experiments can opt in through `/add-llama-cpp`.
- Future `llama.cpp` provider work must be explicit about which surface it
  changes: generation, judging, or embeddings.
- Memory embedding changes remain benchmark-gated and provider-aware.

## Amendment — 2026-05-17: Phase 3 (router mode + per-surface model env vars)

Following PR #452 (agent runtime), PR #453 (eval-side providers), and a Stage 1/Stage 2 model benchmark series, Phase 3 adds **plumbing only** — per-surface model env vars + router-mode skill support. No default model swaps.

**Stage 2 findings summary** (full data: `Research/judge-bench-*-2026-05-17.json/log`):

- **MTP delivers ~4× speedup** when fits; Qwen3.6-27B+MTP doesn't fit sustainably on 36 GB M3 Pro (KV cache + draft buffers OOM under parallel load)
- **Smaller MTP models** (Qwen3.5-4B, Prometheus-7B class) have llama-server reliability issues on the current default config (`-c 4096 -np 4 --jinja`)
- **gemma4:e4b remains best on quality** on chat-only fixture: Pearson 0.744 vs Prometheus-2-7B's 0.610 (Composite 0.852 vs 0.773)
- **Prometheus is 3× faster** (9.0s vs 26.2s) and stable on its native template; rubric architecture mismatch keeps its Pearson lower
- **Bench fixture quality dominates** model differences — earlier all-zero ground-truth was 100% fixture artifact (empty-response reflection rows). Real chat data gave both judges much higher Pearson.

**Phase 3 architecture decisions:**

- **Per-surface env vars** (`LLAMA_CPP_AGENT_MODEL`, `LLAMA_CPP_GEN_MODEL`, `LLAMA_CPP_JUDGE_MODEL`, `LLAMA_CPP_EMBED_MODEL`) each fall back to `LLAMA_CPP_MODEL` for back-compat with PR #452/#453.
- **Container injection**: host sends both `LLAMA_CPP_AGENT_MODEL` AND `LLAMA_CPP_MODEL` (Approach A — safety net). The container's hardcoded `'gpt-3.5-turbo'` fallback is REMOVED; empty model is correct for router mode.
- **Skill `/add-llama-cpp`**: updated to launch llama-server with `--models-dir ~/.cache/huggingface --models-max 4` (router mode). Cross-platform: the path is HF convention (works on macOS + Linux); `HF_HOME` override documented.
- **Defaults unchanged**: this PR is plumbing only. gemma4:e4b on Ollama remains the production judge; Ollama embeddinggemma remains the embedding default. The architecture supports swap-when-ready via single env var.

**Why not commit to a model swap in Phase 3:**
- gemma4:e4b is the best speed/quality balance on this hardware per Stage 2
- Prometheus-2-7B is viable for batch/throughput-heavy workloads but worse on per-call quality
- MTP-capable models suffer from reliability + memory constraints on 36 GB
- Phase 3 architecture lets the user pick empirically per surface when better candidates exist

**Out of scope for this amendment:**
- Phase 4 (embedding migration to bge-m3 or similar) — DEMOTED to LOW priority per Stage 1/2 findings; multilingual reranker (PR #459) already captures most of the gain
- `deus llama` foreground CLI shorthand (PR #6 from earlier roadmap)
- Per-dimension Prometheus architecture (interesting but not warranted at current scale)

## Phase 4 close-out — 2026-05-17: Embedding migration parked indefinitely

**Decision**: Do **NOT** migrate from `embeddinggemma` (Ollama) to `bge-m3` (or any other bi-encoder candidate). Measured zero benefit in the realistic production pipeline.

**Measurement** (`Research/judge-bench-phase4-2026-05-17.json`):

Head-to-head on the 50-query bilingual fixture (40 English + 20 Hebrew, 1709 atoms, full production pipeline = bi-encoder@20 → bge-reranker-v2-m3 → top-5):

| Bi-encoder | Bi-recall@5 | Bi-recall@20 | Pipeline recall@5 | Hebrew | English |
|---|---|---|---|---|---|
| embeddinggemma (baseline) | 90.0% | 98.0% | **49/50 (98.0%)** | 19/20 | 30/30 |
| bge-m3 raw | 92.0% | 100.0% | 49/50 (98.0%) | 19/20 | 30/30 |
| bge-m3 +prefix | 90.0% | 100.0% | 49/50 (98.0%) | 19/20 | 30/30 |

**The multilingual reranker (PR #459) absorbs all bi-encoder differences.** While bge-m3 produces better candidate pools (100% vs 98% bi-encoder recall@20), the reranker is the deciding stage and the single remaining miss trips it the same way regardless.

**The single remaining pipeline miss** is `'איך לטפל ב-RTL במסמכים של פייתון'` (Hebrew "how to handle RTL in python-docx documents"). Failure mode differs by embedder:
- embeddinggemma: "outside-top-20" (bi-encoder couldn't surface target 1371)
- bge-m3 variants: "rerank-failed" (target IS in top-20 but reranker picks 1373 / 2240 / 1777 instead)

**Cost-benefit**: zero pipeline gain vs ~5-10K re-embeds + 768d→1024d schema migration + threshold recalibration + benchmark snapshot + irreversible-without-backup. Decisively negative.

**If we ever want to recover that one miss**, the lever is **not** the embedder:
1. Query rewriting: transliterate Hebrew `"פייתון"` → English `"python"` before retrieval
2. Expand reranker candidate pool: `DEUS_RERANKER_CANDIDATES=30` (currently 20)
3. Try a stronger multilingual cross-encoder if one emerges

**This decision supersedes the "Idea #2C" line in the 2026-05-16 brainstorm and the corresponding pending item.** Phase 4 is closed unless future measurements on different data overturn this finding.
