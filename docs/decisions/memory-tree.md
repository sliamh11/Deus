# ADR: Memory tree for cold-start personal-identity retrieval

**Date:** 2026-04-15
**Status:** Accepted — Phase 1-2 shipped on `feat/memory-tree`; Phase 3-5 in progress.
**Scope:** `scripts/memory_tree.py`, vault frontmatter (`id`, `children`, `see_also`), `MEMORY_TREE.md`, `~/.deus/memory_tree.db`.

## Context

On 2026-04-15, "who do I live with" returned "I don't know" on first ask. The Persona vault had the answer but `Persona/INDEX.md` was not in cold-start context, and the existing `memory_indexer.py` retrieval did not surface it within the loaded budget. The `persona: Deus/Persona/INDEX.md` pointer in CLAUDE.md was a hint for Claude to follow, not a mechanism — and it failed the first time a personal-identity question was asked directly.

We need a deterministic entry point for personal-identity and cross-domain queries that (a) ships a small root map into every cold start, (b) retrieves relevant nodes under a token budget, and (c) surfaces cross-branch connections (e.g. "household" ↔ "movies") without duplicating content.

See [`docs/research/memory-tree.md`](../research/memory-tree.md) for the full design, research foundation (RAPTOR, ConvoMem, HNSW economics), phase plan, and ship criteria.

## Decisions

### 1. Collapsed-flat retrieval, not tree traversal

Retrieve by flat cosine over **all** indexed nodes, then expand the top hit by 1 hop of `see_also`. The tree is a human-readable map; it does not drive retrieval.

**Why:** RAPTOR's own paper ablation (collapsed-flat beats tree-descent), ConvoMem (arXiv 2511.10523, Nov 2025: flat beats RAG by 30–50pp at <150 items, multi-evidence queries collapse mem0 to 25% while flat stays 83–84%), and HNSW economics (index overhead with no recall ceiling at N<1k). **Do not revert to tree descent.**

### 2. Cross-branch via `see_also` edges, not duplicated nodes

Schema supports `child`, `see_also`, `alias_of` edge kinds. Polyhierarchy (a node belongs to multiple branches) is represented by edges, never by duplicating the node. At ~25 nodes we only ship `child` + `see_also`.

### 3. Stable ULID IDs, not paths, as primary key

48-bit timestamp + 80-bit random, lexicographically sortable. Survives renames and vault reorganisation. Paths are a display attribute, not an identity.

### 4. Embed via Ollama `embeddinggemma` (768d), not Gemini

Zero Gemini RPD cost. Gemini steady state is ~25–30 of 500 RPD; budget is already tight with evolution + generation calls. Memory-tree re-embeds on every vault edit — sending that through Gemini would double or triple daily RPD without changing retrieval quality. `embeddinggemma` is drop-in compatible with the existing `evolution.providers.embeddings.embed()` abstraction (both return ~L2-normalised 768d vectors).

### 5. Separate DB at `~/.deus/memory_tree.db`

Per [`evolution-db-split.md`](evolution-db-split.md): never share a database file between subsystems. Memory-tree owns its file; `memory.db` and `evolution.db` are untouched. Override via `DEUS_MEMORY_TREE_DB`.

### 6. Soft-delete only

Per [`no-db-deletion.md`](no-db-deletion.md): `rebuild` marks stale rows with `orphaned_at`/`expired_at`, re-verifies, and backs the DB up first. No `DELETE` or `DROP TABLE`.

### 7. Threshold calibration via labeled data, not hardcoded

`DEFAULT_LOW_THRESHOLD = 0.55`, `DEFAULT_ABSTAIN_THRESHOLD = 0.35` are **initial estimates**. Real embeddinggemma cosine scores on this corpus land in 0.34–0.62; hardcoding 0.55 would reject correct answers. Pin via `memory_tree.py calibrate` against a labeled dataset (Phase 3) and via real query logs at `~/.deus/memory_tree_queries.jsonl` (Phase 4 dogfood).

### 8. Parser accepts `summary:` as `description:` fallback

Existing vault files already use `summary:`. Accepting both as equivalent avoided a full-vault prose edit during Phase 2 — only additive frontmatter (`id:`, `children:`, `see_also:`) was needed.

## Alternatives considered

- **Tree descent (hierarchical narrowing).** Matches intuition and existing Obsidian structure. Rejected: RAPTOR ablation and ConvoMem both show flat retrieval wins at this scale. Tree descent also compounds error — a miss at the root loses everything downstream.
- **Duplicate nodes across branches.** Simpler than polyhierarchy with edges. Rejected: invariants break on edit (which copy is canonical?), and embeddings drift between copies.
- **Gemini embedding.** Same provider as `memory.db`, no new dependency. Rejected: burns ~15–25 RPD per full re-embed on a tight 500 RPD budget.
- **Reuse `memory.db`.** Avoids a third database file. Rejected: violates `evolution-db-split.md`; the indexer's `--rebuild` would wipe memory-tree data.
- **HNSW index from day one.** Standard for vector retrieval. Rejected: N<1k makes flat cosine (~5ms for 100 nodes, <50ms for 1000) faster end-to-end than HNSW's index overhead, with no recall ceiling.

## Consequences

- `MEMORY_TREE.md` (≈425 tokens) is loaded at cold start — small, bounded cost.
- Cross-branch queries ("what movies should I recommend to my roommate") work without a node being in two places.
- Re-embeddings run locally — offline-capable, no network dependency, no rate limits.
- Phase 3 calibration is a **prerequisite for merge**. Shipping defaults without calibration risks either rejecting correct answers (LOW too high) or surfacing garbage (ABSTAIN too low).
- `scripts/memory_tree.py` grows from the 1063-LOC scaffold; future changes to retrieval logic must update `docs/research/memory-tree.md` as well — the research doc is load-bearing context for the thresholds and ship criteria.
- Phase 6 (parallel budget migrations — entity extraction, contradiction detection, domain classifier → Ollama) is intentionally deferred to a separate branch. It is a related RPD-budget win, not part of this feature.
