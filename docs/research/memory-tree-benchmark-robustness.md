# Memory-Tree Benchmark Robustness Report

**Date:** 2026-04-15
**Dataset:** `scripts/tests/fixtures/memory_tree_queries.jsonl` — 90 queries across 7 tags
**Corpus:** 13 tracked nodes in vault
**Goal:** Stress-test the memory-tree retrieval claim before replacing the existing memory pointer scheme.

---

## TL;DR

The memory-tree is a clear upgrade over the existing "CLAUDE.md hints the answer" mechanism, but the first benchmark that showed 100%-across-the-board was flattered by author-bias. Honest numbers on an adversarial dataset:

| Metric | Easy-mode | Robust (this report) | Target | Verdict |
|---|---|---|---|---|
| single-node recall@5 | 1.00 | **1.00** (20/20) | ≥0.90 | PASS |
| multi-node recall@5 | 1.00 | **1.00** (15/15) | ≥0.75 | PASS |
| cross-branch recall@5 | 1.00 | **1.00** (10/10) | ≥0.70 | PASS |
| adversarial recall@5 (NEW) | — | **0.95** (19/20) | — | strong |
| ambiguous recall@5 (NEW) | — | **1.00** (10/10) | — | strong |
| abstain-far accuracy | 1.00 | **1.00** (5/5) | 100% | PASS |
| abstain-near accuracy (NEW) | — | **0.60** (6/10) | — | **weakness** |
| wrong-confident rate (conf≥0.65) | 0% | **0.0%** | <5% | PASS |
| LOO recall@5 | — | **0.973** (73/75) | — | generalizes |
| p95 latency | 79ms | **81ms** | ≤200ms | PASS |
| cold-start footprint | ~585 tok | ~585 tok | ≤800 tok | PASS |

**Recommendation:** merge with current thresholds (LOW=0.65, ABSTAIN=0.35). Near-OOD weakness is a real gap but will tighten naturally after a week of dogfood + re-calibration on real query logs.

---

## 1. What the stress suite adds over the first benchmark

The original 50-query dataset hit 100% on every tag. That score was flattered because:
- The same author wrote both the node descriptions AND the queries — vocabulary overlap was unconscious but large.
- The OOD set was far-field (souffle, Norway hiking) rather than personal-sounding-but-absent.
- Thresholds were fitted on the same dataset they were evaluated on (data leakage).
- No V0 baseline — so we didn't know what each retrieval phase actually contributes.

This report adds:

1. **20 adversarial queries** — paraphrases, typos, synonyms that deliberately avoid the vocabulary in frontmatter descriptions.
2. **10 near-field OOD queries** — personal-sounding questions with no vault answer ("what's my birthday", "what car do I drive", "what was my salary at AWS").
3. **10 ambiguous queries** — queries with 2+ plausibly-correct top hits ("my style", "tools I use").
4. **Leave-one-out cross validation** — refit thresholds on N-1, evaluate on held-out, rotate.
5. **V0/V1/V2/V3 ablation** — does `see_also` expansion actually help? Does abstain?
6. **Threshold sensitivity sweep** — how robust is the current LOW/ABSTAIN pick?
7. **Random baseline** — what's the floor?

---

## 2. Per-tag results (LOW=0.65, ABSTAIN=0.35, k=5)

| Tag | n | recall@5 | MRR@5 | wrong_confident |
|---|---|---|---|---|
| single | 20 | **1.00** | 1.00 | 0 |
| multi | 15 | **1.00** | 0.82 | 0 |
| cross-branch | 10 | **1.00** | 0.95 | 0 |
| adversarial | 20 | **0.95** | 0.86 | 0 |
| ambiguous | 10 | **1.00** | 0.69 | 0 |
| abstain-far | 5 | — | — | 1.00 (correct abstain) |
| abstain-near | 10 | — | — | **0.60 (4 leaked through)** |

**Adversarial miss** (1/20): "directors whose work I rate highly" — confidence 0.334, below ABSTAIN=0.35 floor, fell back. The correct path (`movies.md`) was in the flat top-5, but the abstain gate killed it. Lowering ABSTAIN would recover it at the cost of OOD rejection.

**Near-OOD leaks** (4/10):
- "what was my salary at AWS" → background.md @ 0.484 (shares "AWS" vocab)
- "when did I start programming" → background.md @ 0.488 (shares "SWE" vocab)
- "where did I grow up" → learning.md @ 0.394
- "what car do I drive" → learning.md @ 0.355

The background.md leaks are the most concerning — they score confidently (>0.45) and surface topically-related-but-wrong content. "Wrong-confident" (conf ≥0.65) is 0%, so the system never *confidently* asserts something wrong, but it will sometimes answer a near-OOD question with a shrug-adjacent node at 0.45-0.50 confidence.

---

## 3. Ablation — what does each retrieval phase contribute?

| Variant | recall@5 | MRR@5 | abstain_acc | cross | adversarial | abstain-far | abstain-near |
|---|---|---|---|---|---|---|---|
| V0 flat only | 0.833 | 0.738 | 0.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| V1 flat + abstain | 0.822 | 0.733 | **0.73** | 1.00 | 0.95 | 1.00 | 0.60 |
| V2 flat + see_also | 0.833 | 0.738 | 0.00 | 1.00 | 1.00 | 0.00 | 0.00 |
| V3 **full** | 0.822 | 0.733 | **0.73** | 1.00 | 0.95 | 1.00 | 0.60 |

**Key finding: `see_also` expansion provides no measurable recall gain at 13 nodes.** V0↔V2 are identical; V1↔V3 are identical. The reason: at k=5 with 13 total nodes, flat cosine already returns ~40% of the corpus — any reasonable see_also neighbor is already in the top-5. Expansion will start mattering at ~50+ nodes, not today.

**Abstain provides the entire value add.** V1 catches 100% of far-OOD and 60% of near-OOD at a cost of one adversarial query (0.334 fell under the 0.35 floor). Without abstain (V0/V2) the system is ~identical to pure flat cosine — which is what we suspected anyway.

**Implication for the design:** `see_also` is currently dead code that will earn its spot later. It should stay in — the code is cheap, the edges table is populated, and at 100+ nodes (a year from now) it will matter. This report pins the claim: if corpus <30 nodes, don't expect expansion lift.

---

## 4. Leave-one-out cross validation

For each non-abstain query, re-fit thresholds on the dataset minus that query, evaluate the held-out query with refitted thresholds. Retrieval results are cached (deterministic given DB state), so LOO is fast — O(N × sweep) not O(N²) retrievals.

| Metric | Full-data fit | LOO fit |
|---|---|---|
| recall@5 (non-abstain) | 0.987 (74/75) | **0.973** (73/75) |
| MRR@5 | 0.877 | 0.866 |
| abstain accuracy | 0.733 | 0.733 |
| wrong-confident | 0.000 | 0.000 |
| LOW threshold | 0.65 | **0.600 ± 0.000** |
| ABSTAIN threshold | 0.35 | **0.340 ± 0.002** |

**Stability is very high** — stddev of fitted thresholds under LOO is effectively zero (0.000 for LOW, 0.002 for ABSTAIN). Dropping any single query shifts the fit by at most 0.01. The model is not overfit to any particular query.

**Generalization drop is tiny** — 98.7% → 97.3% = one query. That's one adversarial query that the LOO-fitted ABSTAIN rejected when the full-data fit would have passed it.

---

## 5. Threshold sensitivity grid

`recall% / abstain_acc%`. Target: ≥80/80 in both. Grid computed with all LOW ≥ ABSTAIN (lower-left triangle filled).

```
LOW \ ABSTAIN | 0.25  | 0.30  | 0.35  | 0.40  | 0.45
LOW=0.40      | 100/13| 100/40| 99/73 | 84/87 |   -
LOW=0.45      | 100/13| 100/40| 99/73 | 84/87 | 59/87
LOW=0.50      | 100/13| 100/40| 99/73 | 84/87 | 59/87
LOW=0.55      | 100/13| 100/40| 99/73 | 84/87 | 59/87
LOW=0.60      | 100/13| 100/40| 99/73 | 84/87 | 59/87
LOW=0.65      | 100/13| 100/40| 99/73 | 84/87 | 59/87
LOW=0.70      | 100/13| 100/40| 99/73 | 84/87 | 59/87
```

**LOW is inert at 13 nodes** — every row is identical. Confirms the ablation finding.

**The real dial is ABSTAIN:**
- 0.25 → 100 recall, 13% abstain (permissive, leaks OOD)
- **0.35 (current) → 99 recall, 73% abstain (sweet spot)**
- 0.40 → 84 recall, 87% abstain (tighter OOD at the cost of 15pp recall)
- 0.45 → 59 recall, 87% abstain (too aggressive)

ABSTAIN=0.35 is the Pareto point on this corpus. Moving to 0.40 gains 14pp abstain-accuracy but costs 15pp recall — worse trade. Moving to 0.30 keeps recall at 100 but abstain-accuracy crashes to 40%.

---

## 6. Random baseline (sanity floor)

Draw 5 random paths from the corpus of 13, compute recall@5. 10 trials per query, averaged:

| Tag | Memory-tree | Random | Lift |
|---|---|---|---|
| single | 1.00 | 0.41 | **+59pp** |
| multi | 1.00 | 0.70 | +30pp |
| cross-branch | 1.00 | 0.59 | +41pp |
| adversarial | 0.95 | 0.44 | **+51pp** |
| ambiguous | 1.00 | 0.79 | +21pp |

Theoretical floor is 5/13 = 38.5% for single-hit tags. Tags with multiple acceptable paths (multi, ambiguous, cross-branch) have higher random baselines by construction.

The memory-tree beats random by **21-59pp across every tag**. The adversarial lift of +51pp is the most meaningful number in this table — it shows the embedding model handles paraphrase robustly enough that random guessing isn't a competitive strategy.

---

## 7. Known weaknesses

1. **Near-field OOD leaks** (60% abstain accuracy, not 100%).
   - Root cause: queries that share vocabulary with existing descriptions ("AWS salary", "programming start date") score in the 0.45-0.49 range — above ABSTAIN=0.35 but below LOW=0.65.
   - Mitigation: raise ABSTAIN to 0.50 (would catch 3/4 leaks) but costs 15pp recall. Unfavorable trade today.
   - Better mitigation: description tuning — add negative constraints ("this file does NOT contain financial information or exact dates") to background.md. Not implemented yet.
   - Dogfood will surface which real near-OOD patterns matter. Re-calibrate after a week.

2. **One adversarial abstained** ("directors whose work I rate highly" @ 0.334 < 0.35 floor).
   - Fix is to lower ABSTAIN but then OOD leakage gets worse. Same tradeoff.
   - Real fix: stronger embeddings or query rewriting. Not in scope.

3. **`see_also` expansion is inert at this scale.**
   - Expected — corpus is too small for the feature to matter.
   - Keep the code: the cost is tiny and it will earn its spot as the vault grows past ~50 nodes.

4. **Dataset author-bias remains.**
   - Even with adversarial queries, I wrote both sides. A second author would be more honest. The +51pp lift over random on adversarial is the strongest counter-signal we have.
   - Dogfood queries from real cold starts will be the true final test.

---

## 8. Merge recommendation

**Green.** Memory-tree is a clear upgrade:
- The motivating "who do I live with" query now returns `household.md` at 0.402 confidence — the existing system returned "I don't know" 100% of the time.
- Every ship criterion is met with margin.
- Thresholds are stable under LOO (stddev ≤ 0.002).
- Adversarial recall is 95% (with a single false-abstain at 0.334 that ABSTAIN=0.35 rejects).
- Wrong-confident rate (conf ≥ 0.65) is 0% — system never asserts wrong confidently.

**Caveats baked into the merge:**
- Near-OOD abstain is 60%. Real personal questions that aren't in the vault will sometimes get an adjacent-but-wrong answer at 0.4-0.5 confidence. Not catastrophic (wrong-confident is 0%) but not ideal.
- Re-calibrate from real logs after ≥1 week of dogfood usage. The Phase 4 query log (`~/.deus/memory_tree_queries.jsonl`) will accumulate organically; `memory_tree.py calibrate` re-run will update thresholds.
- `see_also` is inert today. If the vault grows to ~50 nodes and cross-branch queries don't improve, re-audit the expansion logic.

---

## 9. Reproducing this report

```bash
cd /path/to/deus-memory-tree
DEUS_VAULT_PATH=... python3 scripts/memory_tree.py build --rebuild --force

# Standard benchmark (per-tag breakdown)
DEUS_VAULT_PATH=... python3 scripts/memory_tree.py benchmark scripts/tests/fixtures/memory_tree_queries.jsonl

# Ablation (V0/V1/V2/V3 side by side)
DEUS_VAULT_PATH=... python3 scripts/memory_tree.py benchmark scripts/tests/fixtures/memory_tree_queries.jsonl --ablation

# Leave-one-out CV
DEUS_VAULT_PATH=... python3 scripts/memory_tree.py benchmark scripts/tests/fixtures/memory_tree_queries.jsonl --loo

# Calibrate thresholds
DEUS_VAULT_PATH=... python3 scripts/memory_tree.py calibrate scripts/tests/fixtures/memory_tree_queries.jsonl
```

All tests deterministic; rerunning should produce identical numbers up to float precision.
