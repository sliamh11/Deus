# ADR: Benchmark regression gate for memory tree retrieval

**Date:** 2026-04-25
**Status:** Accepted — shipped in PR #248.
**Scope:** `scripts/drift_check.py` (`--bench-labels`, `--bench-snapshot`), `scripts/tests/fixtures/memory_tree_queries.jsonl`, `scripts/tests/fixtures/memory_tree_snapshot.json`.

## Context

Between 2026-04-18 and 2026-04-25, memory tree retrieval recall dropped from 94.4% to 71.1% without anyone noticing. The regression had three compounding causes:

1. **Code bug** (PR #245): Score-gap abstain guard used `best < low_threshold` (0.55). For Ollama embeddinggemma scores (range 0.30-0.50), this was always true — 6 queries falsely abstained. One-line fix: replace with `best < abstain_threshold + gap_threshold`.

2. **Stale benchmark labels**: The vault was restructured (Apr 18-21) — CLAUDE.md content moved to INFRA.md/STATE.md, Persona/INDEX.md became a routing node pointing to leaf files. The benchmark still expected the old paths. Retrieval was correct; the benchmark was wrong. 7 labels updated.

3. **Auto-memory dilution**: `reindex-external` (PR #244) added 119 auto-memory nodes. Legitimate competition, but shifted the ranking baseline the benchmark was tuned for.

The code bug could have been caught at PR time. The stale labels could have been caught at vault-restructure time. Neither was, because no automation validated the benchmark against live state.

## Decisions

### 1. CI label validation (`--bench-labels`) in `--all`

Every expected_path in the benchmark JSONL is checked against vault and auto-memory files on disk. Runs in CI as part of `drift_check.py --all`. No Ollama or embeddings needed -- pure filesystem check.

**Why:** The dominant failure mode (7 of 12 misses) was stale labels. This is the cheapest possible check that catches the most common drift. Wired into `--all` so every PR gets it.

### 2. Local snapshot regression gate (`--bench-snapshot`)

Runs the full 90-query benchmark against a stored recall threshold (default 90%). Requires Ollama + DB, so it's local-only (not CI). The snapshot file (`memory_tree_snapshot.json`) is checked in so the threshold travels with the code.

**Why:** The code bug (score-gap abstain) was invisible to static checks. Only running the actual benchmark with real embeddings catches retrieval regressions. At 136 nodes the benchmark completes in ~30 seconds.

### 3. Separate retrieval vs abstain metrics

Retrieval recall is measured over the 75 retrieval-expected queries only. Abstain accuracy is a separate metric over 15 OOD queries. The blended "overall" number conflates two independent problems and masked the regression severity.

**Why:** The original framing ("94.4% to 71.1%") mixed retrieval misses and abstain failures into one number, making the regression look like a single cause when it was three independent causes with different fixes.

### 4. Score-gap guard uses `abstain_threshold + gap_threshold`, not `low_threshold`

The gap-abstain check is a second-order filter: it catches queries where the top score is weak AND indistinguishable from noise. The ceiling must be near the abstain floor (where scores genuinely can't be trusted), not the low-confidence threshold (which is above most Ollama scores).

**Why:** `low_threshold` (0.55) is above the typical in-domain score range for Ollama embeddinggemma (0.30-0.50). `abstain_threshold + gap_threshold` (0.34 for Ollama, 0.56 for Gemini) stays near the noise floor regardless of provider. **Do not revert to `low_threshold`.**

## Disproved approaches

During the investigation, six retrieval improvement levers were smoke-tested and all failed on this corpus:

| Approach | Result | Why it failed |
|----------|--------|---------------|
| Task prefixes (embeddinggemma) | Rankings degraded in 3/3 queries | Prefixed queries live in different vector subspace than non-prefixed documents |
| Body-text enrichment for vault nodes | Scores dropped in 4/5 queries | Longer input dilutes the description signal for embeddinggemma |
| nomic-embed-text model swap | 69.3% vs 78.9% embeddinggemma | Worse on this specific short-description corpus |
| bge-m3 model swap | Broke 1 hit, recovered 0 misses | Lexical matching on short descriptions |
| Cross-encoder reranking (MiniLM-L6-v2) | Recovered 1/3, collapsed 2/3 to noise | MS-MARCO training doesn't transfer to 1-2 line descriptions |
| Per-namespace z-score normalization | Worsened 2/3 misses | Auto-memory nodes are genuinely more relevant, not unfairly inflated |

**Do not re-attempt these without a concrete hypothesis for why the prior result would change.** The remaining 5 misses at 96% recall are dilution cases where auto-memory nodes are legitimately more semantically relevant than the expected vault structural nodes.
