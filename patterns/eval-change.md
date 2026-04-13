---
governs:
  - evolution/
  - eval/
  - scripts/memory_indexer.py
last_verified: "2026-04-18"
test_tasks:
  - "Add a new DeepEval metric under eval/ for the core_qa test suite"
  - "Add a new judge backend to evolution/judge/ using the provider registry"
  - "Add a new evolution storage provider under `evolution/storage/providers/`"
  - "Add a new atom type to the eval/evolution memory indexer at `scripts/memory_indexer.py`"
---
# Pattern: eval-change

## ADR gate (mandatory)

Before any change to `evolution/`, read `docs/decisions/INDEX.md`. Three decisions have permanent constraints:

| ADR | Ruling | Why permanent |
|-----|--------|---------------|
| `eval-ipc-file-output.md` | Results via shared-volume files, **not stdout** — do not revert | Docker pipe buffering is a runtime constraint, not a fixable bug. Deadlock is guaranteed under load. |
| `eval-no-disk-cache.md` | In-memory cache only | Disk cache silently masks regressions across builds — a passing cached result hides a regression in the new build. |
| `eval-selective-warmup.md` | Warm only active test datasets | Full suite = ~40 container starts; cold start ~10 min. Warming inactive sets wastes time and saturates API rate limits. |

## Database isolation

**Two separate databases.** Never share files or join across them:

| Database | Owner | Safe to delete? | Env override |
|----------|-------|-----------------|--------------|
| `~/.deus/memory.db` | `scripts/memory_indexer.py` | Yes — derived from on-disk files | `DEUS_DB` |
| `~/.deus/evolution.db` | `evolution/` | No — scored interactions, reflections | `DEUS_EVOLUTION_DB` |

**Tests that monkeypatch the database path** must use `EVOLUTION_DB_PATH`, not the old `DB_PATH`. Using `DB_PATH` silently tests against the wrong database file.

## Concurrency limits

Concurrency is `cpu_count // 2`, capped at 8. Override with `DEUS_EVAL_CONCURRENT`. **Never raise this cap** — rate limits saturate fast (~30 containers/session).

## Adding a new dataset

New dataset test files must be named `test_{name}.py` for auto-discovery. If the naming convention isn't followed, add the dataset manually to `_ALL_DATASETS`. Warm only the datasets used by the active test suite.

## Storage migrations

Use the existing `try/except ALTER TABLE` pattern in `evolution/storage/providers/sqlite.py`. Never add columns in the `CREATE TABLE` block — it only runs once.

```python
for col, coltype in [("new_col", "TEXT")]:
    try:
        db.execute(f"ALTER TABLE interactions ADD COLUMN {col} {coltype}")
    except sqlite3.OperationalError:
        pass  # Column already exists
```

## Provider pattern

Adding a new backend = one file + one registration line. Abstract contract in `evolution/storage/provider.py`. Concrete impl in `evolution/storage/providers/`.

## Config file locations

The evolution layer reads from the **project root `.env`** — not from `~/.config/deus/.env`. See `patterns/deployment.md` §Config file locations for the full table.

## Tests

Python tests in `scripts/tests/`. Run `python3 -m pytest scripts/tests/` before committing.
