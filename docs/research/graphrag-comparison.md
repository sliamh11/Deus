# GraphRAG: Research, Comparison, and Hybrid Architecture Proposal

Comparison of graph-based RAG approaches against Deus's memory retrieval system,
with a proposed hybrid integration path.

**Date:** 2026-05-16
**Status:** Proposed (integration deferred until scaling signal)
**Scope:** `scripts/memory_tree.py`, `scripts/memory_indexer.py`
**Related ADRs:**
- [`memory-tree.md`](../decisions/memory-tree.md) -- standing retrieval architecture
- [`kb-phase2-graph.md`](../decisions/kb-phase2-graph.md) -- entity graph design
- [`atom-fallback-evaluation.md`](../decisions/atom-fallback-evaluation.md) -- atom fallback (unchanged by this proposal)

---

## 1. GraphRAG Overview

Standard vector RAG embeds document chunks and retrieves the top-k most similar
to a query. This works for narrow fact-lookup ("what is X?") but fails on
questions whose answers span multiple documents ("how do X, Y, and Z connect?"
or "what are the themes across this corpus?"). No single chunk contains the
answer, so vector similarity cannot surface it.

GraphRAG (Microsoft, arXiv 2404.16130) addresses this by building a knowledge
graph from the corpus before retrieval:

**Indexing (offline, LLM-intensive):**
1. Chunk documents and run an LLM over each chunk to extract entities (people,
   places, concepts) and their relationships -- producing graph nodes and edges.
2. Run community detection (Leiden algorithm) over the graph to find clusters of
   tightly related entities.
3. For each community, generate a natural-language summary via LLM.

**Retrieval (query time):**
- **Local search** -- for entity-specific questions: traverse the graph outward
  from matched entities, gathering neighbors, relationships, and linked chunks.
- **Global search** -- for thematic/synthesis questions: gather all community
  summaries, generate partial answers from each, aggregate via map-reduce.

The key insight: the graph captures inter-document connections that vector
similarity alone cannot see. The cost: indexing is LLM-intensive and the graph
is only as good as the extraction quality.

---

## 2. Tool Survey (mid-2026)

| Tool | License | Local/Ollama? | SDK | Differentiator |
|---|---|---|---|---|
| **Microsoft GraphRAG** | MIT | Partial (community forks) | Python | Reference impl; community reports; heaviest indexing cost |
| **LightRAG** (HKUDS) | MIT | Yes, first-class | Python | ~6000x cheaper queries than MS GraphRAG (EMNLP 2025) |
| **nano-graphrag** | MIT | Yes, Ollama adapter | Python | ~1100 LOC; most hackable; easy to embed |
| **FalkorDB** | Apache 2.0 | Yes, Docker + Redis | Python (via LlamaIndex/LangChain) | Graph-DB-native; sub-ms graph queries |
| **LlamaIndex GraphRAG** | MIT | Yes | Python + JS/TS | Property graph abstraction over 8+ backends |
| **LangChain/LangGraph** | MIT | Yes | Python + JS/TS | Query routing + decomposition pipelines |
| **Neo4j GraphRAG** | Apache 2.0 | Yes (Community ed.) | Python | Cypher-native; best if already on Neo4j |

**Best fit for Deus:** LightRAG -- MIT, native Ollama, Python library mode
(`pip install lightrag-hku`), incremental indexing, Docker + offline deployment
documented. EMNLP 2025 paper provides research backing. Supports PostgreSQL,
Neo4j, MongoDB, Redis, or flat-file storage.

---

## 3. Head-to-Head: Deus Memory System vs LightRAG

### 3.1 Token Efficiency

| Dimension | Deus | LightRAG |
|---|---|---|
| Cold-start cost | ~585 tokens (MEMORY_TREE.md, hard cap 800) | None (graph queried on demand) |
| Per-query LLM tokens | **Zero** -- retrieval is embedding + cosine, no LLM | ~100 tokens keyword extraction + 1 generation call |
| Embedding cost | 1 local Ollama call (embeddinggemma, 768d) | 1 local embedding call + vector lookup |
| Indexing LLM cost | 1 Gemini Flash call/atom (extraction) + up to 5 (contradictions) | 1 LLM call/chunk (~1,450 tokens first, ~150 incremental with caching) |
| Rate limit pressure | Ollama (unlimited) + Gemini (~15-25 of 500 daily RPD) | Ollama (unlimited) or pay-per-token |

Deus's retrieval path uses **zero LLM tokens** -- hard to beat. LightRAG's
"6000x cheaper than GraphRAG" is real but scoped to retrieval orchestration
tokens, not total cost. At indexing time, costs are comparable.

### 3.2 Performance

| Dimension | Deus | LightRAG |
|---|---|---|
| Query latency (p95) | 79-81ms (benchmarked) | ~80ms (community benchmarks) |
| Algorithm | O(N) flat cosine (Python loop) | Vector search + 2-hop graph traversal |
| At 25 nodes | ~5-8ms | Overkill -- graph overhead adds latency |
| At 1,000 nodes | ~50ms estimated | ~80ms; graph traversal starts paying off |
| At 10,000+ nodes | Degrades linearly (no ANN index) | Designed for this scale |
| Memory footprint | SQLite file (KB range) | 8GB RAM minimum recommended |

At small scale (<500 nodes), Deus is lighter and faster. At large scale (1k+),
LightRAG's indexed backends and graph traversal pull ahead.

### 3.3 Scalability

| Scale | Deus | LightRAG |
|---|---|---|
| < 100 nodes | Optimal (RAPTOR + ConvoMem validated) | Overkill |
| 100-500 nodes | Good; `see_also` expansion activates at ~50 | Starting to justify itself |
| 500-1,000 nodes | Crossover zone; O(N) cosine degrades | Sweet spot with incremental indexing |
| 1,000-10,000 nodes | Needs HNSW/ANN (not implemented) | Production-grade with Neo4j/Postgres |
| 10,000+ nodes | Not designed for this | Drop-in solution |
| Incremental updates | Hash-gated re-embed (zero cost if unchanged) | Incremental insertion without rebuild |
| Thematic/global queries | No support | Community-style global search |

Research basis for Deus's flat retrieval:
- RAPTOR (ICLR 2024): collapsed-flat beats tree descent on all benchmarks.
- ConvoMem (arXiv 2511.10523): flat beats RAG by 30-50pp at <150 items.
- HNSW crossover at ~1,000 nodes for 768d vectors.

These findings hold for personal knowledge. They do NOT hold for large external
corpora where no single retrieval pass covers a thematic query.

### 3.4 Customizability

| Dimension | Deus | LightRAG |
|---|---|---|
| Node/edge types | TEXT columns, extensible schema | Implicit schema via prompt customization |
| Embedding swap | Ollama embeddinggemma; env-var override | Full swap via `embedding_func` parameter |
| LLM swap | Gemini Flash for indexing only | OpenAI, Ollama, Gemini, Bedrock, vLLM |
| Storage backends | SQLite only (by design) | Flat files, PostgreSQL, Neo4j, MongoDB, Redis |
| Retrieval strategies | Policy layer, env-tunable thresholds, evolution auto-tuning | Two modes (local/global); reranker injectable |
| Confidence gating | Built-in (calibrated thresholds, graceful abstain) | No abstain mechanism |
| Contradiction detection | Built-in (KB Phase 2; invalidate, never delete) | None |

Deus is more customizable for personal-knowledge retrieval (confidence gating,
contradiction detection, evolution optimizer). LightRAG is more customizable for
infrastructure (storage backends, LLM providers, deployment options).

### 3.5 Accuracy Caveats

LightRAG's EMNLP 2025 paper claims ~80% on legal benchmarks vs 60-70% for
GraphRAG. However, independent blind evaluation (arXiv 2506.06331) found win
rates drop significantly under blinded conditions (e.g., 66.70% claimed vs
39.06% measured on Agriculture dataset). In some domains, naive vector RAG
outperforms LightRAG. Entity extraction quality degrades sharply below 32B
parameter models -- a real barrier for CPU-only local setups.

---

## 4. Hybrid Architecture Proposal

### 4.1 Design Principle

Keep the memory tree as the primary retrieval path for personal/identity
queries. Add LightRAG as an optional, parallel backend for external knowledge
bases where cross-document synthesis is the real need. Neither system replaces
the other; they serve different query classes.

This does NOT modify the standing retrieval architecture
([`memory-tree.md`](../decisions/memory-tree.md)) or the entity graph design
([`kb-phase2-graph.md`](../decisions/kb-phase2-graph.md)). The existing entity
graph (KB Phase 2) and contradiction detection system remain unchanged.

### 4.2 Architecture

```
query text
  |
  v
[Query Router]
  |-- personal/identity/persona --> Memory Tree (flat cosine, confidence-gated)
  |-- external/thematic/synthesis --> LightRAG (graph traversal, dual search)
  |-- ambiguous --> Memory Tree first; if low-confidence, fall back to LightRAG
  |
  v
[Result Merger] --> top-k results with source attribution
```

### 4.3 Integration Points

**Plugin point:** `EXTERNAL_NAMESPACE` in `memory_tree.py` (line 91) already
provides a namespace for non-vault content. LightRAG results could be surfaced
through this namespace, appearing as `external/lightrag/<entity>` nodes in the
unified retrieval results.

**Feature flag:** `DEUS_LIGHTRAG=1` environment variable. Off by default.
Enables the LightRAG backend and query routing.

**Storage:** LightRAG runs against its own storage (SQLite or PostgreSQL for
small-medium; Neo4j for large). No shared database with the memory tree
(consistent with [`evolution-db-split.md`](../decisions/evolution-db-split.md)).

**Embedding:** LightRAG can use the same Ollama instance for embeddings. The
extraction LLM can use Ollama (32B+ recommended for quality) or Gemini Flash.

### 4.4 Query Routing

The router classifies queries before dispatching:

| Signal | Route to |
|---|---|
| Persona triggers (`PERSONA_TRIGGERS` in memory_tree.py) | Memory Tree |
| Entity name matches a memory-tree node | Memory Tree |
| Keywords: "summarize", "themes", "across", "compare" | LightRAG global search |
| Entity name matches a LightRAG entity but not memory-tree | LightRAG local search |
| No strong signal | Memory Tree first; LightRAG fallback on low confidence |

### 4.5 Scaling Triggers

The hybrid architecture becomes relevant when ANY of:
- Memory tree node count exceeds ~500 (flat cosine O(N) starts degrading)
- User ingests an external corpus (PDFs, research papers, course materials)
- Thematic queries ("summarize everything about X") return low-confidence results

### 4.6 What LightRAG Adds

For users with large node trees (1,000+ nodes):
- **Cross-document synthesis** via community detection and global search
- **Incremental indexing** without full rebuilds
- **Production storage backends** (Neo4j, PostgreSQL) for graph-native queries
- **Thematic query support** that flat cosine cannot provide

### 4.7 What Deus Keeps

Regardless of LightRAG integration, the memory tree retains:
- **Zero-LLM retrieval** for personal/identity queries (token efficiency)
- **Confidence gating** with calibrated thresholds and graceful abstain
- **Contradiction detection** (KB Phase 2; invalidate, never delete)
- **Cold-start map** (MEMORY_TREE.md, ~585 tokens, always loaded)
- **Evolution auto-tuning** of retrieval thresholds

---

## 5. Decision

**Status:** Proposed -- integration deferred.

The research shows LightRAG is the best-fit external GraphRAG tool for Deus's
stack (MIT, Ollama-native, Python library, incremental indexing). The hybrid
architecture preserves all existing advantages while adding a path for large-
scale external knowledge retrieval.

Implementation is deferred until a concrete scaling signal:
- A user reports degraded retrieval quality at high node counts
- External corpus ingestion becomes a requested feature
- Memory tree benchmark regression at >500 nodes

Until then, Deus's flat-cosine retrieval with entity graph and confidence gating
remains the right architecture. This document is preserved as reference for when
the scaling signal arrives.

---

## 6. References

- Microsoft GraphRAG: Edge et al., *From Local to Global: A Graph RAG Approach
  to Query-Focused Summarization*, arXiv:2404.16130, 2024.
- LightRAG: Guo et al., *LightRAG: Simple and Fast Retrieval-Augmented
  Generation*, EMNLP 2025 Findings.
- RAPTOR: Sarthi et al., *RAPTOR: Recursive Abstractive Processing for
  Tree-Organized Retrieval*, ICLR 2024.
- ConvoMem: *Long-Term Memory for Conversational Agents*,
  arXiv:2511.10523, Nov 2025.
- LightRAG accuracy critique: *How Significant Are the Real Performance Gains
  of LightRAG?*, arXiv:2506.06331, Jun 2025.
- LightRAG GitHub: github.com/HKUDS/LightRAG
- nano-graphrag GitHub: github.com/gusye1234/nano-graphrag
