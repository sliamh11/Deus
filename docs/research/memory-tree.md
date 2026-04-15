# Memory Tree: Research, Design, and Phase Plan

Hierarchical memory navigation for cold-start personal-identity retrieval in Deus.

**Date:** 2026-04-15
**Status:** Phase 1-2 shipped on `feat/memory-tree`; Phase 3-5 in progress.
**ADR:** [`docs/decisions/memory-tree.md`](../decisions/memory-tree.md)
**Session log:** `Second Brain/Deus/Session-Logs/2026-04-15/memory-tree-design-and-phase1-2.md`

---

## 1. Problem

On 2026-04-15, "who do I live with" returned "I don't know" on first ask. The Persona vault (`Persona/life/household.md`) held the answer; `Persona/INDEX.md` pointed at it. Neither was in cold-start context.

The CLAUDE.md line `persona: Deus/Persona/INDEX.md` is a **hint** for the agent to follow, not a mechanism. `memory_indexer.py` embeddings did not surface `household.md` with the loaded context budget. The first-ask failure mode is unacceptable for personal-identity questions — those are exactly the questions that should never need a second-pass clarification.

We need:
1. A deterministic entry point (a root map) always present at cold start.
2. A retrieval layer that finds the right node under a token budget.
3. Cross-branch linking (household ↔ taste/movies, study ↔ work-style/learning) without duplicating content.
4. Graceful abstain when the query is out-of-distribution.

---

## 2. Research foundation

### 2.1 RAPTOR (Sarthi et al., ICLR 2024)

RAPTOR builds a recursive tree of summaries over a document corpus. The paper evaluates two retrieval modes:
- **Tree traversal:** descend from root, pick top child at each level.
- **Collapsed flat:** flatten all summary + leaf nodes, do flat cosine over the union.

**The ablation finding:** collapsed-flat matches or beats tree traversal on every benchmark. The tree's value is at **index time** (generating summary nodes gives you queryable abstraction levels), not at retrieval time.

**Implication for us:** build the tree as a human-readable map, but retrieve flat.

### 2.2 ConvoMem (arXiv 2511.10523, Nov 2025)

Evaluates conversational memory systems on small personal corpora (<150 items).

- **Flat retrieval beats RAG by 30–50pp** at this scale.
- **Multi-evidence queries** (questions requiring 2+ facts to combine) collapse mem0 (a tree-structured memory system) to 25% accuracy, while flat retrieval holds 83–84%.
- Paper's phrasing: "simplicity is not merely sufficient but superior."

**Implication for us:** at Deus's scale (~25 nodes initially, maybe 100 in a year), flat retrieval is not a compromise — it is the right answer.

### 2.3 HNSW economics at N<1k

HNSW (the standard ANN index for vector retrieval) has an index build cost and per-query overhead that only amortises at large N. Benchmarks in sqlite-vec issues and the HNSWlib README converge: flat cosine is faster end-to-end below ~1000 vectors of dimension 768.

Concrete numbers for our target (100 nodes, 768d, on an M-series Mac):
- Flat cosine: ~5–8ms per query (Python loop, negligible memory).
- HNSW: ~2ms per query + 30–80ms index load + index file on disk.

**Implication:** don't index. Load vectors into memory, cosine loop, done.

### 2.4 Polyhierarchy in personal knowledge systems

Zettelkasten (Luhmann), Obsidian, Org-roam, and Dendron all converge on the same pattern for nodes that belong to multiple branches:
- **Stable ID** (not path).
- **Many-to-many membership** via an edges table or explicit bidirectional links.
- **Backlinks are rendered, not stored** (always derivable).

**Implication:** `edges` table with `{kind: child|see_also|alias_of}` is the canonical model. Frontmatter `see_also:` is the source of truth; the table is projected from it on `build`/`reembed`.

---

## 3. Architecture

### 3.1 Storage

- `~/.deus/memory_tree.db` — SQLite + sqlite-vec, own file per [`evolution-db-split.md`](../decisions/evolution-db-split.md).
- Schema (key tables):
  - `nodes` — id (ULID, PK), path, title, description, type, level, content_hash, embedded_at, orphaned_at, expired_at.
  - `edges` — src_id, dst_id, kind (`child`|`see_also`|`alias_of`).
  - `vectors` — sqlite-vec virtual table, 768d.
  - `queries_log` — query text, top hit path, confidence, returned paths, latency_ms, timestamp.
  - `calibration` — calibrated_at, low_threshold, abstain_threshold, sample_count, notes.
- Soft-delete only (`orphaned_at`, `expired_at`) per [`no-db-deletion.md`](../decisions/no-db-deletion.md).

### 3.2 Retrieval (3-phase)

```
query text
  ↓ embed (embeddinggemma, 768d)
  ↓ phase 1: flat cosine over all active nodes → top-k
  ↓ phase 2: expand top hit by 1 hop of see_also → score neighbours, re-sort
  ↓ phase 3: if top score < abstain_threshold → return "abstain" (caller falls back to Persona/INDEX.md)
result: { results: [{path, score, ...}], confidence, abstained }
```

Phase 1 handles single-node queries ("where do I live"). Phase 2 handles cross-branch queries ("movies to recommend to my roommate" — `household.see_also = [taste/movies]`). Phase 3 prevents confident-wrong answers on out-of-distribution queries.

### 3.3 Cold-start footprint

`MEMORY_TREE.md` (~425 tokens) is loaded at every cold start. It contains:
- Root node description ("This is a map of Liam's personal + project memory").
- Direct children with one-line descriptions (Persona/INDEX, CLAUDE, INFRA, STUDY).
- Instructions: "For factual personal questions, call `memory_tree.py query <text>` and fall back to Persona/INDEX.md on low confidence."

### 3.4 Observability

- **JSONL query log** at `~/.deus/memory_tree_queries.jsonl` — every query, its top hit, confidence, latency. Append-only.
- **`report --low-confidence`** — weekly review surface. Low-confidence hits are the primary signal for description tuning: if `Persona/life/household.md` keeps getting missed for "who lives with me", the fix is a better `description:` field, not a retrieval algorithm change.
- **`graph`** — text-mode visualiser of the see_also topology. Cheap debugging.

---

## 4. Key decisions

See the ADR for the authoritative list. Summary:

| # | Decision | Why |
|---|----------|-----|
| 1 | Collapsed-flat, not tree descent | RAPTOR + ConvoMem + HNSW all say flat wins at this scale |
| 2 | `see_also` edges, not duplicated nodes | Polyhierarchy is canonical; duplication breaks invariants |
| 3 | Stable ULID IDs, not paths | Survives renames |
| 4 | Ollama `embeddinggemma`, not Gemini | Zero RPD cost; Gemini budget is tight |
| 5 | Separate DB | `evolution-db-split.md` applies |
| 6 | Soft-delete only | `no-db-deletion.md` applies |
| 7 | Calibrated thresholds, not hardcoded | Cosine range 0.34–0.62 — 0.55 default would reject correct answers |
| 8 | Parse `summary:` as `description:` fallback | Zero vault prose churn |

---

## 5. Phase plan

### Phase 1 — Scaffold (shipped, commit `59cdac1`)

- `scripts/memory_tree.py` — 7 subcommands: `build`, `query`, `reembed`, `check`, `graph`, `calibrate`, `benchmark`.
- SQLite + sqlite-vec at `~/.deus/memory_tree.db`.
- 3-phase retrieval (flat → graph expansion → abstain).
- `scripts/tests/test_memory_tree.py` — 39 tests, stubbed sparse-bag-of-words embed.

### Phase 2 — Vault content (shipped, commit `006a528`)

- `MEMORY_TREE.md` (root map, ≈425 tokens).
- Frontmatter added to Persona/INDEX, CLAUDE, INFRA, STUDY, and selected leaves.
- 13 nodes indexed, 6/6 motivating queries correct with `embeddinggemma`.
- Parser accepts `summary:` as `description:` fallback.

### Phase 3 — Calibration

- Author labeled query dataset (~50 items, distribution below).
- Run `memory_tree.py calibrate` to pin LOW/ABSTAIN thresholds.
- Run `benchmark` — compare V0 (flat-only, no expansion) vs V3 (current: flat + see_also + abstain).

**Dataset composition (50 queries):**
- 20 single-node (direct match, e.g. "where do I live").
- 15 multi-node (2+ relevant nodes, e.g. "study techniques for math").
- 10 cross-branch (answer requires see_also hop, e.g. "movies for roommate").
- 5 abstain (out-of-distribution, e.g. "airspeed of an unladen swallow").

**Ship criteria:**
- Recall@5: ≥0.90 single-node, ≥0.75 multi-node, ≥0.70 cross-branch.
- Wrong-confident rate: <5% (confident hit that's actually wrong).
- Cold-start footprint: ≤800 tokens.
- p95 latency: ≤200ms.

**Results (2026-04-15, fixture `scripts/tests/fixtures/memory_tree_queries.jsonl`):**

| Metric | Target | Actual |
|---|---|---|
| single-node recall@5 | ≥0.90 | **1.00** (20/20) |
| multi-node recall@5 | ≥0.75 | **1.00** (15/15) |
| cross-branch recall@5 | ≥0.70 | **1.00** (10/10) |
| abstain accuracy | 100% | **1.00** (5/5) |
| wrong-confident rate | <5% | **0.0%** |
| p95 latency | ≤200ms | **79.0ms** |
| cold-start footprint | ≤800 tok | ~585 tok (MEMORY_TREE.md) |
| LOW / ABSTAIN (fitted) | — | 0.65 / 0.35 |

MRR@5: 1.000 single · 0.822 multi · 0.950 cross-branch. All ship criteria met on synthetic calibration. Real-world calibration will re-fit thresholds from `~/.deus/memory_tree_queries.jsonl` once Phase 4 dogfood has accumulated a week of query logs.

### Phase 4 — Startup integration, gated by `DEUS_MEMORY_TREE=1`

- Load `MEMORY_TREE.md` into cold-start `CONTEXT` (`deus-cmd.sh:860`; mirror in `deus-cmd.ps1`).
- Append identity paragraph: "For factual personal questions, call `memory_tree.py query` and fall back to Persona/INDEX.md on low confidence."
- Add pointer line to vault `CLAUDE.md` for non-wrapper launches.
- Feature-flagged so calibration and dogfood run in parallel.

### Phase 5 — Continuous indexing

- PostToolUse hook in `~/.claude/settings.json`, matching `Write|Edit|MultiEdit`; checks if path is under `$VAULT/**` and calls `memory_tree.py reembed <file>`. Hash-gated — most edits cost 0.
- `scripts/stop_hook.py` — mtime-drift scan capped at 5 re-embeds per invocation. Catches external edits (Obsidian mobile, manual).

### Phase 6 — Parallel budget migrations (separate branch)

Not part of memory-tree. Listed for context only. Moves entity extraction → `gemma4:e4b`, contradiction detection → `gemma4:e2b`, domain classifier fallback → `qwen3.5:4b`. Saves ~15–25 Gemini RPD. Adds daily Ollama daemon health ping to startup-gate.

### Phase 7 — Observability + tuning loop

- Weekly `memory_tree.py report --low-confidence` review.
- Action is **tuning `description:` fields on missed nodes**, not algorithm changes.
- Re-run calibration after every ≥10 new nodes or every 30 days, whichever first.

### Merge gate

`feat/memory-tree` merges to `main` after Phase 3 (calibrated) + Phase 4 (dogfooded for ≥1 week) + Phase 5 (re-embeds stay correct under continuous editing).

---

## 6. Open questions

- **Alias-of edges.** Schema supports `alias_of` but no node uses it yet. Candidate: "roommate" as alias of `household`. Worth it only if calibration shows aliasing reduces miss rate — otherwise it's noise.
- **Summary-node generation.** RAPTOR's main contribution is recursive LLM summaries at each tree level. We hand-write them. If the tree grows past ~50 nodes we may want to auto-generate intermediate summary nodes, but that's a separate investigation.
- **Token-budget dynamic trimming.** Currently the cold-start footprint is a hard 425 tokens. If the tree grows, we may need to trim — candidate: include only children with highest query-log hit rate.
- **Phase 2 partial coverage.** 13 of ~25 vault files have frontmatter today. Remaining files are indexed by path+title only. Worth auditing which of the un-described files show up in low-confidence queries.

---

## 7. References

- RAPTOR: Sarthi et al., *RAPTOR: Recursive Abstractive Processing for Tree-Organized Retrieval*, ICLR 2024.
- ConvoMem: *ConvoMem: Long-Term Memory for Conversational Agents*, arXiv:2511.10523, Nov 2025.
- sqlite-vec: asg017/sqlite-vec — flat + vector SQLite extension.
- HNSWlib README — ANN index benchmarks, cross-over point N≈1k.
- Internal: [`evolution-db-split.md`](../decisions/evolution-db-split.md), [`no-db-deletion.md`](../decisions/no-db-deletion.md), [`kb-phase2-graph.md`](../decisions/kb-phase2-graph.md).
