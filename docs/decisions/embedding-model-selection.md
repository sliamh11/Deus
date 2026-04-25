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

**nomic-embed-text:** In-domain 0.50-0.78, OOD 0.50-0.64. Heavy overlap -- "How to train a neural network" (OOD) scores 0.636, higher than several in-domain hits. No threshold can separate them without sacrificing 4/6 OOD queries.

**gemini-embedding-2:** In-domain 0.54-0.73, OOD 0.45-0.53. Clean separation at 0.54. Perfect recall + perfect OOD, but requires API call per query (~1500 RPD free tier).

## Decision

**Default: embeddinggemma (local Ollama).** Best recall-vs-OOD tradeoff among local models. Zero API dependency, zero cost, instant latency.

**Rejected: nomic-embed-text.** Higher recall (0.952) but OOD protection collapses (2/6). For a personal knowledge base, confidently returning irrelevant results is worse than occasionally missing a low-confidence match.

**Available but not default: Gemini API.** Perfect scores but adds API dependency and rate-limit risk for every user query. Activatable via `EMBEDDING_PROVIDER=gemini` in settings.json + one-time re-embed. Provider-aware threshold auto-detection is already implemented (PR #245).

## Consequences

- `OLLAMA_EMBED_MODEL` defaults to `embeddinggemma` in `evolution/providers/embeddings.py`
- `EMBEDDING_PROVIDER` defaults to `ollama` in `~/.claude/settings.json`
- Threshold defaults in `memory_tree.py` are calibrated per provider (Ollama vs Gemini)
- The 3 remaining recall misses (phone/submissions, career goal, current courses) are at the embedding quality ceiling -- they require better OOD-separable embeddings or a fundamentally different retrieval approach
- nomic-embed-text remains installed locally for future re-evaluation if its OOD behavior improves in newer versions
