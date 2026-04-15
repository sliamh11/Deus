# Memory-Tree Verifier Plan — Closing the Near-OOD Gap

**Date:** 2026-04-15
**Status:** Proposed. Not started.
**Scope:** `scripts/memory_tree.py` — second-stage verifier on top of flat retrieval.
**Relation:** Follow-up to PR #173. Assumes memory-tree Phases 1–5 + hardening merged.

---

## 1. Exact gaps mapped

From the robustness report (`memory-tree-benchmark-robustness.md`), four measurable gaps in ranked priority:

| # | Gap | Impact | Severity |
|---|---|---|---|
| 1 | **abstain-near accuracy = 60%** — 4/10 near-OOD queries leak through as confident-adjacent hits | User asks "what was my salary at AWS" → gets `background.md`. Not catastrophic (conf 0.45–0.49, not high-confident), but wrong. | **high** |
| 2 | **One adversarial abstained** — "directors whose work I rate highly" @ 0.334 killed by ABSTAIN=0.35 | Correct path was in flat top-5; abstain gate rejected it. | medium |
| 3 | **`see_also` expansion is inert** at 13 nodes (V0↔V2 and V1↔V3 identical) | Dead code today; will matter at ~50+ nodes. | low (deferred) |
| 4 | **LOW threshold is functionally inert** at this scale (sensitivity grid rows 0.40–0.70 identical) | No behavior change from tuning LOW. | low (deferred) |

Gaps 3 and 4 are scale-bound — they resolve themselves as the corpus grows. **Gaps 1 and 2 are the same underlying problem:** the retrieval layer ranks by vector similarity but has no signal for whether the top document *actually answers* the query.

---

## 2. Root cause — why the gaps exist

### 2.1 Cosine similarity is bag-of-topics, not question-answering

`embeddinggemma` encodes each document independently. A query about "salary at AWS" and `background.md` (which mentions AWS employment) share enough vocabulary that cosine lands in the 0.45–0.49 uncertainty band. The model has no mechanism to distinguish:

- **Topic adjacency** ("this document is *about* AWS" → high cosine)
- **Answer presence** ("this document *contains the salary* of the AWS job" → what we actually want)

This is a well-known limitation of bi-encoder retrieval. It's the reason production search stacks almost always have a re-ranking stage.

### 2.2 Single-score abstain is a blunt instrument

A hard threshold (`if confidence < ABSTAIN: refuse`) reduces the decision to one number. But:

- A **0.334** near-correct result ("directors" vs movies.md) is killed
- A **0.484** topically-adjacent wrong result ("salary" vs background.md) is accepted

Same threshold, opposite-direction errors. No single number can fix this — the two failure modes are orthogonal.

### 2.3 No corpus-scale mechanism

At 13 nodes, every reasonable candidate ends up in top-5 via flat. The retrieval pipeline has no step that would start mattering at 100+ nodes — gaps 3 and 4 quantify this.

---

## 3. Proposed solution — two-stage retrieval with a cheap local verifier

### 3.1 Architecture

```
query
 │
 ├─→ Stage 1 (current): flat cosine → top-k candidates + scores
 │
 └─→ Stage 2 (NEW): local verifier checks each candidate
       ├─ "Does this doc answer this question? yes/partial/no"
       ├─ Cheap: Ollama gemma4:e2b (~2B params, runs locally)
       ├─ Batched: all k candidates in one prompt
       └─ Returns: per-candidate relevance label + confidence
       │
       ▼
    Demote candidates labelled "no"; promote "yes"; keep "partial" at current rank.
    Final abstain: only if all k are "no" OR best cosine < hard floor.
```

### 3.2 Why this is the right answer (not just a fix)

**Generic** — the verifier is query+doc in, relevance out. Works for any corpus size, any query type, any new node types added later.

**Scalable** — Stage 2 is O(k), **independent of corpus size N**. Stage 1 at 10k nodes might take 50ms; Stage 2 always costs ~300ms for k=5. Flat scales fine to ~10k; verifier unchanged.

**Efficient** — zero API cost (Ollama is local). Single batched prompt per query. No fine-tuning. No new databases. Single-file change.

**Addresses both primary gaps simultaneously:**
- Near-OOD: verifier catches that `background.md` doesn't actually contain salary data, demotes it → abstain fires correctly.
- Borderline adversarial: verifier confirms movies.md *is* about directors despite 0.334 cosine → promotes above floor.

**Preserves existing architecture** — Stage 1 is untouched. Stage 2 is additive and gated by a flag (`--verify` / `DEUS_TREE_VERIFY=1`). Ship as opt-in, dogfood, then default on after metrics confirm.

### 3.3 Alternatives considered (and rejected)

| Approach | Why rejected |
|---|---|
| **Raise ABSTAIN to 0.50** | Costs 15pp recall per sensitivity grid — trades one error for another of equal size. |
| **Cross-encoder re-ranker** | Needs model download + inference; heavier than gemma4:e2b; marginal gain over a small verifier. |
| **HyDE (hypothetical document embeddings)** | Still bi-encoder; doesn't fix the "is the answer actually present" gap. |
| **Fine-tune embeddinggemma** | Training data collection + embedding rebuild across vault; high maintenance cost; brittle to corpus changes. |
| **Negative constraints in frontmatter** | Manual ("this file does NOT contain financial data") — doesn't scale; maintenance burden grows with vault size. |
| **Larger embedding model** | Doesn't fix the fundamental bi-encoder limitation; higher latency; higher disk use. |

### 3.4 Cost model

- **Latency:** +100–400ms per query (gemma4:e2b, 5 batched candidates, ~200 tokens total).
- **Memory:** gemma4:e2b needs ~2GB RAM, shared with the existing Ollama daemon. No increase over Phase 6 plan.
- **Complexity:** ~200 LOC in `memory_tree.py`; one new test file with ~10 verifier tests.
- **Config:** 2 env vars (`DEUS_TREE_VERIFY=1`, `DEUS_TREE_VERIFIER_MODEL=gemma4:e2b`).

### 3.5 Predicted metric impact (hypothesis)

Based on the 4 specific near-OOD leaks + 1 adversarial false-abstain, optimistic projection:

| Metric | Current | Predicted (verifier on) | Delta |
|---|---|---|---|
| abstain-near accuracy | 0.60 | **0.90+** | +30pp |
| adversarial recall@5 | 0.95 | **1.00** | +5pp |
| recall@5 overall | 0.82 | **0.90+** | +8pp |
| p95 latency | 81ms | ~350ms | +270ms |
| wrong-confident rate | 0.0% | 0.0% | stable |

Latency goes from "barely noticeable" to "perceptible but fine for cold-start questions". Worth it if accuracy lift holds.

**Must validate empirically before shipping** — these numbers are extrapolation from the specific 4 failing queries, not a real benchmark run.

---

## 4. Implementation plan

### Phase 8.1 — Verifier module (isolated, testable)
- Add `scripts/memory_tree_verifier.py` with `verify_candidates(query, candidates, model) → list[{path, label, confidence}]`.
- Prompt template with 3-class output (yes/partial/no).
- Unit tests with stub model responses; no Ollama dependency in test path.
- Separate file so the verifier can be swapped for a different local model without touching retrieve().

### Phase 8.2 — Wire into retrieve()
- Add `use_verifier: bool = False` flag to `retrieve()`.
- If on and Ollama reachable: call verifier after stage 1, re-rank by (label, cosine).
- If Ollama unreachable: silently skip, log once, continue with stage-1 results. Fail open, not closed.

### Phase 8.3 — CLI + env gates
- `--verify` flag on `memory_tree.py query` and `benchmark`.
- `DEUS_TREE_VERIFY=1` env var.
- `DEUS_TREE_VERIFIER_MODEL` env var (default `gemma4:e2b`).

### Phase 8.4 — Benchmark the verifier on the 90-query dataset
- Run `benchmark --verify` vs `benchmark` (no verify).
- Report: abstain-near delta, adversarial delta, recall delta, latency delta.
- Ship criteria for verifier: abstain-near ≥0.85, adversarial ≥1.00, no regression on single/multi/cross-branch, p95 ≤500ms.

### Phase 8.5 — Default-on decision gate
- If Phase 8.4 metrics meet criteria → flip `use_verifier=True` as default.
- If regressions in any tag → stay opt-in, investigate.

### Phase 8.6 — Observability
- Extend `queries_log.jsonl` with verifier labels when used.
- Weekly `report --verifier-disagreements` — queries where cosine ranked high but verifier said "no".
- These are the high-signal queries for description tuning.

---

## 5. Rejected / deferred extensions

- **Query rewriting / expansion** — premature. Verifier fixes the common case; rewriting adds complexity for a marginal additional tag of queries.
- **Fine-tuning the embedding model** — requires training pipeline + curated pairs. Not aligned with "use what's off-the-shelf"; the verifier achieves the same goal via a different layer.
- **Multi-hop retrieval** — only relevant at larger corpus. Revisit when Phase 3 metrics degrade on future re-calibration.

---

## 6. Success criteria for Phase 8

The verifier earns its place if — measured on the existing 90-query dataset:

1. `abstain-near accuracy ≥ 0.85` (currently 0.60)
2. `adversarial recall@5 ≥ 1.00` (currently 0.95)
3. No regression on single / multi / cross-branch / ambiguous
4. `p95 latency ≤ 500ms` (currently 81ms)
5. `wrong-confident rate = 0.0%` (stable)

If any criterion fails, the verifier stays opt-in and we investigate.
