# ADR: Selective pre-warm (only active test files)

**Status:** Accepted
**Date:** 2026-03-29
**Scope:** `eval/`

## Context

The eval suite has three dataset files: `core_qa`, `tool_use`, `safety` (~13 prompts each, ~40 total). Before any test runs, the `warm_agent_cache` fixture pre-spawns containers in parallel so every test hits the in-memory cache instead of waiting serially.

A naive implementation warms all datasets regardless of which test files are being run. Running only `test_core_qa.py` would still spin up ~40 containers — wasteful and slow.

Additionally, API rate limits saturate quickly (~30 containers/session). Unnecessary warmup burns quota and can cause 429 retries that make the full suite slower.

## Decision

Warmup derives the active dataset list from the collected pytest items at session start:

```
test_core_qa.py   → datasets/core_qa.jsonl
test_tool_use.py  → datasets/tool_use.jsonl
test_safety.py    → datasets/safety.jsonl
```

Only datasets that match a collected test file stem are warmed. Running a single file warms only its ~13 prompts instead of all 40.

Concurrency is `cpu_count // 2`, capped at 8, overridable via `DEUS_EVAL_CONCURRENT`. This balances container RAM pressure (~300–500MB each) against API rate limits.

## Consequences

- Running a single test file starts ~3× faster (13 containers vs 40).
- Full suite behavior is unchanged when all three files are collected.
- Adding a new dataset requires a matching `test_{name}.py` filename for auto-discovery; otherwise it must be added to `_ALL_DATASETS` manually.
