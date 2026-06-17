# Session-Log Consolidation Contract

How a conversation becomes long-term vault memory, and the invariant every
consolidation surface must hold. Read this before adding a new surface or
touching `src/consolidation-core.ts`, `src/auto-compress.ts`,
`src/webui-consolidation.ts`, `src/memory-session-log.ts`, or
`scripts/memory_indexer.py`.

## The pipeline

```
conversation  ──►  surface (format)  ──►  consolidateSessionLog(spec)  ──►  writeSessionLogAndIndex  ──►  memory_indexer.py --add
                   (per-surface)         (shared envelope)                 (sync write + detached spawn)   (atom extract + L2 dedup + entity graph)
```

A surface turns a conversation into a markdown session log written under
`<vault>/Session-Logs/<YYYY-MM-DD>/<stem>.md`, then fire-and-forget indexes it
into long-term memory. Atom extraction, embedding dedup (L2 ≈ 0.55), and the
entity graph all live **inside the indexer** — that is the memory-write heart,
and it is the same subprocess for every surface.

## Roles

| Layer   | File                                                    | Owns                                                                                                                  |
| ------- | ------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Surface | `auto-compress.ts`, `webui-consolidation.ts`            | Trigger, message source, identity (`fileStem`), on-disk **format**, empty-guard, kill-switch, in-flight-key lifecycle |
| Core    | `consolidation-core.ts` → `consolidateSessionLog(spec)` | Vault resolution, the `Session-Logs/<date>/<stem>.md` path, the envelope assembly, dispatch                           |
| Seam    | `memory-session-log.ts` → `writeSessionLogAndIndex`     | Sync mkdir+write, detached `memory_indexer.py --add` spawn, `onSettle`                                                |
| Heart   | `scripts/memory_indexer.py --add`                       | Atom extraction, L2 dedup, entity graph, DB writes                                                                    |

`/compress` and `/handoff` are **host skills**, not TS — they operate on a
developer's Claude Code session, call the indexer as a subprocess, and carry
their own curation layers (CLAUDE.md rolling/pending sync, redaction,
retrospective). They are not part of this TS core; they align with it only by
the on-disk format convention below.

## The envelope (stable contract)

`consolidateSessionLog(spec)` assembles exactly:

```
---\n<frontmatter>\n---\n\n<body>\n
```

where `spec` carries each surface's **pre-rendered** `frontmatter` (a YAML block
with no `---` delimiters), `body`, `dateStr`, `fileStem`, `spawnLabel`, and an
optional `onSettle`. The core resolves the vault (returns `null` and fires
`onSettle` if none), builds the path with `path.join`, assembles the envelope,
and delegates to `writeSessionLogAndIndex`. It has **no try/catch** —
throw-transparency is intentional (see below).

### The critical invariant

**The bytes handed to `writeSessionLogAndIndex` must stay stable across
refactors.** The indexer parses the frontmatter and chunks the body; changing
the bytes silently changes what gets stored. `src/consolidation-golden.test.ts`
pins those bytes per surface as `@oracle` snapshots — if a refactor changes a
snapshot, it changed the contract. Do not update the snapshot to make a test
pass; investigate the regression.

Because the indexer is an unchanged subprocess dependency, any change that keeps
the bytes identical is provably memory-neutral by construction.

## Per-surface differences (intentional, not converged)

The two surfaces emit **different** formats, and that is deliberate — converging
them risks the indexer's body chunker, so it is out of scope:

|                  | auto-compress                        | webui-consolidation                                                      |
| ---------------- | ------------------------------------ | ------------------------------------------------------------------------ |
| Frontmatter type | `type: session`                      | `type: web-session`                                                      |
| tldr             | `tldr: \|` block scalar              | `tldr: "<json>"` (JSON-quoted, injection-safe)                           |
| Body line        | `**<sender>** (HH:MM): <text>`       | `**<role>**: <text>`                                                     |
| Identity         | `auto-<folder>-<HHMM>` (timestamped) | `webui-<sha256(messageText(first-user-msg))[:16]>` (stable, overwritten) |
| Trigger          | idle reset                           | conversation crosses turn/char threshold                                 |

## Adding a new surface

1. Render your own frontmatter block (no `---`) and body — own your format.
2. Guard emptiness and any threshold **before** calling the core (the core does
   not re-derive your source).
3. Call `consolidateSessionLog({ dateStr, fileStem, frontmatter, body, spawnLabel, onSettle? })`.
4. Decide your throw contract:
   - **Propagate** (like auto-compress): let the sync write failure reject; an
     orchestrator above you catches it.
   - **Swallow** (like webui): wrap the call in your own `try/catch`. If you
     hold an in-flight key, pass `onSettle` to release it on the detached
     spawn settling, release it again in your `catch` for the sync-throw path,
     and rely on the core firing `onSettle` on the no-vault skip — so the key
     can never get permanently stuck. `onSettle` may fire more than once; keep
     it idempotent (a `Set.delete`).
5. Add `@oracle` byte snapshots for your surface to `consolidation-golden.test.ts`.
6. **Do not modify `memory_indexer.py`** to accommodate a new format — align to
   the existing session-log convention instead.

## Related

- ADR index: `docs/decisions/INDEX.md` (read before touching `memory_indexer.py`).
- Linear: LIA-302 (this unification), LIA-253 (cross-channel write-back), LIA-254 (morning report).
