# ADR: Split evolution database from memory indexer

**Date:** 2026-04-08
**Status:** Accepted
**Scope:** `evolution/`, `scripts/memory_indexer.py`

## Context

The memory indexer (`scripts/memory_indexer.py`) and the evolution loop (`evolution/`) both stored data in `~/.deus/memory.db`. The memory indexer's `--rebuild` command deletes the entire database file and recreates it from vault session logs — this is correct for the indexer's data (derived from on-disk markdown files, cheap to regenerate).

However, this also destroyed evolution data: scored interactions, reflections, and prompt artifacts. This data is expensive to regenerate (hours of local Ollama GPU time for batch scoring, plus CC session ingestion parsing).

On 2026-04-07, a `--rebuild` run in a concurrent session wiped ~821 scored interactions and all associated reflection data.

## Decision

**Split into two database files:**
- `~/.deus/memory.db` — memory indexer only (sessions, atoms, embeddings). Owned by `scripts/memory_indexer.py`. Safe to delete and rebuild.
- `~/.deus/evolution.db` — evolution loop only (interactions, reflections, prompt artifacts, principle extractions). Owned by `evolution/`. Never deleted by automated processes.

Override paths via `DEUS_DB` (memory) and `DEUS_EVOLUTION_DB` (evolution).

On first run, if `evolution.db` doesn't exist but `memory.db` contains evolution tables with data, the storage provider automatically migrates the data.

## Alternatives considered

**Selective rebuild (drop only indexer tables):** Would keep one file but couples the indexer to knowledge of evolution table names. Any future subsystem adding tables to the shared file faces the same risk. Fragile.

**Separate schema, same file, no `unlink()`:** Replace `unlink()` with table-level `DROP TABLE` + `CREATE TABLE`. Still shares the file — concurrent access, WAL contention, and accidental cross-subsystem operations remain possible.

## Consequences

- Memory indexer's `--rebuild` is safe — it only affects its own file
- Evolution data survives indexer rebuilds
- No cross-database joins (none existed before)
- Tests that monkeypatch `DB_PATH` for evolution tests must now use `EVOLUTION_DB_PATH`
