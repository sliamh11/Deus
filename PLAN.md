# LIA-408 — Nested Subagent Dispatch Primitive

## Scope Restatement

This change implements exactly these five acceptance criteria for the `deus-native` runtime:

- [ ] A parent agent can dispatch a nested `createAgent` instance.
- [ ] Each dispatch declares an explicit output contract.
- [ ] Invalid subagent output is rejected or surfaced as a contract failure.
- [ ] Each subagent can select a model independently of the parent.
- [ ] Dispatch results return to the parent with traceable agent and model metadata.

The implementation is limited to a one-shot, in-process child agent created from the current `DeusNativeRuntime.runTurn()` in `src/agent-runtimes/deus-native-backend.ts`. It does not create a container or call `AgentRuntime.runTurn()` recursively.

`src/multi-agent/orchestrator.ts` remains unchanged. Its `MultiAgentOrchestrator.dispatch()` path topologically schedules `SubagentTask[]`, launches independent container/runtime sessions, and interprets `STATUS_MARKER_RE` text. B8 instead creates a child LangChain agent inside one `deus-native` turn and validates its output against a schema. The two primitives coexist because they have different isolation, scheduling, and lifecycle boundaries; B8 does not reuse the status-marker parser and does not subsume the container orchestrator in this ticket.

The following boundaries also remain unchanged:

- `SAFE_TOOL_NAMES` in `src/agent-runtimes/tool-broker-langchain-adapter.ts` remains exactly `web_search` and `web_fetch`; no shell, filesystem, browser, IPC, scheduling, or mutation tool is added.
- The `claude`, `openai`, and `llama-cpp` backends are untouched.
- `src/agent-runtimes/types.ts` keeps the existing `RuntimeEvent` and `RunResult` vocabulary; no parallel nested-agent event/result protocol is added.
- `RuntimeCapabilities.handoffs` remains `false` in `deus-native-backend.ts:105-134`. A child tool call returns control to the same parent turn; it does not transfer conversation or session ownership, so the five ACs do not justify advertising handoff support.

## Design

### Consolidated File Map

| Path | Change |
| --- | --- |
| `ORACLE_BRIEF.md` | Retain as the provenance for the already-authored independent oracle; do not replace it with a self-authored oracle. |
| `src/agent-runtimes/nested-dispatch.oracle.test.ts` | Keep as the canonical walking-skeleton oracle under the default `src/**/*.test.ts` Vitest glob; make its existing `createNestedDispatcher({ resolveModel })` seam pass without weakening its assertions. |
| `src/agent-runtimes/nested-dispatch.test.ts` | Add focused unit tests for request validation, JSON-schema compilation, success/failure envelopes, tool adaptation, fresh child dependencies, and usage metadata. |
| `src/agent-runtimes/deus-native-checkpointer-integration.test.ts` | Extend the existing real-`createAgent`/`FakeToolCallingModel` integration harness to prove child usage is attributed to the parent session and reconciled into `RunResult.usage` without creating child checkpoint state. |
| `src/agent-runtimes/nested-dispatch.ts` | Add the oracle-defined core factory, request/result types, one nested `createAgent` invocation per dispatch, output extraction, independent schema validation, and trace metadata. |
| `src/agent-runtimes/nested-dispatch-tool.ts` | Add the parent-facing `dispatch_nested_agent` LangChain tool, inline JSON-schema contract compilation, child prompt construction, and `ToolMessage` success/error adaptation. |
| `src/agent-runtimes/deus-native-model.ts` | Extract and parameterize `buildProxyRoutedChatAnthropic()` so the parent retains its default while each child supplies its own model id through the same credential-proxy route. |
| `src/agent-runtimes/deus-native-usage.ts` | Extract the B6 per-AI-message usage emission/aggregation logic so parent and child calls share the exact `RuntimeEvent` and `RunResult.usage` semantics. |
| `src/agent-runtimes/deus-native-backend.ts` | Construct the dispatcher/tool inside `runTurn()`, give children fresh safe tools and middleware, aggregate child usage, and remove B8 from the module's deferred non-goals. |
| `docs/decisions/deus-v2-subagent-dispatch.md` | Add the B8 ADR covering the in-process boundary, schema contract, failure semantics, model factory, middleware/tool isolation, session/usage association, and coexistence with the container orchestrator. |
| `docs/decisions/INDEX.md` | Index the new ADR. |
| `docs/decisions/deus-v2-langchain-runtime.md` | Add the B8 decision as the governed extension of the original single-agent `deus-native` loop and preserve the web-only tool boundary. |
| `AGENTS.md` | Add `nested-dispatch.ts`/`nested-dispatch-tool.ts` to the core-entrypoint map, as required when an architectural entrypoint changes. |

`package.json`, `package-lock.json`, `src/agent-runtimes/types.ts`, `src/multi-agent/orchestrator.ts`, and `src/multi-agent/types.ts` require no change. `zod@^4.4.3` is already a direct production dependency.

### Canonical Primitive and Parent Tool

Implement against the independent oracle's seam, not a competing interface:

```ts
const dispatcher = createNestedDispatcher({ resolveModel });
const result = await dispatcher.dispatch({
  agentId,
  model,
  prompt,
  outputContract,
});
```

`createNestedDispatcher()` accepts the oracle-required `resolveModel(modelId)` dependency and optional production dependencies for fresh child tools, fresh child middleware, provider/session trace context, and child-message usage observation. Those optional dependencies preserve the minimal `createNestedDispatcher({ resolveModel })` construction used by `nested-dispatch.oracle.test.ts`.

`dispatch()` validates `agentId`, `model`, `prompt`, and the presence of a Zod-compatible `outputContract` before calling `resolveModel`. It resolves the model on every call, constructs one child with `createAgent({ model, tools, middleware })`, invokes it with only the explicit child prompt, and validates the terminal child output. It does not call another `AgentRuntime`, use a container, or share the parent's transcript.

The production adapter `buildNestedDispatchTool()` exposes this core primitive to the parent as a LangChain tool named `dispatch_nested_agent`. The parent model emits:

```ts
{
  agentId: string;
  model: string;
  prompt: string;
  outputContract: {
    name: string;
    description?: string;
    schema: Record<string, unknown>;
  };
}
```

The schema is inline and required on every tool call, making the contract explicit per dispatch rather than relying on a hidden global response type or the older `[STATUS:...]` convention. `nested-dispatch-tool.ts` adds the contract name, description, and serialized schema to the child prompt with an instruction to return one JSON value, compiles the schema, and passes the compiled validator to `dispatcher.dispatch()`.

### Design Patterns

- **Factory with dependency injection:** `createNestedDispatcher()`, `buildProxyRoutedChatAnthropic()`, and the child tool/middleware builders create isolated instances per dispatch. Injecting `resolveModel` is the oracle-defined test seam and prevents the dispatcher from hard-coding a provider client.
- **Strategy:** each dispatch supplies its own output-contract parser, while production compiles the parent-declared JSON Schema into that strategy. The model resolver is likewise a per-model strategy selected by the request's `model` value.
- **Adapter:** `buildNestedDispatchTool()` translates between the LangChain tool-call/`ToolMessage` protocol and the Deus-owned `NestedDispatchResult` union. The core dispatcher stays independently testable and does not depend on a parent tool-call id.

### Output Contract and Enforcement

Use Zod as the Deus-owned validation boundary:

1. The parent tool requires an inline JSON Schema for every dispatch.
2. `nested-dispatch-tool.ts` calls `z.fromJSONSchema()` inside a guarded compiler. An unconvertible or malformed schema becomes a contract failure before model resolution or child construction.
3. `nested-dispatch.ts` checks at runtime that `outputContract` exposes the required parse operation, so dynamically malformed direct callers fail before `resolveModel`; this is required by the existing oracle's missing-contract case.
4. After child invocation, the dispatcher extracts the final AI response, parses JSON when the content is JSON text, and runs the compiled contract's asynchronous safe parse. Only the parsed value is returned as `output`.
5. Invalid JSON, missing terminal output, or schema-invalid data returns `status: 'contract_failure'`. Invalid raw output is not echoed back into the parent context; only sanitized validation issues are surfaced.

The core result is a discriminated union:

```ts
type NestedDispatchResult<T> =
  | {
      status: 'success';
      output: T;
      metadata: NestedDispatchMetadata;
    }
  | {
      status: 'contract_failure';
      error: { code: 'subagent_output_contract_failed'; message: string; issues?: unknown };
      metadata: NestedDispatchMetadata;
    }
  | {
      status: 'error';
      error: { code: 'subagent_execution_failed'; message: string };
      metadata: NestedDispatchMetadata;
    };
```

`NestedDispatchMetadata` always contains `agentId` and the resolved/requested `model`; the production path also includes `provider: 'anthropic'`, `parentSessionId`, and child-local `usage` when the provider reported it. Metadata is attached on success, contract failure, and execution failure so a failed dispatch remains traceable.

`buildNestedDispatchTool()` serializes success as a normal tool result. For `contract_failure` or `error`, it returns a `ToolMessage` with the current `ToolRuntime.toolCallId`, `name: 'dispatch_nested_agent'`, and `status: 'error'`. This matches the model-visible denial pattern in `buildPermissionsMiddleware()` at `src/agent-runtimes/middleware-stack.ts:166-201`: the parent remains inside its ReAct loop and may retry or handle the failure. Contract and execution failures use distinct codes.

LangChain structured-output helpers are not the correctness boundary. The child is instructed to emit JSON, but Deus validates the returned value independently. This choice keeps the core compatible with the canonical oracle's Zod-compatible parser and bare final `AIMessage`, and avoids making B8 correctness depend on LangChain's response-format retry/control-flow behavior. A future optimization may add `toolStrategy`; it must retain the independent post-validation path.

### Verified Signatures

The external API claims are verified against the packed artifacts for the locked production versions (`langchain@1.5.3`, `@langchain/core@1.2.2`, `zod@4.4.3`):

- `createAgent` has overloads for ordinary, JSON-schema, Zod, `ToolStrategy`, and provider-strategy response formats in `langchain/dist/agents/index.d.ts:161-232`.
- `responseFormat` produces `structuredResponse` when configured in `langchain/dist/agents/types.d.ts:606-658`, and `invoke()` exposes it in the final state in `langchain/dist/agents/ReactAgent.d.ts:111-134`.
- `toolStrategy()` accepts Zod/serializable/JSON schemas in `langchain/dist/agents/responses.d.ts:105-110`; its `handleError` option is `boolean | string | callback`, with `false` meaning throw, at `responses.d.ts:82-103`.
- `StructuredOutputParsingError` is the framework error for schema-invalid structured tool arguments in `langchain/dist/agents/errors.d.ts:14-20`.
- `ToolRuntime.toolCallId` is a required string in `@langchain/core/dist/tools/types.d.ts:286-361`; the `tool()` overloads accepting `(input, runtime)` are in `@langchain/core/dist/tools/index.d.ts:222-227`, and structured tools may return `ToolMessage` at `index.d.ts:16`.
- `z.fromJSONSchema(schema, params?)` returns a `ZodType` in `zod/v4/classic/from-json-schema.d.ts:9-11`; that declaration labels the converter semi-experimental, so compilation is isolated and regression-tested rather than trusted implicitly.

Of these APIs, B8 relies directly on `createAgent`, `ToolRuntime.toolCallId`, `ToolMessage`, and `z.fromJSONSchema()`. `toolStrategy`, `structuredResponse`, `handleError`, and `StructuredOutputParsingError` are documented here because they were evaluated as the framework-native alternative, but are deliberately not required for contract correctness.

### Independent Model Selection

Move `buildProxyRoutedChatAnthropic()` from `deus-native-backend.ts:175-211` into `deus-native-model.ts` and change its signature to accept `modelId`:

```ts
buildProxyRoutedChatAnthropic(
  runContext: RunContext,
  modelId: string,
): ChatAnthropic
```

The parent passes the existing default constant `claude-opus-4-8`. `resolveModel` passes each dispatch's requested child model to the same factory on every call; it does not inherit or cache the parent's client. Both parent and child preserve `PROXY_BIND_HOST:CREDENTIAL_PROXY_PORT`, `detectAuthMode()`, placeholder credentials, and the per-group `x-deus-proxy-token`. The provider remains Anthropic for this ticket; the AC requires an independently selected model, not a new multi-provider router.

Trace metadata reads the actual constructed client's `model` value when available and otherwise retains the requested id. Tests dispatch two children with different model ids and assert two separate resolver calls and clients.

### Middleware, Tools, Context, and Session Isolation

The parent keeps its existing middleware/checkpointer setup at `deus-native-backend.ts:322-385`. Each child dispatch calls `buildMiddlewareStack(resolveMiddlewareStackConfig(), { permissionProfile })` again. This produces a fresh array and fresh middleware instances in the same canonical `permissions -> wardens -> memory -> telemetry` order; no parent middleware object is inherited by reference. The child does not receive the parent's session-open `promptLifecycle` middleware because it is not opening or resuming a user conversation.

Each child also calls `buildSafeTools()` again with the same group-scoped broker context and web-fetch allowlist. The child never receives `dispatch_nested_agent`, so its tool set is a strict subset of the parent's and recursive nesting is not introduced. No dispatch argument can select or add tools.

The parent's permission middleware wraps `dispatch_nested_agent`. Under the current `default` profile it is allowed. Under `read-only`, the existing fail-closed default denies this new meta-tool because it is not in the reviewed broker catalog; B8 does not silently weaken or reclassify that profile.

Children are one-shot and receive no checkpointer. They do not create a `RuntimeSession`, session DB row, or child thread id. The production dispatcher receives the parent's `outgoingSessionId` only as trace context, and returns that as `metadata.parentSessionId`.

### Usage and Result Propagation

Extract the B6 loop at `deus-native-backend.ts:403-465` into a turn-scoped collector in `deus-native-usage.ts`. Its `record(messages, { provider, model })` method:

- Emits one existing `RuntimeEvent.type === 'usage'` for every child or parent `AIMessage`.
- Uses the parent `outgoingSessionId` for every event, the actual provider/model for that model call, and `undefined` token fields when `usage_metadata` is absent.
- Returns the local aggregate for inclusion in child metadata while maintaining a combined aggregate for the final parent `RunResult.usage`.
- Omits aggregates when no AI message supplied usage metadata; it never fabricates zeros.

The child graph's messages are not inserted into the parent graph. The dispatcher records them once through the shared collector, then returns only the validated result envelope as the parent tool message. The parent graph's later message scan therefore cannot double-count child usage. `RunResult.sessionRef` remains the existing parent session, and `RunResult.usage` becomes the sum of reported parent and child usage without changing `src/agent-runtimes/types.ts`.

## Concrete Implementation Steps

1. **Freeze the independent oracle as the canonical red test.**

   Preserve `ORACLE_BRIEF.md` and `src/agent-runtimes/nested-dispatch.oracle.test.ts`. Run the oracle in its current red state and record that the failure is the missing `./nested-dispatch.js` import. Do not add `subagent-dispatch.oracle.test.ts`, rename the seam, mock away `createAgent`, or weaken the existing assertions.

2. **Add the core `createNestedDispatcher` factory.**

   Create `src/agent-runtimes/nested-dispatch.ts` with the typed request/result/metadata union and optional dependency hooks described above. Validate the request before model resolution, resolve the model per dispatch, build fresh child dependencies, call a nested `createAgent`, normalize the terminal output, validate it with the supplied contract, and retain trace metadata on every result path. Omit `checkpointer` from the child configuration.

3. **Parameterize model construction and centralize B6 usage collection.**

   Create `src/agent-runtimes/deus-native-model.ts` by moving the credential-proxy logic from `deus-native-backend.ts:175-211` without changing authentication or routing; add the `modelId` parameter and parent default constant. Create `src/agent-runtimes/deus-native-usage.ts` by extracting the exact per-AI-message semantics from `deus-native-backend.ts:403-465` and make it return both local and turn aggregates.

4. **Build the parent-facing tool adapter.**

   Create `src/agent-runtimes/nested-dispatch-tool.ts`. Define the required tool-call shape, compile `outputContract.schema` with `z.fromJSONSchema()`, construct the child-only prompt, call the dispatcher, and return either the success envelope or an error-status `ToolMessage` tied to `ToolRuntime.toolCallId`. Ensure schema compilation and missing/invalid contract failures happen before `resolveModel` or `createAgent`.

5. **Wire nested dispatch into `DeusNativeRuntime.runTurn()`.**

   Modify `src/agent-runtimes/deus-native-backend.ts` to resolve the middleware configuration/profile once, create the shared usage collector after `outgoingSessionId` is known, build the production dispatcher with per-dispatch model/tool/middleware factories, append `dispatch_nested_agent` only to the parent tool list, and process parent usage through the same collector. Keep the parent checkpointer and lifecycle hook unchanged; remove only the B8 non-goal text. Leave `DEUS_NATIVE_CAPABILITIES.handoffs` false.

6. **Add focused tests without replacing the oracle.**

   Add `src/agent-runtimes/nested-dispatch.test.ts` for pure validation, adapter, factory-freshness, and metadata behavior. Extend `src/agent-runtimes/deus-native-checkpointer-integration.test.ts` for end-to-end parent-session usage reconciliation and absence of child checkpoint/session state. Strengthen the independent oracle only if implementation exposes a real untested contract gap; never relax it.

7. **Record the architectural decision and entrypoint.**

   Add `docs/decisions/deus-v2-subagent-dispatch.md`, index it in `docs/decisions/INDEX.md`, cross-reference it from `docs/decisions/deus-v2-langchain-runtime.md`, and update the `AGENTS.md` core-entrypoint table. State explicitly that the new schema contract replaces status-marker parsing only for this in-process primitive and that `src/multi-agent/orchestrator.ts` continues unchanged.

## Testing Plan

### Contract Unit Tests

In `src/agent-runtimes/nested-dispatch.test.ts`, cover:

- A valid Zod-compatible object contract returns only its parsed output.
- JSON text is parsed before schema validation; malformed JSON and wrong primitive/field types return `contract_failure`.
- Missing required fields and `additionalProperties: false` are enforced after `z.fromJSONSchema()` compilation.
- An invalid/unsupported JSON Schema becomes a contract failure before `resolveModel` or child construction.
- Omitting `outputContract` from a dynamically malformed direct request fails before `resolveModel`, matching the oracle.
- Contract failure never has `status: 'success'`, never includes raw invalid output, and retains `agentId`/`model` metadata.
- Model resolution or child invocation errors use `subagent_execution_failed`, not the contract-failure code, and retain metadata.
- Success, contract failure, and execution failure all serialize through the tool adapter; failures produce `ToolMessage.status === 'error'` with the original `toolCallId`.
- Per-dispatch tool and middleware factories return different references across calls; the child receives no dispatch tool and no checkpointer.
- Two sequential dispatches call `resolveModel` separately with different ids and report the actual child model in metadata.
- Child usage metadata is returned only when reported; absent usage produces undefined event fields and no fabricated aggregate.

### Walking-Skeleton Oracle

Use the existing `src/agent-runtimes/nested-dispatch.oracle.test.ts` unchanged as the primary AC proof. It already:

1. Creates a real parent `createAgent` with a real LangChain tool call.
2. Calls `createNestedDispatcher({ resolveModel }).dispatch(...)` from that tool.
3. Captures a second real `createAgent` construction for the child.
4. Proves the child model differs from the parent and that model resolution occurs per dispatch.
5. Validates a successful payload against the dispatch's Zod-compatible contract.
6. Proves schema-invalid child output is never success and retains agent/model trace metadata.
7. Proves a missing contract fails before model selection.

Because `vitest.config.ts` includes `src/**/*.test.ts`, this exact oracle is part of plain `npm test`. The implementation is not complete until that default suite resolves `./nested-dispatch.js` and passes; a narrower command using a differently named oracle is not acceptable.

### Usage and Session Reconciliation

Extend `src/agent-runtimes/deus-native-checkpointer-integration.test.ts`, following its existing `withKnownUsage()` convention, with a scripted parent model that calls `dispatch_nested_agent` and a child model carrying different known usage values. Assert:

- Parent usage events name the parent model; child usage events name the independently selected child model.
- Every event uses the parent `RunResult.sessionRef.session_id` and provider `anthropic`.
- The tool result visible to the parent contains `agentId`, child model, provider, parent session id, and child-local usage.
- `RunResult.usage` equals the exact parent-plus-child totals.
- A child AI message without `usage_metadata` still emits undefined token fields and contributes no zero values.
- Only the parent thread exists in the real test checkpointer; child messages are absent from the persisted parent transcript except for the serialized tool result, and no child `RuntimeSession` or checkpoint thread is created.

### Regression and Verification Commands

Run the canonical oracle and focused suite first:

```bash
npm test -- \
  src/agent-runtimes/nested-dispatch.oracle.test.ts \
  src/agent-runtimes/nested-dispatch.test.ts \
  src/agent-runtimes/deus-native-checkpointer-integration.test.ts
```

Then run affected regressions and the full default glob:

```bash
npm test -- \
  src/agent-runtimes/deus-native-backend.test.ts \
  src/agent-runtimes/middleware-stack.test.ts \
  src/agent-runtimes/middleware-stack.oracle.test.ts \
  src/agent-runtimes/permission-rules.test.ts \
  src/agent-runtimes/permission-rules.oracle.test.ts \
  src/agent-runtimes/deus-native-tool-scope.oracle.test.ts
npm test
npm run typecheck
npm run build
npm run lint
git diff --check
```

## Risks and Open Questions

### Risks

- **MEDIUM: LangChain middleware and nested-agent behavior are comparatively immature.** A library upgrade could change middleware composition, tool-return handling, or nested graph behavior. Bound the risk by creating fresh middleware arrays through the public `buildMiddlewareStack()` API, using only public `createAgent`/`tool` types, omitting child checkpointing, and retaining a real-graph oracle with `FakeToolCallingModel`. Pin the verified declaration paths in the ADR and re-run the oracle on dependency upgrades.
- **`z.fromJSONSchema()` is explicitly semi-experimental.** Unsupported schema features or conversion changes could alter validation. Keep compilation in one Deus-owned adapter, catch conversion errors before child execution, independently safe-parse every returned value, and freeze supported behavior with valid/invalid schema tests. Do not write a second hand-rolled validator whose semantics can drift from the declared contract.
- **Model-visible JSON instructions are not enforcement.** A child may ignore the requested shape or emit malformed JSON. That is expected to produce a traceable `contract_failure`; no unvalidated value reaches the parent as success.
- **Usage can be double-counted if child messages leak into the parent scan.** Keep child graph state isolated, record child messages exactly once in the dispatcher callback, and assert exact totals plus persisted transcript shape in the integration test.
- **Arbitrary model ids can cause provider errors or unexpected cost.** The resolver still routes only through the existing Anthropic credential proxy, and failures retain requested agent/model metadata. A reviewed model allowlist or alias registry would be separate policy scope.

### Open Questions

- **Should the `read-only` permission profile allow `dispatch_nested_agent`?** This plan leaves it fail-closed because the meta-tool is not in B7's reviewed catalog. Allowing it requires an explicit permission-policy decision, even though the child currently receives only read-only web tools.
- **Should child model selection accept every Anthropic model id or only named aliases?** The AC requires independence but does not define a policy. The minimal implementation passes the requested id to the existing proxy-routed constructor; a human may require a bounded registry before release.
- **Does product language intend `handoffs` to include temporary delegation?** This plan interprets the capability as conversation/session ownership transfer and leaves it false. If the capability is redefined to mean any subagent call, that semantic change should update all backends consistently rather than only B8.
- **Should child agents receive parent lifecycle/persona context?** The ticket requires context isolation/orchestration ownership but does not define inherited context. This plan sends only the explicit task and output contract, while rebuilding the standard middleware stack; broader context inheritance needs an explicit privacy and token-budget decision.

None of these ambiguities requires silently expanding B8. If a human selects a different interpretation, update the new ADR and the corresponding tests before implementation diverges.

## Explicitly Out of Scope

- No modifications to `src/multi-agent/orchestrator.ts`, `src/multi-agent/types.ts`, its topological/parallel scheduling, container lifecycle, prompt templates, or `STATUS_MARKER_RE` parsing.
- No replacement or weakening of `ORACLE_BRIEF.md` or `src/agent-runtimes/nested-dispatch.oracle.test.ts`, and no competing self-authored oracle under another filename.
- No changes to `claude`, `openai`, or `llama-cpp`; B8 is `deus-native`-only.
- No widening of `SAFE_TOOL_NAMES`, host filesystem/shell access, child-selected tool sets, or new mutating tool path.
- No replay-safety/idempotency mechanism. The child receives only existing read-only web tools, so `docs/decisions/deus-v2-replay-safety.md`'s claim/complete contract is not triggered. Any future mutating child tool must satisfy that ADR first.
- No recursive child-of-child dispatch, dependency graph, parallel fan-out, cancellation protocol, depth limit, or budget scheduler. Children do not receive `dispatch_nested_agent`.
- No persistent child sessions, child checkpointer namespaces, resume semantics, or child session DB rows.
- No new provider router, model alias registry, pricing policy, or automatic fallback; only the model id varies within the existing Anthropic path.
- No new `RuntimeEvent`, `RunResult`, or `AgentRuntime` types, and no change to `handoffs` capability semantics.
- No automatic schema-repair loop or hidden retry policy. Contract failure is surfaced to the parent, which may choose whether to retry.
- No general persona, memory, or parent-transcript inheritance for children beyond the freshly constructed middleware stack and explicit child prompt.
