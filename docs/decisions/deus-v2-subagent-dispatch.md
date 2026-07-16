# Deus v2: Nested Subagent Dispatch Primitive (B8)

**Status:** Accepted
**Date:** 2026-07-16
**Scope:** `src/agent-runtimes/nested-dispatch.ts`, `src/agent-runtimes/nested-dispatch-tool.ts`, `src/agent-runtimes/deus-native-model.ts`, `src/agent-runtimes/deus-native-usage.ts`, `src/agent-runtimes/deus-native-backend.ts`
**Related:**

- `deus-v2-langchain-runtime.md` — establishes the `deus-native` `runTurn()`
  loop, its conservative web-only tool surface (`SAFE_TOOL_NAMES`), and its
  credential-proxy routing. This ADR extends that loop; it does not widen the
  tool surface and does not change credential handling.
- `deus-v2-permission-rules.md` — the declarative allow/deny permissions
  layer wrapping every tool call, including `dispatch_nested_agent` itself.
  This ADR does not add a new authorization mechanism; the dispatch tool is
  authorized exactly like any other parent tool.
- B6/LIA-406 (no standalone ADR — landed as inline `deus-native-backend.ts`
  doc comments, now extracted into `deus-native-usage.ts`) — the
  per-AI-message `RuntimeEvent.type === 'usage'` contract this ADR reuses
  unchanged for child dispatches.
- `multi-agent-orchestration-research.md` — the prior-art evaluation of
  Runner/LLM placement options that led to `src/multi-agent/orchestrator.ts`.
  This ADR does not touch that module; see Decision 1 below for how the two
  primitives coexist.

## Context

`src/multi-agent/orchestrator.ts` already dispatches subagent work: it
topologically schedules `SubagentTask[]`, launches independent
container/runtime sessions per task, and interprets `[STATUS:...]` text
markers to decide success. That primitive is deliberately container-based,
multi-session, and marker-driven — a good fit for its own use case, but not
usable from inside a single in-process `deus-native` turn, and not
schema-validated.

LIA-408 (B8) asks for a different, smaller primitive: from *inside* one
`DeusNativeRuntime.runTurn()`, the parent agent must be able to delegate a
self-contained subtask to a nested `createAgent` instance, with:

1. A real nested `createAgent` dispatch (not a text-based simulation).
2. A mandatory, explicit output contract per dispatch.
3. Contract-invalid output rejected or surfaced as a contract failure, never
   silently accepted as success.
4. A model selected independently per dispatch, not inherited from the
   parent.
5. Traceable agent/model metadata on every dispatch result, including
   failures.

No such primitive existed before this ticket — `deus-native-backend.ts`'s own
module doc comment listed "nested subagent dispatch (B8/LIA-408)" as an
explicit non-goal through B7.

## Decision

### 1. A new, smaller primitive — not a replacement for the orchestrator

`nested-dispatch.ts`'s `createNestedDispatcher()` is a **Deus-owned,
in-process, one-shot child-agent factory**, scoped to a single `runTurn()`.
It is architecturally distinct from `src/multi-agent/orchestrator.ts`:

| | `multi-agent/orchestrator.ts` | `nested-dispatch.ts` (B8) |
| --- | --- | --- |
| Isolation | Independent container/runtime session per task | One in-process LangChain `createAgent` graph per dispatch, same host process |
| Scheduling | Topological, parallel fan-out across `SubagentTask[]` | One dispatch per tool call, driven by the parent's own ReAct loop |
| Success signal | `STATUS_MARKER_RE` text-marker parsing | A Zod-compatible output contract, independently validated |
| Session/checkpoint | Each task gets its own session | No session, no checkpointer — one-shot, discarded after the call |
| Lifecycle | Long-running, can outlive the dispatching turn | Bound to the parent turn; returns before the parent's tool-call node completes |

`src/multi-agent/orchestrator.ts`, `src/multi-agent/types.ts`, and their
topological/parallel scheduling, container lifecycle, and `STATUS_MARKER_RE`
parsing are **unchanged by this ticket**. The two primitives coexist because
they solve different problems at different isolation boundaries; this ADR
does not claim one subsumes the other.

### 2. Canonical seam and the independent oracle

The implementation is built directly against an **independently authored
oracle** (`nested-dispatch.oracle.test.ts`, provenance in `ORACLE_BRIEF.md`),
written from the LIA-408 spec before any implementation existed, blind to
this design. Its minimal seam —

```ts
const dispatcher = createNestedDispatcher({ resolveModel });
const result = await dispatcher.dispatch({ agentId, model, prompt, outputContract });
```

— is preserved unweakened. `createNestedDispatcher()`'s only *required*
dependency is `resolveModel(modelId) => BaseChatModel`; every other
dependency (`buildChildTools`, `buildChildMiddleware`, `parentSessionId`,
`provider`, `onUsage`) is optional and defaults to production-safe no-ops, so
the oracle's minimal construction keeps working unchanged as production
dependencies are added around it.

### 3. Discriminated result, always carrying trace metadata (AC5)

```ts
type NestedDispatchResult<T> =
  | { status: 'success'; output: T; metadata: NestedDispatchMetadata }
  | { status: 'contract_failure'; error: {...}; metadata: NestedDispatchMetadata }
  | { status: 'error'; error: {...}; metadata: NestedDispatchMetadata };
```

`metadata.agentId`/`metadata.model` are populated on **every** path,
including a failure before `resolveModel` is ever called (request
validation) and a failure inside child construction/invocation. A failed
dispatch is therefore always traceable to which subagent and which requested
model produced it. `metadata.usage` (child-local token counts) and
`metadata.parentSessionId`/`metadata.provider` are populated only when a
production dependency actually supplies them — never fabricated.

### 4. Output contract enforcement is Deus-owned, not LangChain's structured-output machinery (AC2/AC3)

The child is instructed (via the tool adapter's prompt construction) to
return JSON matching the declared contract, but that instruction is **not**
the correctness boundary. After the child's terminal `AIMessage`, the
dispatcher:

1. Parses JSON text when the content is a JSON string (tolerant of the
   common "model returns a JSON string" case); non-JSON-string content is
   passed through unchanged to the contract.
2. Runs the contract's own `safeParseAsync` — a **Zod-compatible**, minimal
   interface (`{ safeParseAsync }`), not a hard dependency on any specific
   Zod version/import path, so the oracle's own hand-authored contract and
   the production `z.fromJSONSchema()`-compiled contract both satisfy it.
3. Returns only the **parsed** value as `output` on success. Invalid raw
   output is never echoed back to the parent as `output` — only sanitized
   validation issues are surfaced in `contract_failure`.

This deliberately avoids depending on LangChain's `toolStrategy`/
`responseFormat`/`structuredResponse` machinery (evaluated and documented in
the implementation plan as the framework-native alternative) for
correctness: those APIs govern how the *child* is prompted/constrained, not
whether Deus independently validates what actually came back. A future
optimization may add `toolStrategy` as a best-effort accuracy improvement,
but the independent post-validation path must remain the actual contract
boundary regardless.

### 5. The parent-facing tool adapter compiles JSON Schema, never trusts it uncompiled (AC2)

`nested-dispatch-tool.ts`'s `buildNestedDispatchTool()` is the only path from
the parent model to the core dispatcher. Every tool call must declare an
inline `outputContract: { name, description?, schema }` (a JSON Schema
object) — there is no global/implicit response type and no reuse of the
older `[STATUS:...]` marker convention. The adapter compiles `schema` via
`z.fromJSONSchema()` (zod@4.4.3; its own `.d.ts` labels this
"semi-experimental") inside a try/catch, **before** calling `resolveModel` or
constructing any child. An unconvertible or malformed schema becomes a
`contract_failure` result at that point — schema-compilation risk is isolated
to this one adapter and cannot reach model resolution or child execution.

On success, the adapter returns the JSON-serialized result as a plain string
— LangChain's own tool-invocation machinery wraps that into a `status:
'success'` `ToolMessage` when the parent's ReAct loop actually calls it
(verified against `@langchain/core@1.2.2`'s `StructuredTool.call()`/
`_formatToolOutput`). On `contract_failure` or `error`, the adapter itself
constructs an error-status `ToolMessage`, tied to the real `ToolRuntime.
toolCallId` LangChain's `ToolNode` supplies during actual graph execution —
matching the model-visible denial pattern `buildPermissionsMiddleware()`
already uses in `middleware-stack.ts` (deny: a `status: 'error'` `ToolMessage`
without invoking further logic; the parent's ReAct loop continues and may
retry or proceed without the result).

### 6. Independent per-dispatch model selection (AC4)

`buildProxyRoutedChatAnthropic()` (moved unchanged from
`deus-native-backend.ts` into `deus-native-model.ts`) is parameterized with
`modelId` instead of hardcoding `'claude-opus-4-8'`. The parent still passes
exactly that constant (now named `PARENT_DEFAULT_MODEL`) — B8 introduces no
parent-side model selection. Every nested dispatch's `resolveModel` callback
calls this **same factory, fresh, per dispatch**, passing the dispatch's own
requested model id:

```ts
resolveModel: (modelId) => buildProxyRoutedChatAnthropic(runContext, modelId)
```

There is still exactly one provider (Anthropic) and one credential-proxy
route (`PROXY_BIND_HOST:CREDENTIAL_PROXY_PORT`, `detectAuthMode()`,
per-group `x-deus-proxy-token`) — only the model id varies per call, and it
is never cached or inherited across dispatches or from the parent.

### 7. Child isolation: fresh tools/middleware, no checkpointer, no recursive nesting

Each dispatch's `buildChildTools`/`buildChildMiddleware` factories are called
**again, fresh**, on every individual dispatch — never once and reused:

- `buildChildTools` rebuilds `buildSafeTools()` with the same group-scoped
  broker context and web-fetch allowlist the parent uses. The child's tool
  set is therefore a strict subset of the parent's `SAFE_TOOL_NAMES`
  boundary (`deus-v2-langchain-runtime.md`), and the child **never** receives
  `dispatch_nested_agent` itself — this factory alone does not introduce
  recursive/child-of-child nesting.
- `buildChildMiddleware` rebuilds `buildMiddlewareStack()` with the same
  resolved `permissionProfile` the parent's own middleware uses — a child
  never runs under a looser policy than its parent.
- The child `createAgent({ model, tools, middleware })` call omits
  `checkpointer` entirely. Children are one-shot: they create no
  `RuntimeSession`, no session DB row, and no LangGraph checkpoint thread.
  `metadata.parentSessionId` is trace context only (the parent's
  `outgoingSessionId`, echoed through), never a real child thread id.

### 8. Usage accounting is centralized, not duplicated (B6/LIA-406 extension)

`deus-native-usage.ts`'s `TurnUsageCollector` (extracted unchanged in
semantics from B6's inline loop) is the single code path that builds every
`RuntimeEvent.type === 'usage'` event and the turn-level aggregate — for
**both** the parent's own AI messages and every dispatched child's. The
dispatch tool's `onUsage` callback forwards each completed child `AIMessage`
into the same collector, using the child's own resolved model id but the
**parent's** `outgoingSessionId` (a dispatched child never gets a session id
of its own). The child's messages are never inserted into the parent's own
LangGraph message state — the collector records them once, through the
callback, so the parent's own later message-scan cannot double-count them.
`RunResult.usage` is therefore the exact sum of parent-plus-every-child usage,
omitted entirely (never a fabricated zero) when nothing in the turn reported
usage.

## Alternatives Considered

- **Reuse `src/multi-agent/orchestrator.ts` directly for in-turn delegation.**
  Rejected: that primitive is container-based and session-scoped by design —
  adapting it to a one-shot, in-process, schema-validated call inside a
  single `runTurn()` would require rebuilding most of its scheduling and
  session machinery for a fundamentally different isolation boundary. A
  smaller, purpose-built primitive is simpler to reason about and test.
- **Trust LangChain's `responseFormat`/`toolStrategy` structured-output
  machinery as the sole correctness boundary.** Rejected: those APIs
  constrain the *child's* generation but Deus still needs an independent
  validation step to guarantee AC3 (invalid output never masquerades as
  success) regardless of how reliably the framework enforces its own
  contract in a given version. Independent post-validation is required
  either way, so it is the single source of truth here.
- **Give the child the parent's full tool list (including
  `dispatch_nested_agent`) to support arbitrary-depth delegation.** Rejected
  as out of scope: the ACs describe one level of delegation with an explicit
  contract; recursive/child-of-child dispatch, depth limits, and cancellation
  protocols are real future design questions this ticket does not need to
  answer, and omitting the dispatch tool from the child's list is the
  simplest way to guarantee no accidental recursion today.
- **Let a child inherit or share the parent's checkpointer/session.**
  Rejected: sharing checkpoint state would make a one-shot delegated subtask
  indistinguishable from a real resumable session, complicate usage
  attribution, and risk leaking parent conversation state into a subtask that
  the AC's isolated "self-contained task" framing does not call for.

## Rationale

The dispatcher/tool split (core primitive vs. LangChain tool adapter) is an
Adapter pattern: `nested-dispatch.ts` stays independently testable without a
LangChain tool-call context (its own unit tests construct requests directly),
while `nested-dispatch-tool.ts` owns everything LangChain-specific
(`ToolRuntime.toolCallId`, JSON-Schema compilation, `ToolMessage`
construction). The fresh-factory-per-dispatch design
(`buildChildTools`/`buildChildMiddleware`/`resolveModel` all called again per
call) is a deliberate, objective choice to prevent state leakage between
sibling dispatches — it is directly falsified by the "fresh per-dispatch tool/
middleware factories" and "independent per-dispatch model resolution" test
suites. Centralizing usage accounting into one collector (rather than a
second, child-specific usage loop) is a maintainability choice: the B6
contract's semantics (unconditional emit, undefined over fabricated zero) now
have exactly one implementation to keep correct.

## Consequences

**Positive:**

- The five LIA-408 ACs are satisfied by one small, independently-oracled
  primitive that composes with the existing middleware/tool/credential
  boundaries rather than duplicating them.
- `src/multi-agent/orchestrator.ts` is untouched — no risk to its existing
  container-based scheduling behavior.
- `RunResult.usage`/`RuntimeEvent.type === 'usage'` now correctly reflect
  delegated work, not just the parent's own model calls — a prerequisite for
  any future cost/billing visibility into subagent-heavy turns.
- `deus-native-model.ts`/`deus-native-usage.ts` extractions make the model
  factory and usage collector independently testable/reusable, reducing
  `deus-native-backend.ts`'s own size and coupling.

**Negative:**

- The dispatch tool's contract-compilation path depends on
  `z.fromJSONSchema()`, explicitly semi-experimental upstream; unsupported
  JSON Schema features could behave unexpectedly. Mitigated by isolating
  compilation to one adapter, catching conversion errors before any child
  executes, and independently safe-parsing every returned value regardless
  of how compilation went.
- A child that ignores its requested output shape produces a traceable
  `contract_failure`, not a silent retry — the parent model must itself
  decide whether/how to retry a failed dispatch; B8 adds no automatic
  schema-repair loop.
- `read-only` permission-profile handling for `dispatch_nested_agent` is
  fail-closed by inheriting `permission-rules.ts`'s existing default-deny for
  unreviewed meta-tools (it is not in B7's reviewed catalog); a future ticket
  may need to explicitly decide whether `read-only` turns should be able to
  delegate at all.

**Risks:**

- Arbitrary child model ids route through the existing Anthropic credential
  proxy with no allowlist/registry — a malformed or unexpected model id
  surfaces as a normal `subagent_execution_failed` result (traceable), but a
  cost-control policy for permitted child models is a separate, later
  decision.
- Nested dispatch adds a second in-process `createAgent` graph per dispatch
  within the same host turn; very high dispatch fan-out from one turn is not
  rate-limited or budgeted by this ticket.

## Reversibility and rollback

This change is **REVERSIBLE and additive-only**: it adds four new files
(`nested-dispatch.ts`, `nested-dispatch-tool.ts`, `deus-native-model.ts`,
`deus-native-usage.ts`) plus one wiring edit inside `DeusNativeRuntime.
runTurn()` (constructing the dispatcher/tool and routing usage through the
shared collector). No schema/data migration, no change to `RuntimeEvent`/
`RunResult`/`AgentRuntime` types, and no change to `claude`/`openai`/
`llama-cpp`. Rollback is a single revert: remove the `dispatch_nested_agent`
tool from the parent's tool list and the four new files; restore
`deus-native-backend.ts`'s inline `buildProxyRoutedChatAnthropic`/usage loop
from this commit's parent if desired (or simply leave the extracted modules
in place, unused, since they change no external behavior on their own).

## Non-Goals

- No modification to `src/multi-agent/orchestrator.ts`, `src/multi-agent/
  types.ts`, its topological/parallel scheduling, container lifecycle,
  prompt templates, or `STATUS_MARKER_RE` parsing.
- No widening of `SAFE_TOOL_NAMES`, host filesystem/shell access, or any new
  mutating tool path — children receive the exact same web-only surface the
  parent does, built fresh per dispatch.
- No recursive child-of-child dispatch, dependency graph, parallel fan-out,
  cancellation protocol, depth limit, or budget scheduler. Children never
  receive `dispatch_nested_agent`.
- No persistent child sessions, child checkpointer namespaces, resume
  semantics, or child session DB rows.
- No new provider router, model alias registry, pricing policy, or automatic
  fallback — only the model id varies within the existing Anthropic path.
- No new `RuntimeEvent`, `RunResult`, or `AgentRuntime` type, and no change
  to `handoffs` capability semantics (`DEUS_NATIVE_CAPABILITIES.handoffs`
  remains `false` — a dispatched child returns control to the same parent
  turn; it never transfers conversation/session ownership).
- No automatic schema-repair loop or hidden retry policy on contract
  failure.
