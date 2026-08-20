# Memory-Tree Baseline — 2026-08-20 (post migration-canary closeout)

**Date:** 2026-08-20
**Status:** Recorded baseline / regression oracle for later phases.
**Context:** Closes a follow-up step from a personal (untracked, host-local) rule about a
2026-07-28 vault migration that split a large combined memory index into per-topic pointer files
under `Migrated/*.md`. That rule's mandate, restated here so this doc is verifiable without the
untracked source: "add representative canary queries to
`scripts/tests/fixtures/memory_tree_queries.jsonl` covering the migrated pointer layer, so a
future regression (retrieval code change, threshold recalibration, or a careless future migration)
gets caught by the existing benchmark instead of silently recurring." This was never done: the
fixture sat at 74 queries, last modified 2026-06-20 (before the migration), with zero canaries
targeting `Migrated/*.md`. See also the repo-tracked `docs/decisions/benchmark-regression-gate.md`
for the broader benchmark-as-regression-gate rationale this builds on.

## What changed

Added 8 canary queries to `scripts/tests/fixtures/memory_tree_queries.jsonl` (74 → 82 queries,
tag `single`), each targeting a `Migrated/*.md` pointer file created by the 2026-07-28 migration.
Regenerated `scripts/tests/fixtures/memory_tree_snapshot.json` to match the new query count and
corpus.

**Privacy constraint on canary selection:** the `Migrated/` directory holds 56 pointer files
across 3 destination projects (client projects A, B, C — internal counts 36 / 18 / 2
respectively), and most filenames bake the client/project codename directly into the slug. Since
this fixture is tracked, CI-validated (`drift_check.py --bench-labels`), and lives in a repo whose
upstream is public, the 8 canaries were deliberately chosen from the subset of `Migrated/*.md`
files whose filename and content do NOT encode a client/project identifier — generic
engineering-gotcha slugs (a Terraform/GitHub-var wiring gap, a GitHub platform incident,
form-semantics, CSS-animation/grid interaction, PR-collision recheck timing, a
destructive-git-in-subagent incident, an Asana API bug, and a DNS-registrar limitation — this last
file's slug lacks the `-YYYY-MM-DD` suffix the other 7 new canaries share; that's just how the
vault filename was originally created, not a typo introduced here). Query text was written to
describe only the technical gotcha, with no client/project name anywhere in query text or path.

**Known gap:** the smallest destination project (client project C, 2 files) has only 2
`Migrated/*.md` files, both with the project name baked into the filename — no privacy-safe
canary exists for it without renaming vault files, which is out of scope for this change (vault
files are not touched by repo-scoped work). 7 of the 8 canaries happen to trace to files
originally filed under one destination project's internal tag, 1 to another — those internal tags
are vault metadata, not exposed anywhere in the tracked fixture.

**Explicitly out of scope:** project-memory topic files under
`~/.claude/projects/*/memory/*.md` (232 files across all projects) are NOT covered by any canary.
Confirmed via direct query against `~/.deus/memory_tree.db`: 0 of 232 have a memory-tree node.
Indexing that layer is a separate, later phase — adding a canary that targets an unindexed path
would fail both `--bench-labels` and retrieval immediately.

## Exact commands run

```bash
# Node count at time of measurement
python3 -c "
import os, sqlite3
db = sqlite3.connect(os.path.expanduser('~/.deus/memory_tree.db'))
print('total', db.execute('select count(*) from nodes').fetchone()[0])
print('migrated', db.execute(\"select count(*) from nodes where path like 'Migrated/%'\").fetchone()[0])
"
# -> total 83, migrated 56

# Label validation (runs in CI)
python3 scripts/drift_check.py --bench-labels
# -> Benchmark labels OK: 82 queries, all expected paths exist in vault.

# Full benchmark
python3 scripts/memory_tree.py benchmark scripts/tests/fixtures/memory_tree_queries.jsonl

# Snapshot self-verification (non-abstain-only recall, matches check_bench_snapshot())
python3 scripts/drift_check.py --bench-snapshot
# -> Benchmark PASS: retrieval recall = 98.3% (59/60), threshold = 90.0%
```

## Full benchmark output (`memory_tree.py benchmark`)

Corpus: 82 queries (60 non-abstain retrieval, 22 abstain). Node count: 83 total / 56 under
`Migrated/`. Embedding provider: ollama / embeddinggemma.

```json
{
  "n": 82,
  "recall_at_k": 0.72,
  "mrr_at_k": 0.554,
  "abstain_accuracy": 0.636,
  "wrong_confident_rate": 0.0,
  "latency_p50_ms": 95.3,
  "latency_p95_ms": 108.4,
  "by_tag": {
    "single": { "n": 14, "recall_at_k": 1.0, "mrr_at_k": 1.0, "wrong_confident": 0 },
    "multi": { "n": 10, "recall_at_k": 1.0, "mrr_at_k": 0.703, "wrong_confident": 0 },
    "cross-branch": { "n": 5, "recall_at_k": 1.0, "mrr_at_k": 0.9, "wrong_confident": 0 },
    "abstain-far": { "n": 5, "abstain_accuracy": 1.0 },
    "adversarial": { "n": 9, "recall_at_k": 1.0, "mrr_at_k": 0.713, "wrong_confident": 0 },
    "abstain-near": { "n": 10, "abstain_accuracy": 0.5 },
    "ambiguous": { "n": 7, "recall_at_k": 1.0, "mrr_at_k": 0.529, "wrong_confident": 0 },
    "vocab-mismatch": { "n": 14, "recall_at_k": 0.929, "mrr_at_k": 0.627, "wrong_confident": 0 },
    "abstain-vocab": { "n": 7, "abstain_accuracy": 0.571 },
    "adversarial-2": { "n": 1, "recall_at_k": 1.0, "mrr_at_k": 1.0, "wrong_confident": 0 }
  },
  "config": {
    "k": 5,
    "low_threshold": 0.55,
    "abstain_threshold": 0.31,
    "gap_threshold": 0.06,
    "rrf_k": 60,
    "use_see_also": true,
    "use_abstain": true,
    "use_fts": true,
    "use_coherence_gate": true,
    "min_entity_overlap": 1
  }
}
```

`single` recall went from 6/6 (pre-change) to 14/14 (post-change) — all 8 new `Migrated/*.md`
canaries hit their target as top-5 result on first try, with no negative effect on any other tag.

## Metric note (why the snapshot's number differs from `recall_at_k` above)

`docs/decisions/benchmark-regression-gate.md` (decision #3) explicitly rejects using the blended
`recall_at_k` figure (0.72 above) as a regression gate, because it conflates retrieval failures
with abstain-tagged items scored as misses. `scripts/drift_check.py --bench-snapshot` instead
computes `retrieval_recall` over non-abstain items only (60 of 82) via a direct `retrieve()` +
top-5 hit check. That number is **0.983 (59/60)**, recorded in
`scripts/tests/fixtures/memory_tree_snapshot.json` as the regression threshold going forward
(`min_retrieval_recall: 0.90`).

## Regression oracle for later phases

This file is the reference point for the next phase (indexing the 232 project-memory topic
files under `~/.claude/projects/*/memory/*.md`). Before and after that work, re-run the exact
commands above and diff against these numbers — particularly `single`/`retrieval_recall`, which
should not regress, and node counts, which should grow by up to 232 once that indexing lands.
