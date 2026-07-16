# Deus v2: LangChain-Based Host-Side Agent Runtime (deus-native)

**Status:** Accepted
**Date:** 2026-07-15
**Scope:** `src/agent-runtimes/deus-native-backend.ts`, `src/agent-runtimes/tool-broker-langchain-adapter.ts`, `src/agent-runtimes/types.ts`, `src/index.ts`, `package.json`
**Related:**

- `multi-agent-orchestration-research.md` — evaluated Runner/LLM placement options in May 2026 and rejected "Runner on host, LLM on host". This ADR partially supersedes that rejection (see Consequences).
- `backend-neutral-agent-runtime.md` — defines the `AgentRuntime` contract, credential boundary ("adapters must not read raw host secrets"), and context-parity requirement this adapter implements against. Not superseded.

## Context

Deus's agent execution has been backend-neutral since `backend-neutral-agent-runtime.md`
(2026-04-23), but every registered backend (`claude`, `openai`, `llama-cpp`) is a thin
wrapper around the same `ContainerRuntime`: the agentic loop itself runs inside an
isolated container, driven by the Claude Agent SDK (or the OpenAI/llama.cpp drivers)
rather than by Deus-owned code.

The deus-v2 decision (2026-07-13, tracked as the 49-issue roadmap in Linear project
db126dc4; this file is its first repo-committed record) is to retire that dependence:
Deus owns its own agentic loop, built on LangChain JS `createAgent` plus a Deus-owned
middleware stack, running **in-process on the host**. The A-milestone walking skeletons
(LIA-394 through LIA-400) validated the load-bearing assumptions one by one:
tool-broker reuse through a pure LangChain adapter (A1), provider override (A3),
credential-proxy routing with placeholder credentials for both OAuth and API-key
modes (A4), MCP adapters (A5), SQLite checkpointing (A6), and tool-loop reliability
(A7). B1 (LIA-401) turns that validated shape into the first production-registered
`AgentRuntime` adapter, selectable via `DEUS_AGENT_BACKEND=deus-native`.

This directly conflicts with `multi-agent-orchestration-research.md` (Accepted,
2026-05-06), which evaluated three Runner/LLM placement options and rejected
"Runner on host, LLM on host" with the reasoning: "Containers become tool sandboxes.
Loses Claude Agent SDK autonomous loop. Massive rewrite, loses core strength."
A1's spike write-up explicitly flagged that a production host-side proposal requires
its own ADR superseding or reconciling with that rejection. This is that ADR.

## Decision

1. **Adopt LangChain JS `createAgent` as the engine of a new host-side
   `AgentRuntime` adapter, `deus-native`**, registered in the existing runtime
   registry alongside `claude`/`openai`/`llama-cpp` and selected through the
   existing `DEUS_AGENT_BACKEND` mechanism. This is a Strategy-pattern addition:
   no new abstraction is introduced; the `AgentRuntime` interface Deus already
   owns is reused as-is.
2. **The May 2026 rejection's primary reason is moot by design.** "Loses the
   Claude Agent SDK autonomous loop" was a cost when the SDK loop was the core
   strength; deus-v2's entire premise is to replace that loop with a Deus-owned
   one. The rejection is reversed for this specific case — see Consequences.
3. **The rejection's implicit second reason — losing container-grade
   tool-execution isolation — is still true and is NOT mooted.** The tool broker's
   `bash_exec` spawns `/bin/bash -lc` with the full inherited environment and no
   resource limits, and `resolveWorkspacePath` hardcodes container-only
   `/workspace/*` roots that were never designed as a host boundary. No OS-level
   sandboxing primitive exists in this repo. Therefore `deus-native` ships with a
   **conservative tool surface**: only `web_search`/`web_fetch` (the two broker
   tools that neither spawn shells nor touch `resolveWorkspacePath`), with
   `web_fetch` behind a code-level host allowlist. `bash_exec`/`read_file`/
   `write_file`/`edit_file`/`glob_files`/`grep_files` are explicitly not wired
   until the permission-rules engine (B7/LIA-407, `wrapToolCall`) or an
   equivalent stopgap exists. `capabilities()` reports `shell: false,
filesystem: false` accordingly. **`web_search` is NOT host-allowlisted**
   (added per ai-eng-warden review, round 1): its destination is fixed
   (DuckDuckGo), so it's exempt from `withHostAllowlist`, but its query
   string is model-controlled and sent verbatim — it is the one always-on
   network-egress channel this adapter exposes, and a hijacked turn could use
   it to leak text to a third party regardless of the `web_fetch` allowlist
   setting. Accepted for this milestone given the tool's fixed destination
   and the prompt-injection boundary added to all tool output (below); a
   query-length cap or content scrub would need its own review if adopted.
   All broker-tool output re-entering the model's context is wrapped in an
   explicit `<tool-output>` boundary with an "untrusted data, not
   instructions" framing (`tool-broker-langchain-adapter.ts`), since
   `runTurn` sends no system prompt (see Decision 5) and the model would
   otherwise have no signal distinguishing fetched/searched content from a
   command.
4. **Credential boundary is preserved.** The adapter holds no raw secrets: it
   targets the live credential proxy at `PROXY_BIND_HOST:CREDENTIAL_PROXY_PORT`
   with placeholder credentials (branching on `detectAuthMode()` for OAuth vs
   API-key mode) and a per-group `x-deus-proxy-token`, exactly as the container
   backends do — satisfying `backend-neutral-agent-runtime.md`'s "must not read
   raw host secrets" requirement.
5. **Context parity is deliberately deferred.** B1's `runTurn` sends the bare
   prompt only — it does not load `CLAUDE.md`/`AGENTS.md`/
   `AI_AGENT_GUIDELINES.md`/persona context. That is a stated gap against
   `backend-neutral-agent-runtime.md`'s parity requirement, owned by B2
   (middleware stack) or B3 (lifecycle events); `deus-native` is not
   production-ready for real user traffic until it lands.
6. **publicIngress (webhook-originated) groups are refused, fail-closed**
   (added per code-review). `container-runner.ts`'s `buildContainerArgs`
   already refuses to launch a publicIngress group on any backend but
   `claude`, since the reduced-privilege curated-tool profile it enforces is
   claude/container-only — its own comment states "refuse to launch rather
   than silently downgrade isolation." `deus-native` never calls that
   function (it is not a `ContainerRuntime` wrapper), so without an
   independent check a publicIngress group routed to `deus-native` would run
   with an unscoped proxy token and the full `web_search`/`web_fetch`
   surface instead of the curated webhook profile — the exact silent
   downgrade the container-path guard exists to prevent. `DeusNativeRuntime.
runTurn` mirrors the same rule directly (returns a `status: 'error'`
   result before minting any token or constructing a model), matching the
   per-backend enforcement pattern `buildContainerArgs` already uses rather
   than introducing a new centralized mechanism.

## Alternatives Considered

- **Keep the container-based Claude Agent SDK loop as the permanent engine
  (status quo).** Rejected by the deus-v2 decision itself: it leaves Deus's core
  agentic behavior owned by a vendor harness, limits middleware/hook control,
  and makes the loop unswappable.
- **Runner on host, LLM in container (bridged).** Already rejected in
  `multi-agent-orchestration-research.md` for impedance mismatch — the runner
  cannot orchestrate individual turns inside a container. Still rejected; the
  deus-v2 pivot does not change this.
- **Run the LangChain loop inside the container.** Preserves the sandbox but
  re-creates the exact problem deus-v2 retires: the host cannot own middleware,
  hooks, checkpointing, or per-turn control across the container boundary
  without rebuilding an IPC protocol equivalent to the one being retired.
  Rejected for B1; per-tool sandboxing (B7+) is the chosen isolation path
  instead.
- **Ship B1 with the full tool-broker surface and rely on
  `DEUS_AGENT_BACKEND` opt-in as the safety gate.** Rejected by threat-model
  review: backend selection is a soft, global `.env`-flippable default with no
  per-call gate; it is defense-in-depth, not a sandbox. The conservative
  web-only tool surface is the actual boundary.

## Consequences

- **This ADR supersedes the "Runner on host, LLM on host: Rejected" line of
  `multi-agent-orchestration-research.md` (its Architectural Finding table,
  line 37) for the deus-native case.** The rejection's stated reason — losing
  the Claude Agent SDK's autonomous loop — no longer applies, because deus-v2
  deliberately retires that loop in favor of a Deus-owned LangChain loop. The
  rest of that ADR (interface renames, orchestrator patterns 1–2, guardrail/
  tracing concepts, the other two placement rejections) stands unchanged. The
  independent tool-isolation concern embedded in that rejection is addressed
  separately by this ADR's conservative tool scope, not by the supersession.
- The container backends (`claude`/`openai`/`llama-cpp`) remain registered,
  default, and unchanged. `deus-native` is reachable only by explicit
  `DEUS_AGENT_BACKEND=deus-native` (or group/task override).
- `langchain`, `@langchain/core`, `@langchain/anthropic`, and
  `@anthropic-ai/sdk` move from `devDependencies` to `dependencies`: the
  adapter is imported unconditionally at `src/index.ts` startup, so a
  production install that omits dev dependencies would otherwise crash on boot
  even when `deus-native` is never selected. The remaining `@langchain/*`
  spike-only packages stay in `devDependencies` until a production import
  exists.
- Host-side tool execution grows one production surface (web fetch/search) with
  no container between the model and the host. The allowlist decorator and the
  web-only inclusion filter are the security boundary; widening the tool
  surface requires B7's permission engine (or equivalent) first, plus its own
  review.
- **Update (B7/LIA-407):** the permission engine referenced above has since
  landed — see `deus-v2-permission-rules.md` for the declarative allow/deny
  rule evaluator and its `default`/`read-only` named profiles. `SAFE_TOOL_NAMES`
  (`tool-broker-langchain-adapter.ts:124`) is unchanged at `{web_search,
web_fetch}` and remains the operative boundary; B7 added an authorization
  layer behind that boundary, it did not widen it. Any future widening still
  requires its own separate review per this ADR's Decision 3.
- Session persistence (`persistent_sessions: false`) and Deus context loading
  are deferred to B4 and B2/B3 respectively — `deus-native` is a roadmap
  vehicle, not yet a parity backend.
- **Update (D1/LIA-415):** the middleware stack's memory slot is now filled
  for CONTROL-GROUP turns: `buildMemoryMiddleware` performs one `beforeModel`
  retrieval per turn through a narrow subprocess adapter
  (`src/agent-runtimes/memory-retrieval.ts`) over the byte-for-byte unchanged
  `scripts/memory_retrieval_hook.py`, appending non-empty recalled context to
  the model input as a user-role message and failing open (no context, turn
  proceeds) on empty/malformed output, timeout, or process failure.
  Non-control groups deliberately keep the pass-through observer — the hook
  reads the user's personal vault and is not group-scoped; that parity gap is
  tracked as `AAG-014` in `docs/agent-agnostic-debt.md`. This fills the
  existing memory slot only: it does not reopen this ADR's runtime choice and
  claims no broader hook-pipeline parity (the general host `HookPipeline`
  remains unimplemented per `hook-dispatch-facade-correction.md`).
- **Update (B8/LIA-408):** nested subagent dispatch has since landed — see
  `deus-v2-subagent-dispatch.md` for the in-process, one-shot nested
  `createAgent` dispatch primitive and its `dispatch_nested_agent` tool. It
  is governed as an EXTENSION of this ADR's `runTurn()` loop, not a
  parallel agent-execution path: a dispatched child receives a fresh
  instance of the SAME conservative web-only tool surface this ADR
  establishes (Decision 3), the same credential-proxy routing (Decision 4),
  and never receives `dispatch_nested_agent` itself (no recursive nesting).
  `SAFE_TOOL_NAMES` remains unchanged and is still the operative tool-surface
  boundary for every child, exactly as it is for the parent.
- **Update (D3/LIA-417):** that same memory middleware now also contains a
  post-success `wrapToolCall` mechanism for edit-triggered re-embedding. It
  maps supported broker-shaped (`write_file`/`edit_file`) and existing-hook-
  shaped (`Write`/`Edit`/`MultiEdit`) calls onto the unchanged
  `scripts/memory_tree_hook.py` PostToolUse protocol, invokes the hook only
  after the delegated tool succeeds, and fails open if hook launch fails.
  This mechanism is unit-complete but currently dormant: `SAFE_TOOL_NAMES`
  still exposes only `web_search` and `web_fetch`, so no live `deus-native`
  tool can activate it. If a separately reviewed future decision widens that
  inclusion set to supported filesystem edit tools, the middleware branch
  becomes live automatically; LIA-417 neither authorizes nor implements that
  widening. The `claude` backend is unchanged and continues to re-embed
  independently through its existing `Write|Edit|MultiEdit` PostToolUse hook.

## Rollback

Remove the `registry.register(createDeusNativeRuntime(...))` line in
`src/index.ts` and the `'deus-native'` member from `AgentRuntimeId`/
`parseAgentBackend`; the adapter module and its tests become dead code that can
be deleted in the same commit. No schema, data, or container changes are
involved — the sessions table's generic `backend` column simply stops seeing
the value. Dependency moves may stay (harmless) or be reverted with the code.
If rolled back, restore `multi-agent-orchestration-research.md`'s banner note
removal and INDEX.md rows in the same commit so the decision record stays
consistent.
