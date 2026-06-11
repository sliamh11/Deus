# ADR: Threshold Calibration Sweep

**Status:** Accepted  
**Date:** 2026-05-09  
**Scope:** scripts/memory_tree.py, deus-cmd.sh  
**Supersedes:** None  
**Related:** benchmark-regression-gate.md, embedding-model-selection.md

## Context

The memory retrieval system uses four thresholds to decide when to abstain:
`abstain_threshold`, `gap_threshold`, `coverage_threshold`, `content_cap`.
These were originally hand-tuned via manual grid search. Every time the
retrieval pipeline changes (embedding model swap, approach angle regeneration,
new benchmark queries), the optimal thresholds shift and must be recalibrated.

## Decision

Ship `calibrate-sweep` as a CLI subcommand (`deus sweep`) that automatically
grid-searches threshold combinations against the benchmark dataset. The sweep
uses **Pareto selection** (not a blended metric like F1) per
`benchmark-regression-gate.md` §3: recall and abstain accuracy are kept
separate. The sweep finds all combos where `recall >= min_recall` (default
0.70), then ranks by `abstain_accuracy`.

### When to run

Run `deus sweep` after any change to the retrieval pipeline:
- Switching or upgrading the embedding model
- Re-backfilling approach angles (new prompt, new model)
- Adding benchmark queries to the fixture
- Modifying the abstain gate logic
- Adding new retrieval phases (e.g., atom fallback)

### Implementation

- Function: `calibrate_sweep()` in `scripts/memory_tree.py`
- Pre-caches query embeddings before sweeping (~5s upfront) to avoid
  redundant Ollama calls across the grid. The cache monkeypatch targets
  `sys.modules[__name__]` — under CLI invocation the executing module is
  `__main__`, and patching via `import memory_tree` would hit a second
  module copy, silently disabling the cache (observed: a 92s sweep ran
  ~7h making one live embed call per query per combo)
- CLI: `python3 scripts/memory_tree.py calibrate-sweep <dataset.jsonl> --json`
- Wrapper: `deus sweep [optional-dataset-path]`
- Output: best thresholds, top-5 Pareto frontier, current defaults for
  comparison

### Constraints

- Never blend recall and abstain_accuracy into one score (per
  benchmark-regression-gate.md §3)
- Grid size bounded: the grid has grown with the pipeline — a 5th
  dimension (`min_entity_overlap`, added with the coherence gate) brought
  it to 6×4×5×4×3 = 1,440 combos. Acceptable because per-combo cost with
  an effective embed cache is ~0.1s
- Pre-cached embeddings keep wall-clock ~90s for 1,440 combos
  (measured 2026-06-10, 74-query dataset)
- Thresholds are env-var tunable — the sweep recommends, the operator decides
