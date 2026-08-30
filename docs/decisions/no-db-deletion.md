# No Database Row Deletion

**Status:** Accepted
**Date:** 2026-04-13
**Amended:** 2026-07-15 — Rule 6 clarified to permit per-row deletes from derived
index tables outside `--rebuild` (motivated by LIA-368/LIA-370).
**Amended:** 2026-08-26 — Rule 9 added: bounded-retention telemetry tables may
hard-delete AFTER off-DB archival, scoped to `queries_log` only (LIA-128).
**Scope:** All database operations across the entire codebase

## Context

During the KB Phase 2–4 implementation, `--rebuild` silently destroyed all runtime data (access_log, query_log history) by deleting the entire database file. A subsequent fix changed this to `DELETE FROM` on rebuildable tables, but this still permanently removes rows. Separately, `cmd_prune()` hard-deletes orphaned entries, and `delete_entries()` hard-deletes entries during re-indexing — both lose audit trail.

Root cause: the codebase treated "repopulate" as "delete then recreate" when it should mean "mark stale then re-verify."

## Decisions

1. **Never DELETE or DROP rows from any database table.** Use status flags (`orphaned_at`, `expired_at`) to mark rows as inactive. All queries must filter by these flags. **Do not change this.**

2. **Soft-delete columns:**
   - `orphaned_at TEXT DEFAULT NULL` — set when the source file is removed, re-indexed, or entry is superseded during rebuild. Indicates the row is no longer current but preserved for audit.
   - `expired_at TEXT DEFAULT NULL` — set when an atom is invalidated by contradiction or TTL. Already exists.
   - Both columns use ISO-8601 date strings. NULL = active row.

3. **Rebuild = mark stale + re-verify.** `--rebuild` marks all rebuildable entries as `orphaned_at = now` with `orphan_reason = 'rebuild'`, then re-indexes from disk. New entries get fresh IDs. Old orphaned entries remain for audit trail.

4. **Re-indexing (`cmd_add`) = soft-delete old + insert new.** When a file is re-indexed, old entries for that path are marked `orphaned_at = now` before new entries are inserted.

5. **Orphan cleanup (`cmd_prune`) = soft-delete.** When an atom file is deleted from disk, the DB row is marked `orphaned_at = now`, not deleted.

6. **Derived tables may use per-row `DELETE FROM` — including outside `--rebuild`.** Tables that are fully derived from primary data (entities, relationships, atom_entities, embeddings, `entries_fts`, `chunks_vec`, `chunks_fts`) may use `DELETE FROM` — both the bulk clear during `--rebuild` AND targeted per-row deletes during incremental reindex / soft-delete — because:
   - They contain no primary user data
   - They are fully rebuildable from atoms/entries/chunks
   - Adding soft-delete to virtual tables (vec0, fts5) is not supported — they have no arbitrary columns, so a stale derived row can only be removed by a hard `DELETE`
   - The source row (with its `orphaned_at` soft-delete) provides the audit trail

   Amended 2026-07-15 (LIA-368/LIA-370): the previous wording exempted derived-table deletes "during rebuild only" and said individual rows "should still be preserved where possible" outside rebuild. That left stale rowids in the `vec0`/`fts5` indexes forever (measured: ~69% dead rows in code-search, and orphaned entries surfacing as live memory results), because a per-file reindex / soft-delete has no `--rebuild` to piggyback on. Per-row derived-index deletes keyed on the primary row's id/rowid are now explicitly permitted at any time. **This carve-out applies ONLY to derived index tables** — primary tables (`entries`, `chunks`, `atoms`, `nodes`) remain strictly soft-delete (Rule 1), never hard-deleted.

7. **Backup before rebuild is mandatory.** A timestamped `.bak` copy of the database is always created before any rebuild operation. This is a safety net, not a replacement for soft-delete.

8. **Vault files are separate.** This ADR applies strictly to database operations. Vault files (atoms, session logs, memory files) are git-versioned and follow different rules — file operations like archive-and-delete (memory_gc) or in-place update (atom frontmatter) are acceptable because git provides the audit trail.

9. **Bounded-retention telemetry tables may hard-delete rows AFTER archiving them off-DB.** `queries_log` (`~/.deus/memory_tree.db`) is telemetry, not memory content, and has no production reader — `git grep -na "FROM queries_log"` returns only two test assertions (`scripts/tests/test_memory_tree_phase3.py`, `scripts/tests/test_memory_tree.py`), neither of which depends on history; `calibrate()`/`calibrate_sweep()` take a caller-supplied labeled dataset and call `retrieve()` live. Its durable record is the JSONL twin written by the same `_log_query()` call, which `scripts/maintenance/rotate_query_log.py` already rotates into 365-day gzip archives — that script's own docstring calls the SQLite table a "secondary copy". A retention prune may therefore hard-delete rows, but **only after** every doomed row has been written to a gzip archive AND that archive has been re-read and verified, with a failed or short archive aborting the run before any `DELETE` executes. This is the same audit-trail substitution Rule 8 already accepts for vault files, where git rather than a soft-delete column carries the history.

   Soft-delete cannot satisfy this case. The entire purpose is reclaiming file bytes, and a soft-deleted row still occupies its pages — Rule 1's mechanism is structurally unable to fix the problem it would be applied to. Measured 2026-08-26: `queries_log` held 914,159 rows / 164 MB of a 172 MB database whose actual memory content is 378 nodes.

   **Scope: `queries_log` only.** Extending this to any other table requires a further amendment. Primary content tables (`entries`, `chunks`, `atoms`, `nodes`) remain strictly soft-delete under Rule 1.

## Consequences

- Database size grows over time with orphaned rows. This is acceptable — SQLite handles millions of rows efficiently, and storage is cheap compared to data loss.
- All SELECT queries on `entries` must include `AND orphaned_at IS NULL` (or explicitly opt out for audit/admin queries).
- Future: add periodic `--purge-orphans --older-than 90d` command for optional cleanup of very old orphaned entries, gated behind `--confirm`.
- Bounded telemetry tables under Rule 9 no longer grow without limit. Their history moves to gzip archives under `~/.deus/archive/` and stays recoverable there for `--archive-keep-days` (default 365), so "not in the DB" no longer means "gone".
