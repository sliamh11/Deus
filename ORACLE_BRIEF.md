# Task Brief — LIA-408: Nested Subagent Dispatch (B8)

Branch: `lia-408-nested-subagent-dispatch`
Worktree: `~/deus/.claude/worktrees/deus-v2-b8`

## Description

Add a Deus-owned subagent dispatch primitive based on nested `createAgent`
instances. Require an explicit output contract for each dispatch and support
a distinct model selection per agent. Replaces Claude Code's Agent tool with
nested Deus-owned agents.

Risk: MEDIUM — LangChain's middleware subsystem is less than one year old in
production; Deus must replace Claude Code's context-isolation/orchestration
scaffolding with its own.

## Acceptance Criteria

- [ ] A parent agent can dispatch a nested `createAgent` instance.
- [ ] Each dispatch declares an explicit output contract.
- [ ] Invalid subagent output is rejected or surfaced as a contract failure.
- [ ] Each subagent can select a model independently of the parent.
- [ ] Dispatch results return to the parent with traceable agent and model
      metadata.

## Existing public surface (read-only — do not treat as the new implementation)

This module lands inside `src/agent-runtimes/`, the same package as the
prior B1–B7 milestones on this roadmap (LIA-401..407). "Nested subagent
dispatch" is explicitly named as future work, out of scope, in the B1 module
doc comment:

- `src/agent-runtimes/deus-native-backend.ts:57` — non-goals list: "Replay-
  safety auditing (B5/LIA-405), token/usage accounting events (B6/LIA-406),
  **nested subagent dispatch (B8/LIA-408)**."

Relevant pre-existing types/contracts (`src/agent-runtimes/types.ts`):

- `RunContext` (line 34) — carries `prompt`, `groupFolder`, `chatJid`,
  `backendConfig?: Record<string, unknown>`, `effort?`, etc. Any per-dispatch
  model/contract config would flow through a shape like this or a new
  sibling type — not existing yet.
- `RunResult` (line 90) — `{ status: 'success' | 'error', result: string |
  null, sessionRef?, usage?, error? }`. This is the shape a top-level
  `runTurn` returns; a subagent dispatch result contract is a NEW concept,
  not yet defined anywhere.
- `RuntimeEvent` / `RuntimeEventSink` (lines 62–88) — the discriminated
  union of events a runtime emits mid-turn (`output_text`, `activity`,
  `tool_call`, `usage`, `session`, `turn_complete`, `error`). A dispatch/
  agent-call event, if any, does not exist yet in this union.
- `AgentRuntime` interface (line 106) — `name()`, `capabilities()`,
  `startOrResume()`, `runTurn()`, `close()`. The top-level runtime contract;
  a nested dispatch primitive is a separate, smaller surface used FROM
  within a `runTurn`, not a new `AgentRuntime` implementation itself.

Relevant pre-existing implementation surface
(`src/agent-runtimes/deus-native-backend.ts`):

- Lines 67–90 (imports) — `createAgent` and `AgentMiddleware` are imported
  from `langchain` today; the parent agent is built with `createAgent({
  model, tools, middleware, checkpointer })` at line ~366.
- `buildProxyRoutedChatAnthropic(runContext)` (line 175) — the ONLY model
  construction path that exists today, and it is **hardcoded** to
  `model: 'claude-opus-4-8'` (line 187) with an explicit comment noting
  `runContext.effort` is not yet honored. There is currently **no per-run,
  let alone per-subagent, model selection anywhere in this codebase** — this
  ticket is the first place independent model selection would be
  introduced.
- `buildMiddlewareStack` / `resolveMiddlewareStackConfig`
  (`middleware-stack.ts`) — the ordered, per-layer-toggleable middleware
  stack (permissions → wardens → memory → telemetry) wired into the parent
  `createAgent` call. Only the permissions layer
  (`permission-rules.ts`, B7/LIA-407) is real; the others are observe-only
  placeholders.
- `buildSafeTools` / `ToolBrokerContext`
  (`tool-broker-langchain-adapter.ts`) — builds the tool list passed to
  `createAgent`; today's tool surface is `web_search`/`web_fetch` only
  (`SAFE_TOOL_NAMES`).
- `getCheckpointer()` (`checkpointer.ts`) — the LangGraph checkpointer wired
  into the parent `createAgent` for conversation continuity (B4/LIA-404).
  Not established yet whether/how a nested dispatch shares or isolates
  checkpoint state from its parent — that is part of what this ticket must
  decide and the oracle should probe.

Existing oracle-test naming/pattern convention in this package (for
reference only — do not copy content, these test PRIOR tickets):
`src/agent-runtimes/middleware-stack.oracle.test.ts`,
`src/agent-runtimes/permission-rules.oracle.test.ts`,
`src/agent-runtimes/deus-native-tool-scope.oracle.test.ts`. Each uses
`vitest` (`npm test` → `vitest run`) and, where a real `createAgent` graph
is exercised, LangChain's `FakeToolCallingModel` test double rather than a
live model call — follow this pattern rather than hitting a real model API
from the oracle test.

No `.claude/.plan-scope-*.md` file exists yet for LIA-408 (unlike B7, which
had `.claude/.plan-scope-b7.md`) — this brief and the Linear ticket ACs above
are the full spec available. Do not infer implementation approach from
absence of a plan doc; derive the contract from the ACs and description only.

## Exact oracle output file path(s)

Write the oracle test(s) to:

- `src/agent-runtimes/nested-dispatch.oracle.test.ts`

Follow the existing `.oracle.test.ts` convention in this directory (see
above) for structure, spec-citation header comment, and `@oracle` tagging
per the oracle-author role's required format.
