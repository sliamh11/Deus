# ADR: Procedure Memory On By Default (MCP Recall Path)

**Status:** Accepted
**Date:** 2026-06-28
**Scope:** scripts/memory_mcp_server.py, setup/codeintel.ts
**Supersedes:** None
**Reverses:** LIA-334 (procedure nodes dormant-by-default in `recall()`)
**Related:** memory-tree.md, threshold-calibration-sweep.md, benchmark-regression-gate.md

## Context

Procedure-memory (`kind: procedure` nodes captured via `/learn-procedure`) is
recalled through `memory_query.recall()`, whose default `exclude_kinds` drops
both `"standard"` and `"procedure"` — so procedures are **dormant by default**
across every caller (LIA-334 kill-switch, added when the node type was new and
unproven, measured neutral on the 136-query benchmark).

Two consequences made procedure-memory effectively unusable outside `~/deus`:

1. The globally-registered `deus-memory` MCP server (`memory_recall`) — the
   by-default external recall surface — never opted procedures in, so they were
   invisible in external projects even though general memory worked.
2. The `deus-memory` server was not registered by `/setup` at all (only
   codegraph + code-search were); users hand-added it.

The user requirement was explicit: procedure-memory must work in external
projects **seamlessly, with no manual interference** — no hand-editing MCP
config, no manual restart.

The LIA-337 intent gate (`_INTENT_GATE_ENABLED` in `recall()`, which runs for
**all** callers including `memory_recall`) passed blind re-validation: **100%
procedure recall / 94% near-domain veto** on a fresh 68-query set authored by an
agent blind to the classifier — i.e. procedures surface when a query is genuinely
procedural and are suppressed for near-domain factual queries. That removes the
original reason for dormant-by-default.

## Decision

Make procedure recall **on by default on the MCP path**, and register the
`deus-memory` server automatically during setup:

1. **`scripts/memory_mcp_server.py`** — `memory_recall` defaults procedures ON.
   The kill-switch is an **explicit** `DEUS_PROCEDURE_MEMORY=0` (any other value,
   including unset, keeps procedures eligible via `exclude_kinds={"standard"}`).

2. **`setup/codeintel.ts`** — a `setupDeusMemory` sub-step registers the
   `deus-memory` MCP server (mirrors the code-search sub-step: windows-skip,
   an `import mcp` probe of the interpreter the launcher will actually select —
   `.venv` first, mirroring `scripts/deus-memory-mcp`). Registration uses a direct
   `claude mcp add --scope user`, which is non-clobbering (re-adding an existing
   user-scope entry is a no-op: exit 0, nothing on stdout) and scope-correct (a
   project-scope entry elsewhere does not satisfy it). So a user's deliberate
   `DEUS_PROCEDURE_MEMORY=0` kill-switch is never overwritten.

### Intentional divergence: MCP default-on, host hook default-off

The host `UserPromptSubmit` hook (`scripts/memory_retrieval_hook.py`) keeps its
**opt-in/default-off** semantics (it enables procedures only when
`DEUS_PROCEDURE_MEMORY=1`). The two paths diverge on purpose: the hook
auto-injects into **every** host prompt (a personal, always-on channel that
should stay explicit), whereas the MCP server is the **broad external recall
surface** an agent pulls from deliberately — that is where "just works" belongs.

## Consequences

- **Seamless for:** (i) new users via `/setup`, and (ii) users who already
  registered `deus-memory` — they get default-on from the server-code change via
  `deus sync` (no re-registration, no config writes).
- **Not retroactive for:** a pre-existing user who never registered `deus-memory`
  — they need one `/setup` re-run to register it. ("Zero manual steps" is scoped
  to the two cases above, not a retroactive guarantee for unregistered installs.)
- **Kill-switch retained:** set `DEUS_PROCEDURE_MEMORY=0` on the `deus-memory`
  env; `/setup` will not overwrite it (register-if-absent).
- **Reversibility:** reverting the PR restores default-off in the server code; the
  registered `deus-memory` entry is an inert, legitimate memory server (also used
  for general recall) — nothing to unwind.

## Deferred follow-up

The MCP recall path (`memory_recall`) has no server-side context cap — unlike the
host hook's `MAX_CONTEXT_CHARS=4096`, `k` is uncapped at the MCP boundary. This is
**pre-existing** (it affects every recall, factual or procedural, not just the
default-on flip) and the `_wrap_untrusted` framing still applies, so it is a
token-budget concern, not a safety hole. Default-on widens the population it
touches, so a server-side cap (a `MAX_CONTEXT_CHARS` in `memory_recall` or a `k`
ceiling) is worth adding — tracked as a follow-up rather than bundled here, since
it changes behavior for all MCP recalls beyond this change's scope.

## Why no `deus sweep` (retrieval-sweep-gate does not fire)

The change alters only the **candidate-kind eligibility default** in the MCP
wrapper, not the retrieval pipeline. `calibrate_sweep()` (`deus sweep`) grid-
searches thresholds by calling `retrieve()` **directly** — it never goes through
`memory_recall` — and no benchmark fixture contains a procedure target, so a
sweep produces byte-identical output before and after this change. The correct
evidence is the LIA-337 intent-gate validation above, confirmed by an integration
check: a procedural query ("how do I prune merged git worktrees") surfaces the
procedure as the top result with procedures on and not at all with the
kill-switch, while a generic query surfaces no procedure.
