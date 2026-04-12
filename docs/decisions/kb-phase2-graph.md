# KB Phase 2: Entity/Relationship Graph

**Date:** 2026-04-12

## Status: Accepted

## Context
Phase 1 added temporal invalidation and domain tagging to atoms. Phase 2 lifts atoms into a lightweight entity graph for multi-hop reasoning and contradiction detection.

## Decisions

1. **Entities are text-keyed (name + entity_type), not embedding-keyed.** Embeddings are expensive and entities need stable identifiers for the junction table. UNIQUE(name, entity_type) is the primary key constraint.

2. **Contradictions invalidate, never delete.** When a new atom contradicts an existing one, we call `invalidate_atom()` (sets `expired_at`) — we never delete rows. This preserves audit trail and allows reversal. **Do not change this.**

3. **Graph is rebuildable from atoms.** The entities/relationships tables can be repopulated from existing atom files via `--rebuild --with-graph`. This means the graph is a derived view, not primary data — it's safe to drop and rebuild.

4. **Contradiction detection is best-effort.** Failures in entity extraction or contradiction detection must never abort the atom extraction pipeline. All Phase 2 logic in `cmd_extract` is wrapped in try/except.

5. **L2 distance > 1.2 skips LLM call.** This threshold is deliberately looser than the dedup threshold (0.55) — we want semantically adjacent facts, not just near-duplicates. Max 5 LLM calls per atom.
