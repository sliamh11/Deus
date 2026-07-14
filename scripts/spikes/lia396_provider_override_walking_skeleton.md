# Spike: per-request provider overrides through LangChain's `wrapModelCall` middleware (LIA-396 / A3)

**Date:** 2026-07-14 · **Verdict: per-AC breakdown below (no blanket PASS)**
— selection and provider-attribution logging proven live and deterministic;
the subagent/Ollama leg is fully live with zero mocking; the main-agent/
Claude leg's live network execution is explicitly deferred to A4
(LIA-397, credential-proxy) since `ANTHROPIC_API_KEY` is absent here.

## Question

Can LangChain JS's real `wrapModelCall` middleware contract — the third
member of the middleware family after `createAgent` (A1) and `wrapToolCall`
(A2) — route different chat model providers to different requests **within
one logical session**? This is A3 of the Linear-tracked MA milestone
(LIA-394..400), continuing from A1 (LIA-394, PR #1031) and A2 (LIA-395, PR
#1032, both still open/unmerged).

LIA-396 lists five acceptance criteria:

1. `wrapModelCall` selects a Claude model for the main-agent request.
2. `wrapModelCall` selects a local Ollama model for a subagent request.
3. Both requests execute within one logical session.
4. Logs or test assertions identify which provider handled each request.
5. The proof can be reused by the A7 benchmark harness.

## Method

### Reuse target — unmodified

`wrapModelCall`'s real contract, confirmed by reading
`node_modules/langchain/dist/agents/middleware/types.d.ts`:
`createMiddleware({ name, wrapModelCall: async (request: ModelRequest,
handler: WrapModelCallHandler) => Promise<AIMessage | Command> })`, passed
to `createAgent({ middleware: [...] })`. The official JSDoc example shows
`request.model` being swapped and re-dispatched via
`handler({...request, model: fallbackModel})` — this spike's
`createProviderRoutingMiddleware` uses exactly that documented pattern, not
a novel mechanism.

### `createProviderRoutingMiddleware` — the reusable primitive

A real `wrapModelCall` middleware that reads `request.runtime.context.role`,
resolves a model instance via an injectable `resolveModel` Strategy, swaps
`request.model`, and records every selection to an inspectable `log:
ProviderRoutingRecord[]` (`requestIndex`, `role`, `providerClass`, `modelId`,
`executed`). `resolveModel`/`executeOverride` are Strategy-pattern seams —
interchangeable per-role algorithms, matching A2's `invokeGate`
injectable-seam precedent. `executeOverride` is an optional second Strategy
seam that, when it returns a message, short-circuits the real network call
for a given role — used ONLY to route around the disclosed
`ANTHROPIC_API_KEY` absence, never silently, and always recorded truthfully
in the log (`executed: false`).

### "One logical session" — one top-level `agent.invoke()`, one nested `agent.invoke()`

Modeled the deus-native two-tier architecture directly: a `delegate_to_
subagent` tool whose `execute()` constructs and invokes a *second*,
nested `createAgent`, sharing the *same* `wrapModelCall` middleware instance
as the outer agent, disambiguated by `request.runtime.context.role`
(`'main'` vs `'sub'`). This is call-stack/session continuity — both legs
occur within one real `mainAgent.invoke()` call tree, one shared middleware
instance, one shared routing log. Two independent top-level `.invoke()`
calls with no nesting would not satisfy this — there would be no shared
call stack and nothing forcing the two calls to be causally related within
one run.

### Provider selection — both real, live, in the same call stack

`defaultMainSubResolver()`: role `'main'` → a real `ChatAnthropic` instance
(placeholder API key when `ANTHROPIC_API_KEY` is absent — construction
throws without *some* key present, confirmed empirically); role `'sub'` →
a real `ChatOllama` instance pointed at local Ollama (`OLLAMA_URL`, reusing
the repo's existing env-var convention). Both providers expose the model id
on the same `.model` field, confirmed by construction — uniform
provider-attribution logging with no per-provider branching.

### Driving the loop without a Claude credential

`makeScriptedMainStub()`: a *stateful*, call-count-aware `executeOverride`
for role `'main'` only — its first invocation returns a `tool_calls`-bearing
`AIMessage` naming `delegate_to_subagent`, every subsequent invocation
returns a plain final `AIMessage`. A stateless single-canned-response stub
would never actually invoke the tool, so the `'sub'` leg would never fire —
this was caught and fixed during planning, before implementation. The
`'sub'`/Ollama leg has no override — it always hits the real local Ollama
server for real.

## What was built

- `scripts/spikes/lia396_provider_override_walking_skeleton.ts` —
  `createProviderRoutingMiddleware` (the reusable primitive),
  `defaultMainSubResolver`, `makeScriptedMainStub`,
  `makeDelegateToSubagentTool`, and a `main()` direct-execution demo.
- `scripts/spikes/lia396_provider_override_walking_skeleton.test.ts` — 7
  tests (6 passing, 1 conditionally skipped):
  - Selection: `'main'` role resolves a real `ChatAnthropic` instance with
    the right model id; `'sub'` role resolves a real `ChatOllama` instance;
    an unrecognized role passes the original `request.model` through
    unchanged.
  - Routing log: entries recorded in order with correct
    `requestIndex`/`role`/`providerClass`/`modelId`; `executed: false` on
    the `executeOverride` short-circuit path vs `executed: true` on the
    default real-handler path.
  - **The integration test** — one real `mainAgent.invoke()` call, real
    nested `subAgent.invoke()`, real Ollama network call: asserts exactly
    one `'sub'` log entry (`ChatOllama`, `executed: true`), at least two
    `'main'` entries (`ChatAnthropic`, `executed: false`) straddling the
    `'sub'` entry in `requestIndex` order (the live `main → sub → main`
    trace), and that the subagent's actual returned content is real
    (non-empty, not the canned stub string) — proving the Ollama round-trip
    genuinely happened, not just that the mechanism fired.
  - Optional live full-Claude smoke test, gated on `ANTHROPIC_API_KEY`
    presence (same disposition as A1) — skipped in this environment.
- `vitest.config.ts` — same `scripts/spikes/**/*.test.ts` glob addition
  A1/A2 made (independently, since neither is merged yet).
- Direct-execution demo output (`npx tsx
  scripts/spikes/lia396_provider_override_walking_skeleton.ts`), matching
  the automated assertions exactly:
  ```
  ── Transcript ──
  [human] Delegate to your subagent to find out its favorite fruit, then report back.
  [ai:model]
  [tool:delegate_to_subagent] {"subagent_answer":"Apple"}
  [ai:model] Delegated to the subagent and got an answer back.

  ── Routing log ──
  #0 role=main provider=ChatAnthropic model=claude-opus-4-8 executed=false
  #1 role=sub provider=ChatOllama model=gemma4:e2b executed=true
  #2 role=main provider=ChatAnthropic model=claude-opus-4-8 executed=false
  ```

## Verdict — per acceptance criterion

- **AC1** "wrapModelCall selects a Claude model for the main-agent request"
  — **PASS**. A real `ChatAnthropic` instance is genuinely constructed and
  assigned to `request.model` for the `'main'` role, live, in the same
  session run as the `'sub'` leg.
- **AC2** "wrapModelCall selects a local Ollama model for a subagent
  request" — **PASS**. A real `ChatOllama` instance is genuinely
  constructed, assigned, AND executed against local Ollama
  (`gemma4:e2b`) — zero mocking on this leg.
- **AC3** "Both requests execute within one logical session" — **PARTIAL**.
  Session/call-stack nesting: yes — one top-level `agent.invoke()`, one
  nested `agent.invoke()` inside `delegate_to_subagent`, sharing one
  middleware instance and one routing log, live-verified `main → sub →
  main` trace. Live network *execution* of both legs: no — the `'main'`/
  Claude leg is stubbed via the disclosed `executeOverride` seam because
  `ANTHROPIC_API_KEY` is absent; only the `'sub'`/Ollama leg genuinely
  executes over the network. Full literal AC3 satisfaction (both legs
  actually executing) is A4's job (LIA-397, credential-proxy), not A3's —
  stated explicitly here rather than folded into a blanket pass.
- **AC4** "Logs or test assertions identify which provider handled each
  request" — **PASS**. `ProviderRoutingRecord[]` entries carry
  `role`/`providerClass`/`modelId`/`executed` per request.
- **AC5** "The proof can be reused by the A7 benchmark harness" —
  **PASS**. `createProviderRoutingMiddleware` is a standalone, parameterized
  export with no hardcoded `'main'`/`'sub'` coupling — that logic lives only
  in the separately-exported, explicitly-example-labeled
  `defaultMainSubResolver()`. A7 supplies its own resolver per benchmark
  matrix cell.

## Scope of this spike

Read-only proof-of-concept; nothing wired into production. No modifications
to any reused mechanism. Not merged to `main` — pushed as a PR for review
only. When A4 (LIA-397) lands, the same `createProviderRoutingMiddleware`
runs with no `executeOverride` on the `'main'` leg and a real Anthropic call
happens live — nothing about this design needs to change for that
transition; `executeOverride` simply becomes unused for that role.
