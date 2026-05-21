# ADR: Embedding Model Selection for Memory Tree

**Date:** 2026-04-24
**Status:** Accepted
**Scope:** `scripts/memory_tree.py`, `evolution/providers/embeddings.py`

## Context

The memory tree retrieval system uses vector embeddings (768d cosine similarity) to match user queries against ~136 indexed knowledge-base nodes. The system also uses FTS5 BM25 keyword matching fused via Reciprocal Rank Fusion (PR #245).

Retrieval quality depends on the embedding model's ability to:
1. Score in-domain queries high (recall)
2. Score out-of-domain queries low (OOD abstain)
3. Separate the two distributions cleanly (no overlap)

Three models were benchmarked on a 21-query in-domain dataset + 6 OOD probes, with threshold sweeps to find each model's optimal operating point.

## Candidates

| Model | Type | Size | Dims |
|-------|------|------|------|
| embeddinggemma | Local (Ollama) | 621 MB | 768 |
| nomic-embed-text | Local (Ollama) | 274 MB | 768 |
| gemini-embedding-2-preview | API (Google) | — | 768 |

## Results

Each model at its optimal threshold configuration (abstain + gap + low):

| Model | Recall | MRR | OOD Abstain | Wrong-Confident | Config |
|-------|--------|-----|-------------|-----------------|--------|
| embeddinggemma | 0.857 | 0.654 | 5/6 | 0.000 | at=0.30, gap=0.04, lt=0.55 |
| nomic-embed-text | 0.952 | 0.610 | 2/6 | 0.000 | at=0.30, gap=0.02, lt=0.55 |
| gemini-embedding-2 | 1.000 | 0.829 | 6/6 | 0.000 | at=0.54, gap=0.02, lt=0.55 |

### Score distributions

**embeddinggemma:** In-domain 0.34-0.66, OOD 0.25-0.39. Clean separation at 0.30 threshold. Gap between worst hit and best OOD: ~0.05.

**nomic-embed-text:** In-domain 0.50-0.78, OOD 0.50-0.64. Heavy overlap — "How to train a neural network" (OOD) scores 0.636, higher than several in-domain hits. No threshold can separate them without sacrificing 4/6 OOD queries.

**gemini-embedding-2:** In-domain 0.54-0.73, OOD 0.45-0.53. Clean separation at 0.54. Perfect recall + perfect OOD, but requires API call per query (~1500 RPD free tier).

## Decision

**Default: embeddinggemma (local Ollama).** Best recall-vs-OOD tradeoff among local models. Zero API dependency, zero cost, instant latency.

**Rejected: nomic-embed-text.** Higher recall (0.952) but OOD protection collapses (2/6). For a personal knowledge base, confidently returning irrelevant results is worse than occasionally missing a low-confidence match.

**Available but not default: Gemini API.** Perfect scores but adds API dependency and rate-limit risk for every user query. Activatable via `EMBEDDING_PROVIDER=gemini` in settings.json + one-time re-embed. Provider-aware threshold auto-detection is already implemented (PR #245).

## Consequences

- `OLLAMA_EMBED_MODEL` defaults to `embeddinggemma` in `evolution/providers/embeddings.py`
- `EMBEDDING_PROVIDER` defaults to `ollama` in `~/.claude/settings.json`
- Threshold defaults in `memory_tree.py` are calibrated per provider (Ollama vs Gemini)
- The 3 remaining recall misses (phone/submissions, career goal, current courses) are at the embedding quality ceiling — they require better OOD-separable embeddings or a fundamentally different retrieval approach
- nomic-embed-text remains installed locally for future re-evaluation if its OOD behavior improves in newer versions

## Amendment — 2026-05-17: General policy "embedding model is the schema"

Phase 4 of the llama.cpp migration produced a measurement that should generalize beyond memory-tree retrieval. This amendment lifts the principle to a general policy applying to all Deus embedding surfaces (memory tree, atom retrieval, evolution loop reflection retrieval, auto-memory indexing).

**The embedding model is the schema.** Treat any change to it as a foundational data-format choice, not as a hyperparameter.

Any proposal to change the production embedding model — whether changing `OLLAMA_EMBED_MODEL`, swapping `EMBEDDING_PROVIDER` defaults, or introducing a new provider as the default — must pass **all** of the following gates:

1. **Full re-embed of stored vectors** — irreversible without pre-migration DB snapshot. Always snapshot first.
2. **Schema migration if dimension differs** — sqlite_vec `vec0` column widths are baked in. Different dim = new table.
3. **Threshold recalibration** — every distance / similarity / abstain / OOD threshold in production was tuned against the current model's distribution. Re-measure, don't blindly carry over.
4. **Benchmark snapshot before promotion** — recall@k (5 + 20), MRR when ground-truth ordering exists, OOD-abstain rate on held-out queries, wrong-confident rate, per-call latency. Compare against current production on the same fixture.
5. **Never auto-fallback across models** — `EMBEDDING_PROVIDER=auto` must only resolve to models that produced the stored vectors. Cross-model auto-fallback silently breaks retrieval.
6. **Document the decision (with measurements) in an ADR amendment** — both promotion AND non-promotion outcomes. Future investigators must not have to re-derive the work.

### When this policy applies

- Any change to `OLLAMA_EMBED_MODEL` default
- Any change to `EMBEDDING_PROVIDER` default
- Any new embedding-provider implementation becoming the default
- Any swap from one model family to another (gemma↔bge↔nomic↔...)
- Any swap within a family that changes dimension

### When this policy does NOT apply

- **Reranker (cross-encoder) swaps** — covered by `atom-retrieval-pipeline.md`; rerankers don't store state and can be swapped via env var freely
- **Generation / judge model swaps** — covered by `llama-cpp-optional-integration.md`; these don't store state
- **Adding new providers without flipping the default** — PR #453's `LlamaCppEmbeddingProvider` registration is fine; this policy only fires on default promotion

### Worked example: Phase 4 close-out

`docs/decisions/llama-cpp-optional-integration.md` Phase 4 close-out (2026-05-17) is the canonical example of this policy applied correctly to a non-promotion outcome:
- Candidate (`bge-m3` raw + prefix variants) measured vs baseline (`embeddinggemma`)
- 50-query bilingual fixture in full production pipeline (bi-encoder@20 → bge-reranker-v2-m3 → top-5)
- Result: zero pipeline recall gain
- Decision: do not migrate; document; close until new data
- Avoided ~5-10K re-embeds + schema migration + threshold sweep

### Rejected alternatives

**Allow auto-fallback across models.** Rejected. Silent data-format mismatch produces silent retrieval breakage. Better to fail loud than to be subtly wrong.

**Make embedding swaps a runtime env var (like rerankers).** Rejected. The schema-breaking nature makes the env-var-flip pattern misleading — it would be a footgun. The explicit provider-registration + benchmark-gating friction is appropriate.

**Skip the benchmark snapshot when swapping within a known-good family.** Rejected. Even minor model variants shift the distribution enough to invalidate calibrated thresholds. The benchmark cost is dwarfed by the cost of a silent retrieval regression.
