/**
 * `deus-native` AgentRuntime adapter (LIA-401 / B1).
 *
 * Unlike `claude`/`openai`/`llama-cpp` (thin `ContainerRuntime` wrappers that
 * spawn an isolated container and talk over IPC — see `container-backend.ts`),
 * `deus-native` is a standalone `AgentRuntime` implementation: it runs
 * LangChain's `createAgent` IN-PROCESS on the host. This is the first
 * production-registered, host-side, zero-container-sandboxed LLM execution
 * path in this repo — see docs/decisions/deus-v2-langchain-runtime.md for the
 * architectural decision and its threat-model reasoning.
 *
 * Non-goals of this adapter, intentionally deferred to later roadmap items
 * (do not add these here):
 * - Loading AGENTS.md/AI_AGENT_GUIDELINES.md/persona context and the full
 *   context-registry.ts parity (claudeSystemAppend/skipForControlGroup/
 *   projectOnly flags, EXTRA RULES dirs, VAULT: entries) — B3/LIA-403 lands
 *   ONLY group-scoped CLAUDE.md session-open injection (see
 *   lifecycle-events.ts), deliberately minimal; broader context parity is
 *   out of that ticket's literal ACs and can be a follow-up.
 * - Real backend-scoped session persistence via the LangGraph checkpointer
 *   lands here (B4/LIA-404): the MESSAGE exchange itself genuinely persists
 *   and resumes across turns. Still deferred: session-open CLAUDE.md content
 *   injected via `wrapModelCall`'s `systemMessage` remains a per-call,
 *   EPHEMERAL override — it is never written into the checkpointed
 *   `messages` state (confirmed: `AgentNode`'s `baseHandler` passes it
 *   directly to the model call and only ever writes the model's OWN response
 *   back into graph state) — so B3's documented once-per-session-lifecycle
 *   injection limitation is NOT fixed by this plan, contrary to an earlier
 *   draft's incorrect claim; re-supplying system context on every resumed
 *   turn (or persisting it into message state some other way) is real,
 *   separate future work, deliberately out of this ticket's scope. Also
 *   still deferred: `startOrResume` real-lookup parity for
 *   `multi-agent/orchestrator.ts`'s one-shot task path (correctly out of
 *   scope — that caller mints a fresh one-shot task per call by design) and
 *   any cleanup/expiry of accumulated checkpoint rows (unbounded growth, a
 *   separate future concern).
 * - Context windowing/summarization for the checkpointed message history
 *   (flagged by ai-eng-warden review). `agent.invoke()` loads the FULL
 *   accumulated `messages` channel on every resumed turn and sends it to the
 *   model as-is — per-turn token cost grows linearly, unbounded, for the
 *   life of a session. No cap, truncation, or summarization exists yet.
 *   Deliberately out of this ticket's scope; a real future concern once
 *   long-lived sessions are common.
 * - Any tool beyond web_search/web_fetch. B7/LIA-407 landed the wrapToolCall
 *   permission-rules engine (permission-rules.ts + middleware-stack.ts's
 *   real permissions layer, profile-selected via
 *   backendConfig.permissionProfile below), but it is an AUTHORIZATION layer
 *   only — SAFE_TOOL_NAMES is unchanged and widening the live tool surface
 *   still requires its own isolation review plus the replay-safety contract
 *   (docs/decisions/deus-v2-replay-safety.md).
 * - Middleware layer SUBSTANCE beyond permissions, memory, and wardens. The
 *   ordered/configurable middleware stack itself landed with B2/LIA-402, B7
 *   made the permissions layer real, D1/LIA-415 made the memory layer real
 *   for CONTROL-GROUP turns (one beforeModel retrieval per turn through the
 *   unchanged scripts/memory_retrieval_hook.py — non-control groups keep
 *   the pass-through observer on group-scoping safety grounds, tracked as
 *   AAG-014), and C1/LIA-409 made the wardens layer real (the unchanged
 *   `scripts/codex_warden_hooks.py` plan-review/code-review/ai-eng/
 *   verification gates now run over the deus-native `wrapToolCall` path).
 *   Telemetry remains the sole explicit observe-only placeholder (-> real
 *   usage accounting).
 * - Replay-safety auditing (B5/LIA-405), token/usage accounting events
 *   (B6/LIA-406).
 * - Nested subagent dispatch (B8/LIA-408) is now real: `runTurn()` builds a
 *   `dispatch_nested_agent` tool (`nested-dispatch-tool.ts`) so the parent
 *   agent can delegate a self-contained, contract-validated subtask to a
 *   one-shot, in-process child `createAgent` with its own independently
 *   selected model. This is NOT `src/multi-agent/orchestrator.ts`'s
 *   container-based `SubagentTask[]` scheduler — that primitive is
 *   unchanged; see docs/decisions/deus-v2-subagent-dispatch.md for how the
 *   two coexist.
 * - Consuming the middleware stack's inspectable `logs` output (added per
 *   ai-eng-warden review). `buildMiddlewareStack(...).logs` is discarded
 *   here (only `middleware` is destructured) -- each layer's log becomes a
 *   real observability/debug sink alongside whichever future item lands
 *   that layer's substance (B7 for permissions, telemetry's own usage-
 *   accounting work for that layer, etc.), not as part of B2 itself.
 */

import crypto from 'crypto';
import path from 'node:path';

import { createAgent, type AgentMiddleware } from 'langchain';

import type {
  AgentRuntime,
  RuntimeCapabilities,
  RuntimeSession,
  RunContext,
  RunResult,
  RuntimeEventSink,
} from './types.js';
import { defaultSession } from './types.js';
import type { ContainerRuntimeDeps } from './container-backend.js';
import {
  buildSafeTools,
  type ToolBrokerContext,
} from './tool-broker-langchain-adapter.js';
import {
  buildMiddlewareStack,
  resolveMiddlewareStackConfig,
} from './middleware-stack.js';
import {
  buildPromptLifecycleHook,
  loadSessionOpenContext,
  type PromptEventRecord,
} from './lifecycle-events.js';
import { getCheckpointer } from './checkpointer.js';
import {
  buildProxyRoutedChatAnthropic,
  PARENT_DEFAULT_MODEL,
} from './deus-native-model.js';
import { createTurnUsageCollector } from './deus-native-usage.js';
import { buildNestedDispatchTool } from './nested-dispatch-tool.js';

// capabilities() — each flag has an inline rationale, matching
// llama-cpp-backend.ts's existing comment convention.
const DEUS_NATIVE_CAPABILITIES: RuntimeCapabilities = {
  // Deliberately false: the tool-broker's bash_exec spawns `/bin/bash -lc`
  // with the full inherited environment and no namespace isolation — safe
  // today only because a CONTAINER is the sandbox boundary. deus-native runs
  // in-process on the host with no container between the model and the
  // machine. B7/LIA-407's permission-rules engine now exists, but it is an
  // application-level AUTHORIZATION layer, not a sandbox — wiring shell
  // execution still requires real isolation review plus the replay-safety
  // contract, so this stays false.
  shell: false,
  // Deliberately false: resolveWorkspacePath's `/workspace/*` allowlist was
  // designed and tested as a CONTAINER boundary, not a host boundary — it
  // "protects" the host only by the accident that /workspace/* doesn't exist
  // there. Filesystem tools stay unwired for the same reason as shell above.
  filesystem: false,
  // True: web_search/web_fetch are the only tools wired in B1 (the proven-
  // safe subset A1's spike validated — neither spawns a shell nor touches
  // resolveWorkspacePath), with web_fetch host-allowlisted.
  web: true,
  // False: no image input handling in this PR's runTurn.
  multimodal: false,
  // False: same parity gap as every other backend today.
  handoffs: false,
  // True (B4/LIA-404): runTurn wires the LangGraph SqliteSaver checkpointer
  // (checkpointer.ts) with RuntimeSession.session_id as the thread_id, so a
  // stored session id IS a real, resumable message-thread identifier.
  persistent_sessions: true,
  // False: runTurn returns one buffered result, no incremental deltas.
  tool_streaming: false,
};

/** Web-fetch host allowlist. Empty by default: opt-in via env, deny-by-default. */
function resolveAllowedWebFetchHosts(): string[] {
  const raw = process.env.DEUS_NATIVE_WEB_FETCH_ALLOWED_HOSTS;
  if (!raw) return [];
  return raw
    .split(',')
    .map((h) => h.trim())
    .filter((h) => h.length > 0);
}

function buildToolBrokerContext(runContext: RunContext): ToolBrokerContext {
  // web_search and web_fetch never touch ctx.containerInput beyond identity
  // fields (confirmed by reading executeBrokerTool's web_search/web_fetch
  // case bodies in container/agent-runner/src/tool-broker.ts) — this context
  // is sufficient for the only two tools buildSafeTools ever returns.
  return {
    cwd: runContext.cwd ?? process.cwd(),
    containerInput: {
      groupFolder: runContext.groupFolder,
      chatJid: runContext.chatJid,
      isControlGroup: runContext.isControlGroup,
    },
  };
}

export class DeusNativeRuntime implements AgentRuntime {
  constructor(private deps: ContainerRuntimeDeps) {}

  name(): 'deus-native' {
    return 'deus-native';
  }

  capabilities(): RuntimeCapabilities {
    return DEUS_NATIVE_CAPABILITIES;
  }

  async startOrResume(_runContext: RunContext): Promise<RuntimeSession> {
    // Mirrors ContainerRuntime.startOrResume's existing stub pattern exactly
    // (container-backend.ts:46-48) — real session lookup happens at the
    // orchestrator layer (db.getSession) before the runtime is invoked, not
    // inside the adapter. Real persistence integration is B4's job
    // (LIA-404); this must not invent a parallel mechanism that would
    // conflict with it.
    return defaultSession('', 'deus-native');
  }

  async runTurn(
    runContext: RunContext,
    sessionRef: RuntimeSession,
    eventSink: RuntimeEventSink,
  ): Promise<RunResult> {
    try {
      // publicIngress fail-closed guard (added after code-review: a real gap
      // the earlier drafts missed). container-runner.ts's buildContainerArgs
      // refuses to launch a publicIngress (webhook-originated) group on any
      // backend but 'claude', since the reduced-privilege curated-tool
      // profile is enforced only on that path -- "refuse to launch rather
      // than silently downgrade isolation" (its own comment). deus-native
      // never calls buildContainerArgs (it's not a ContainerRuntime wrapper),
      // so without this check a publicIngress group routed to deus-native
      // would run with an UNSCOPED proxy token and the full web_search/
      // web_fetch tool surface instead of the curated webhook profile --
      // the exact silent downgrade that guard exists to prevent. Mirror it
      // here rather than centralizing, matching the per-backend pattern
      // buildContainerArgs itself already uses.
      const group = this.deps.resolveGroup(runContext.groupFolder);
      if (group?.containerConfig?.publicIngress === true) {
        return {
          status: 'error',
          result: null,
          error:
            `publicIngress group "${group.folder}" requires the 'claude' backend ` +
            `(reduced-privilege profile is claude-only); refusing to run on 'deus-native'`,
        };
      }

      // B4 (LIA-404): the session_id doubles as the LangGraph checkpointer
      // thread_id, so it's computed FIRST — needed before invoke() runs, not
      // just as the RunResult.sessionRef output after. Same echo-if-present/
      // mint-if-empty logic B3 established (keeps db.setSession's dedup on
      // "same id = touch, don't insert" to one row per real session-open),
      // unchanged, just computed earlier.
      const outgoingSessionId =
        sessionRef.session_id !== ''
          ? sessionRef.session_id
          : crypto.randomUUID();

      // B4 (LIA-404): read the real checkpoint state ONCE, and derive BOTH
      // the injection-gating signal and the tool-call-scoping count from it.
      // This read happens before invoke()'s own internal getTuple() in the
      // same process — both read the identical "before this turn" state.
      const priorTuple = await getCheckpointer().getTuple({
        configurable: { thread_id: outgoingSessionId },
      });
      const priorMessages = Array.isArray(
        (priorTuple?.checkpoint?.channel_values as { messages?: unknown[] })
          ?.messages,
      )
        ? (priorTuple!.checkpoint.channel_values as { messages: unknown[] })
            .messages
        : [];
      // isNewSession = "does a REAL checkpoint exist for this thread_id" —
      // deliberately NOT B3's `sessionRef.session_id === ''` string check.
      // B3-era production rows carry non-empty, UUID-shaped session_ids that
      // were NEVER real checkpointer threads (B3's own marker was explicitly
      // "not a real resumable checkpoint ID"); under the old string signal,
      // the first post-B4 turn for such a row would misclassify as "resumed",
      // skip session-open CLAUDE.md injection, AND hand the checkpointer a
      // thread_id it has never seen — a fresh, memoryless context with no
      // group rules either. Under this checkpoint-existence signal the same
      // pre-existing id is still echoed back (preserving the DB row's
      // identity) AND correctly triggers re-injection, because from the
      // checkpointer's perspective it truthfully IS a new session. This one
      // signal is the entire upgrade-path fix — no migration, no backfill.
      const isNewSession = priorTuple === undefined;
      const priorMessageCount = priorMessages.length;

      // Session-open injection fires only on a genuinely NEW session (once
      // per open lifecycle — a resumed turn never even calls
      // loadSessionOpenContext) AND only when session-open content actually
      // exists (systemMessage stays undefined otherwise). Awaited (LIA-416/
      // D2): the loader now composes vault context whose recent-sessions
      // provider is an async subprocess — the await occurs ONLY on this
      // new-session branch, bounded by the vault pipeline's 5s subprocess
      // timeout, and never blocks the process-wide event loop. The returned
      // SessionOpenRecord is an inspectable log consumed by tests, matching
      // B2's own discarded-logs precedent here.
      const sessionOpenMessage = isNewSession
        ? (await loadSessionOpenContext(runContext)).systemMessage
        : undefined;

      const model = buildProxyRoutedChatAnthropic(
        runContext,
        PARENT_DEFAULT_MODEL,
      );
      const toolCtx = buildToolBrokerContext(runContext);

      // B2 (LIA-402): ordered, per-layer-toggleable middleware stack —
      // permissions -> wardens -> memory -> telemetry (index 0 outermost).
      // B7 (LIA-407): the permissions layer is REAL — a declarative
      // first-match-wins rule engine (permission-rules.ts) selected by the
      // named profile below. D1 (LIA-415): the memory layer is REAL for
      // control-group turns (see memoryRequest below). C1 (LIA-409): the
      // wardens layer is REAL — it invokes the unchanged
      // `scripts/codex_warden_hooks.py` gate runners over the `wardenCwd`
      // resolved below. Telemetry remains the sole observe-only placeholder
      // (see middleware-stack.ts for its caveat).
      //
      // Profile selection: runContext.backendConfig.permissionProfile.
      // Omitted => 'default' (allow-all — today's behavior, unchanged);
      // 'read-only' => the fail-closed read-only preset. Any other value
      // (unknown name, non-string) THROWS here — before createAgent — and
      // surfaces as this turn's normal status:'error' RunResult rather than
      // silently weakening the requested restriction.
      //
      // B8 (LIA-408): resolved ONCE here (not re-derived further down)
      // because both the parent's own middleware stack AND every
      // nested-dispatch child's fresh middleware stack (rebuilt per-dispatch
      // by buildNestedDispatchTool's `buildChildMiddleware`) select the SAME
      // named profile — a child never runs under a looser policy than its
      // parent. C1 (LIA-409): `wardenCwd` below is resolved once for the
      // same reason and threaded into both call sites — a nested-dispatch
      // child's wardens gate must resolve the same worktree/repo-root as
      // the parent's, never silently default to `process.cwd()`.
      const rawPermissionProfile =
        runContext.backendConfig?.['permissionProfile'];
      if (
        rawPermissionProfile !== undefined &&
        typeof rawPermissionProfile !== 'string'
      ) {
        throw new Error(
          `deus-native: backendConfig.permissionProfile must be a string ` +
            `profile name, got ${typeof rawPermissionProfile}`,
        );
      }
      // Warden event cwd: `worktreePath` wins because autonomous pipeline
      // runs already identify their checked-out worktree explicitly; `cwd`
      // is the normal group/project fallback; `process.cwd()` preserves the
      // existing `buildToolBrokerContext` fallback when neither optional
      // field is present. Always made absolute so the serialized PreToolUse
      // event's `cwd` and the resolved warden repo root are unambiguous
      // regardless of what relative path a caller supplied.
      const wardenCwd = path.resolve(
        runContext.worktreePath ?? runContext.cwd ?? process.cwd(),
      );
      const { middleware } = buildMiddlewareStack(
        resolveMiddlewareStackConfig(),
        {
          permissionProfile: rawPermissionProfile,
          wardenCwd,
          // D1 (LIA-415): the memory layer's retrieval input — the submitted
          // prompt plus the backend-scoped session id (computed above; it
          // drives the hook's session-concept expansion and injection
          // dedup). CONTROL-GROUP TURNS ONLY: memory_retrieval_hook.py reads
          // the user's PERSONAL vault and is not group-scoped, so supplying
          // it for arbitrary groups would leak personal context across
          // unrelated conversations — exactly the deferral reason the old
          // placeholder documented. Non-control groups keep the layer as a
          // pass-through observer (parity gap tracked as AAG-014 in
          // docs/agent-agnostic-debt.md).
          ...(runContext.isControlGroup
            ? {
                memoryRequest: {
                  prompt: runContext.prompt,
                  sessionId: outgoingSessionId,
                },
              }
            : {}),
        },
      );

      // B8 (LIA-408): turn-scoped usage collector, created once
      // outgoingSessionId is known and shared by the parent's own
      // per-AIMessage usage recording below AND every nested-dispatch
      // child's `onUsage` callback — one code path builds every 'usage'
      // event and the combined RunResult.usage aggregate, whether the
      // AIMessage came from the parent or from a dispatched child.
      const usageCollector = createTurnUsageCollector({
        sessionId: outgoingSessionId,
        eventSink,
      });

      // B8 (LIA-408): the parent-facing dispatch_nested_agent tool. Every
      // factory below runs AGAIN, fresh, on each individual dispatch (never
      // once here) — resolveModel constructs an independently selected
      // client per dispatch's requested model id (AC4); buildChildTools/
      // buildChildMiddleware build fresh tool/middleware instances per
      // dispatch, so no child ever shares the parent's (or a sibling
      // child's) object by reference, and the child list is a strict subset
      // of the parent's (the child never receives this dispatch tool
      // itself, so recursive nesting is not introduced by this factory
      // alone). onUsage folds every child AIMessage into the SAME
      // usageCollector the parent uses below, using the parent's
      // outgoingSessionId and the child's own resolved model id.
      const dispatchTool = buildNestedDispatchTool({
        // KNOWN GAP (ai-eng-warden review; tracked in
        // docs/decisions/deus-v2-subagent-dispatch.md's Risks section, NOT
        // fixed by this ticket): `modelId` is a raw string the PARENT model
        // supplies via its own tool-call arguments, routed straight into
        // the real credential proxy with no server-side allowlist and no
        // per-turn dispatch cap. This does not cross the credential-proxy
        // boundary (single provider, same group token) but is a real,
        // currently uncapped cost-escalation surface — a compromised or
        // misdirected parent can select an expensive model on an unbounded
        // number of dispatches within one turn. A model-id allowlist plus a
        // per-turn dispatch budget is real future work, not silently
        // deferred.
        resolveModel: (modelId) =>
          buildProxyRoutedChatAnthropic(runContext, modelId),
        buildChildTools: () =>
          buildSafeTools(toolCtx, resolveAllowedWebFetchHosts()),
        // C1 (LIA-409): thread the SAME resolved `wardenCwd` into every
        // nested-dispatch child's fresh middleware stack — a child's wardens
        // layer must resolve the same worktree/repo-root as the parent's,
        // never silently fall back to `process.cwd()` (this factory runs
        // inside the same process, so an unset wardenCwd wouldn't throw or
        // even look wrong in isolation — it would just gate the wrong
        // worktree).
        buildChildMiddleware: () =>
          buildMiddlewareStack(resolveMiddlewareStackConfig(), {
            permissionProfile: rawPermissionProfile,
            wardenCwd,
          }).middleware,
        parentSessionId: outgoingSessionId,
        provider: 'anthropic',
        onUsage: async (observation) => {
          await usageCollector.record([observation.message], {
            provider: 'anthropic',
            model: observation.model,
          });
        },
      });

      const tools = [
        ...(await buildSafeTools(toolCtx, resolveAllowedWebFetchHosts())),
        dispatchTool,
      ];

      // B3 (LIA-403): the prompt-lifecycle hook is a SEPARATE, small
      // middleware APPENDED after B2's stack — never prepended or inserted,
      // so B2's own already-locked canonical order is untouched (LangChain
      // composes every middleware's beforeModel/wrapModelCall hooks
      // regardless of position). promptEvents is the inspectable per-prompt
      // log, discarded here like B2's own logs.
      const promptEvents: PromptEventRecord[] = [];
      const promptLifecycle = buildPromptLifecycleHook(
        sessionOpenMessage,
        promptEvents,
      );
      // Explicit AgentMiddleware[] annotation: the mixed array literal
      // otherwise infers a shape that misses createAgent's overloads.
      const allMiddleware: AgentMiddleware[] = [...middleware, promptLifecycle];

      const agent = createAgent({
        model,
        tools,
        middleware: allMiddleware,
        // B4 (LIA-404): real conversation continuity — LangGraph loads the
        // prior checkpoint for thread_id and appends this turn's input via
        // the messages channel's own reducer.
        checkpointer: getCheckpointer(),
      });

      // Non-goal (see module doc comment): the prompt sent to createAgent is
      // the bare runContext.prompt — session-open CLAUDE.md content reaches
      // the model via the prompt-lifecycle middleware's wrapModelCall
      // systemMessage injection, never by changing what invoke() receives.
      const result = await agent.invoke(
        {
          messages: [{ role: 'user', content: runContext.prompt }],
        },
        { configurable: { thread_id: outgoingSessionId } },
      );

      // result.messages is typed as the broad BaseMessage[] union (matches
      // A1's spike's own printTranscriptMessage pattern) -- only the AIMessage
      // subtype carries tool_calls/content in the shape used below, and
      // narrowing via `instanceof AIMessage` would require importing that
      // class just to satisfy the type checker for two optional-field reads.
      // These casts read tool_calls/content defensively (both accessed via
      // `?.`/Array.isArray, never assumed present) rather than assert a
      // stronger runtime guarantee than the union actually provides.
      //
      // B4 (LIA-404): with a checkpointer wired in, invoke() on a RESUMED
      // thread returns the FULL accumulated state (prior turns included),
      // not just this turn's messages — slicing by priorMessageCount scopes
      // the tool-call-emission loop below to only THIS turn's messages, so
      // earlier turns' tool calls are never re-emitted on resume. For a new
      // session (or a pre-B4 row with no real checkpoint) the count is 0 and
      // every message is processed — identical to pre-B4 behavior.
      const messages = (result.messages ?? []).slice(priorMessageCount);
      for (const message of messages) {
        const m = message as {
          tool_calls?: Array<{ name?: string; args?: unknown }>;
        };
        if (Array.isArray(m.tool_calls)) {
          for (const call of m.tool_calls) {
            if (!call.name) continue;
            await eventSink({
              type: 'tool_call',
              name: call.name,
              arguments: (call.args as Record<string, unknown>) ?? {},
            });
          }
        }
      }

      // B6 (LIA-406) + B8 (LIA-408): one 'usage' event per PARENT AIMessage
      // this turn, via the shared collector (identical unconditional-emit/
      // never-fabricate-zeros semantics as pre-B8 — see
      // deus-native-usage.ts). Any nested-dispatch child's usage was already
      // folded into the SAME collector by the dispatch tool's `onUsage`
      // callback during `agent.invoke()` above, so `aggregate()` below is
      // the combined parent-plus-every-child total for RunResult.usage —
      // never just the parent's own local total.
      // Hardcoded 'anthropic' because buildProxyRoutedChatAnthropic is the
      // only model-construction path today (single-provider). If a second
      // provider path is added, this must become a function of which client
      // was actually built.
      await usageCollector.record(messages, {
        provider: 'anthropic',
        // Read off the already-constructed ChatAnthropic instance — not
        // re-hardcoded, so it can't drift if the model tier changes.
        model: model.model,
      });
      const turnUsage = usageCollector.aggregate();

      const last = messages[messages.length - 1] as
        { content?: unknown } | undefined;
      const text =
        typeof last?.content === 'string'
          ? last.content
          : last?.content !== undefined
            ? JSON.stringify(last.content)
            : '';

      await eventSink({ type: 'output_text', text });
      await eventSink({ type: 'turn_complete' });

      // B4 (LIA-404): outgoingSessionId (computed at the top of the turn —
      // it doubled as the checkpointer thread_id) flows back through
      // RunResult.sessionRef into message-orchestrator.ts/task-scheduler.ts's
      // existing generic `if (runResult.sessionRef) setSession(...)`
      // persistence. Unlike B3's cosmetic marker, this id now references
      // real, resumable checkpointer state for this thread.
      return {
        status: 'success',
        result: text,
        sessionRef: { backend: 'deus-native', session_id: outgoingSessionId },
        // B6 (LIA-406): omitted (not zeroed) when no message in the turn
        // carried usage_metadata — never fabricate counts.
        ...(turnUsage !== undefined ? { usage: turnUsage } : {}),
      };
    } catch (err) {
      // Never throw out of runTurn — matches ContainerRuntime.runTurn's
      // never-throw contract (network failure, proxy auth failure, or any
      // other thrown exception surfaces as a status: 'error' result).
      const message = err instanceof Error ? err.message : String(err);
      return {
        status: 'error',
        result: null,
        error: message,
      };
    }
  }

  async close(_sessionRef: RuntimeSession): Promise<void> {
    // Stateless per-turn ChatAnthropic client — no persistent connection or
    // process to release at this stage of the roadmap. Session cleanup
    // handled by host via db.clearSession() (mirrors ContainerRuntime.close's
    // own comment).
  }
}

export function createDeusNativeRuntime(
  deps: ContainerRuntimeDeps,
): DeusNativeRuntime {
  return new DeusNativeRuntime(deps);
}
