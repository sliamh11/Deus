# No Database Row Deletion

**Status:** Accepted
**Date:** 2026-04-13
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

6. **Derived tables are exempt during rebuild only.** Tables that are fully derived from primary data (entities, relationships, atom_entities, embeddings, entries_fts) may use `DELETE FROM` during `--rebuild` because:
   - They contain no primary user data
   - They are fully rebuildable from atoms/entries
   - Adding soft-delete to virtual tables (vec0, fts5) is not supported
   - The source entries (with soft-delete) provide the audit trail

   Outside of rebuild, individual derived rows should still be preserved where possible.

7. **Backup before rebuild is mandatory.** A timestamped `.bak` copy of the database is always created before any rebuild operation. This is a safety net, not a replacement for soft-delete.

8. **Vault files are separate.** This ADR applies strictly to database operations. Vault files (atoms, session logs, memory files) are git-versioned and follow different rules — file operations like archive-and-delete (memory_gc) or in-place update (atom frontmatter) are acceptable because git provides the audit trail.

## Consequences

- Database size grows over time with orphaned rows. This is acceptable — SQLite handles millions of rows efficiently, and storage is cheap compared to data loss.
- All SELECT queries on `entries` must include `AND orphaned_at IS NULL` (or explicitly opt out for audit/admin queries).
- Future: add periodic `--purge-orphans --older-than 90d` command for optional cleanup of very old orphaned entries, gated behind `--confirm`.
