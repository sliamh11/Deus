/**
 * Ordered, configurable middleware stack for the `deus-native` runtime
 * (LIA-402 / B2).
 *
 * Ordering semantics (verified against the installed langchain@1.5.3 source,
 * not docs — see the B2 plan's "Verified LangChain middleware semantics"):
 * `createAgent({middleware: [m0, m1, ...]})` treats array index 0 as the
 * OUTERMOST layer everywhere — `wrapToolCall`/`wrapModelCall` compose
 * backward so index 0 wraps everything else (dist/agents/utils.js
 * `chainToolCallHandlers`, dist/agents/nodes/AgentNode.js), and
 * `beforeAgent`/`beforeModel` nodes chain in forward array order while
 * `afterModel`/`afterAgent` chain in reverse (dist/agents/ReactAgent.js).
 * Passing `[permissions, wardens, memory, telemetry]` is therefore the one
 * array order that satisfies the AC's literal "permissions -> wardens ->
 * memory -> telemetry": permissions sees the request first and the response
 * last; telemetry sits closest to the real model call.
 *
 * The PERMISSIONS layer is real as of B7/LIA-407: a declarative first-match-
 * wins rule engine (permission-rules.ts) evaluated inside `wrapToolCall`,
 * with named profiles (`default` allow-all, `read-only` fail-closed) selected
 * via `BuildMiddlewareStackDeps.permissionProfile`. The MEMORY layer is real
 * for control-group turns as of D1/LIA-415: one `beforeModel` retrieval per
 * turn through the unchanged `scripts/memory_retrieval_hook.py` (see
 * memory-retrieval.ts and buildMemoryMiddleware's doc comment; other groups
 * keep the pass-through observer — AAG-014). The wardens and telemetry
 * layers remain explicit observe-only placeholders — each factory's doc
 * comment names the future work that replaces its substance. In particular,
 * the wardens layer
 * deliberately does NOT call `codex_warden_hooks.py`: wiring real gates into
 * a non-Claude backend's live tool-call path is exactly the remediation that
 * the Accepted ADR docs/decisions/hook-dispatch-facade-correction.md holds
 * as "deferred — NOT greenlit" pending its own separately-approved decision.
 */

import { createMiddleware, type AgentMiddleware } from 'langchain';
import { HumanMessage, ToolMessage } from '@langchain/core/messages';

import {
  evaluatePermission,
  resolvePermissionProfile,
} from './permission-rules.js';
import {
  retrieveMemoryContext,
  type MemoryRetrievalAdapter,
  type MemoryRetrievalRequest,
} from './memory-retrieval.js';

/** The four supported middleware layers, by canonical position. */
export type MiddlewareLayerName =
  'permissions' | 'wardens' | 'memory' | 'telemetry';

/**
 * Single source of truth for the enforced order (AC1). buildMiddlewareStack
 * FILTERS this array (never re-sorts), so any enabled subset preserves
 * relative order by construction (AC3).
 */
export const CANONICAL_MIDDLEWARE_ORDER: readonly MiddlewareLayerName[] = [
  'permissions',
  'wardens',
  'memory',
  'telemetry',
];

/**
 * Per-layer enable/disable. An absent key means ENABLED — disabling is
 * opt-in, matching the AC's "configuration CAN disable" phrasing (these
 * layers are meant to run by default once their substance is real).
 */
export type MiddlewareStackConfig = Partial<
  Record<MiddlewareLayerName, boolean>
>;

/**
 * One entry/exit marker pushed by a layer's beforeAgent/afterAgent pair when
 * a shared `orderMarkers` array is supplied (test-only observability — the
 * AC4 ordering proof). beforeAgent/afterAgent is the ONE hook type that
 * wraps the entire turn exactly once per layer regardless of how many
 * model/tool cycles run inside it (ReactAgent.js: entryNode/exitNode sit
 * outside the loopEntryNode), so it is the correct uniform hook for proving
 * cross-layer array-order composition. wrapToolCall and beforeModel/
 * afterModel/wrapModelCall fire at different ReAct-loop PHASES and never
 * nest into one onion — see the plan's "Defect found" writeup.
 */
export interface OrderMarker {
  layer: MiddlewareLayerName;
  phase: 'enter' | 'exit';
}

/** One observed tool-call decision — permissions/wardens log. */
export interface ToolCallDecisionRecord {
  toolName: string;
  /** The permissions layer records real allow/deny outcomes (B7/LIA-407);
   *  the wardens placeholder still only ever records 'allow'. */
  decision: 'allow' | 'deny';
  /** Evaluation provenance (AC1): 'rule' for an explicit rule match,
   *  'default' for the policy fallback. Populated by the permissions layer;
   *  absent for the wardens placeholder. */
  source?: 'rule' | 'default';
  /** Model-safe evaluation reason — never includes tool-call arguments.
   *  Populated by the permissions layer; absent for the wardens placeholder. */
  reason?: string;
}

/** One memory-layer hook firing. Records every beforeModel/afterModel pass
 *  (unchanged shape from the B2 placeholder era — D1's retrieval substance
 *  kept the record contract intact). */
export interface MemoryPassRecord {
  hook: 'beforeModel' | 'afterModel';
}

/** One observed model call — telemetry log (matches A3's inspectable
 *  `ProviderRoutingRecord[]` pattern, not real token accounting). */
export interface ModelCallRecord {
  /** 0-based, monotonic across this middleware instance's lifetime. */
  requestIndex: number;
  /** request.model's constructor name, e.g. 'ChatAnthropic'. */
  providerClass: string;
  /** Best-effort model id: (model as { model?: string }).model. */
  modelId: string | undefined;
  /** Number of messages in the wrapped request. */
  messageCount: number;
}

interface BuiltLayer<TRecord> {
  middleware: AgentMiddleware;
  log: TRecord[];
}

/**
 * The marker-only beforeAgent/afterAgent pair, wired IN ADDITION to a
 * layer's substantive hook when `orderMarkers` is provided (and not at all
 * otherwise, so the production graph gains no extra nodes). Deliberately
 * plain functions returning void: langchain's `getHookConstraint` only
 * wires a conditional (jumpTo-capable) edge for object-form hooks carrying
 * `canJumpTo` — a plain pass-through edge is what the ordering proof needs.
 */
function orderMarkerHooks(
  layer: MiddlewareLayerName,
  orderMarkers?: OrderMarker[],
): {
  beforeAgent?: () => void;
  afterAgent?: () => void;
} {
  if (!orderMarkers) return {};
  return {
    beforeAgent: () => {
      orderMarkers.push({ layer, phase: 'enter' });
    },
    afterAgent: () => {
      orderMarkers.push({ layer, phase: 'exit' });
    },
  };
}

/**
 * Permissions layer — REAL enforcement (B7/LIA-407). A thin `wrapToolCall`
 * adapter over the pure declarative evaluator in permission-rules.ts:
 * - resolves `permissionProfile` (default: `'default'`, today's allow-all
 *   behavior) via the named-profile registry, THROWING on an unknown name
 *   before any agent construction — never silently weakening the requested
 *   restriction;
 * - on ALLOW, records the decision and calls `handler(request)` exactly once
 *   with the ORIGINAL request object. Never `handler({ ...request })` or any
 *   tool-call reconstruction (AC5): langchain's `chainToolCallHandlers`
 *   (dist/agents/utils.js) supports request modification by inner layers, so
 *   passing the original reference is a deliberate no-rewrite guarantee, not
 *   an accident;
 * - on DENY, records the decision and returns a synthetic error-status
 *   `ToolMessage` carrying the original tool name/id and a stable
 *   `permission_denied` message naming the profile and reason — the handler
 *   is never invoked. Denial content deliberately EXCLUDES the tool call's
 *   arguments (AC2: no needless exposure of potentially sensitive values).
 *   Returning a `ToolMessage` (not a thrown error or `Command`) follows
 *   LangChain's installed authentication example and keeps the feedback
 *   inside the model's ReAct loop; no HITL/interrupt/edit path exists here
 *   (AC5, plan Non-goals).
 */
export function buildPermissionsMiddleware(
  permissionProfile?: string,
  orderMarkers?: OrderMarker[],
): BuiltLayer<ToolCallDecisionRecord> {
  const profileName = permissionProfile ?? 'default';
  // Throws on an unknown profile name — fail visibly, before createAgent.
  const policy = resolvePermissionProfile(profileName);
  const log: ToolCallDecisionRecord[] = [];
  const middleware = createMiddleware({
    name: 'permissions',
    wrapToolCall: (request, handler) => {
      const toolName = request.toolCall.name;
      const evaluation = evaluatePermission(policy, toolName);
      log.push({
        toolName,
        decision: evaluation.decision,
        source: evaluation.source,
        reason: evaluation.reason,
      });
      if (evaluation.decision === 'allow') {
        // AC5: the original request object, delegated exactly once.
        return handler(request);
      }
      return new ToolMessage({
        tool_call_id: request.toolCall.id ?? '',
        name: toolName,
        status: 'error',
        content:
          `permission_denied: tool "${toolName}" was blocked by the ` +
          `"${profileName}" permission profile (${evaluation.reason}). ` +
          `The call was not executed; continue without this tool.`,
      });
    },
    ...orderMarkerHooks('permissions', orderMarkers),
  });
  return { middleware, log };
}

/**
 * Wardens layer — PLACEHOLDER, same allow-all + log shape as permissions
 * but a structurally distinct instance (separate middleware, separate log,
 * separate toggle) so a future real implementation replaces ONE layer
 * without touching the other. Deliberately does NOT call
 * `codex_warden_hooks.py`: real non-Claude-backend guardrails are exactly
 * what docs/decisions/hook-dispatch-facade-correction.md (Accepted) lists
 * under "Remediation options (deferred — NOT greenlit)", pending a
 * separate approval — likely alongside/after B7 once deus-native has
 * file-editing tools for a gate to meaningfully check.
 */
export function buildWardensMiddleware(
  orderMarkers?: OrderMarker[],
): BuiltLayer<ToolCallDecisionRecord> {
  const log: ToolCallDecisionRecord[] = [];
  const middleware = createMiddleware({
    name: 'wardens',
    wrapToolCall: (request, handler) => {
      log.push({ toolName: request.toolCall.name, decision: 'allow' });
      return handler(request);
    },
    ...orderMarkerHooks('wardens', orderMarkers),
  });
  return { middleware, log };
}

/**
 * Memory layer — REAL for control-group turns as of D1/LIA-415. On the
 * FIRST `beforeModel` firing of a turn that supplied `memoryRequest`, it
 * runs one retrieval through the unchanged `scripts/memory_retrieval_hook.py`
 * (via the subprocess adapter in memory-retrieval.ts) and, when the hook
 * returns non-empty context, appends ONE user-role context message through
 * LangGraph's default messages append-reducer — so the recalled context is
 * visible in the model input for this and every later cycle of the turn.
 *
 * Deliberate constraints, each load-bearing:
 * - One retrieval per turn (closure-local boolean): `buildMiddlewareStack`
 *   is constructed once per `runTurn`, so tool-driven follow-up model
 *   cycles retain the injected message but never rerun retrieval or the
 *   hook's session-concept/dedup side effects.
 * - A user-role message, NEVER a SystemMessage: `beforeModel` message
 *   updates APPEND after the user message, and Anthropic's payload
 *   converter rejects a system message outside position zero (see
 *   lifecycle-events.ts's module doc for the verified failure mode). The
 *   hook's own untrusted-memory framing stays intact inside the content.
 * - Fail-open (AC4): an absent `memoryRequest`, an empty result, or an
 *   adapter failure all leave the model input unchanged and never fail
 *   the turn.
 * - Group scoping: the hook reads the user's PERSONAL vault and is not
 *   group-scoped, so the caller (deus-native-backend.ts) supplies
 *   `memoryRequest` only for control-group turns; every other group keeps
 *   the pass-through observer behavior. The remaining non-control-group
 *   parity gap is tracked as AAG-014 in docs/agent-agnostic-debt.md.
 */
export function buildMemoryMiddleware(
  memoryRequest?: MemoryRetrievalRequest,
  retrievalAdapter: MemoryRetrievalAdapter = retrieveMemoryContext,
  orderMarkers?: OrderMarker[],
): BuiltLayer<MemoryPassRecord> {
  const log: MemoryPassRecord[] = [];
  // One-shot gate: this factory runs once per turn, so a plain closure
  // boolean is the whole "first applicable firing" mechanism.
  let retrievalAttempted = false;
  const middleware = createMiddleware({
    name: 'memory',
    beforeModel: async () => {
      log.push({ hook: 'beforeModel' });
      if (memoryRequest === undefined || retrievalAttempted) return;
      retrievalAttempted = true;
      let context: string;
      try {
        context = await retrievalAdapter(memoryRequest);
      } catch {
        // The production adapter never rejects by contract; this guard
        // keeps the AC4 never-fail-the-turn invariant independent of any
        // injected adapter's behavior.
        context = '';
      }
      if (context === '') return;
      // Appended via the default messages reducer — lands AFTER the user's
      // message, before this cycle's model call.
      return { messages: [new HumanMessage(context)] };
    },
    afterModel: () => {
      log.push({ hook: 'afterModel' });
    },
    ...orderMarkerHooks('memory', orderMarkers),
  });
  return { middleware, log };
}

/**
 * Telemetry layer — PLACEHOLDER. A `wrapModelCall` middleware recording
 * call metadata (provider class, model id, message count) into an
 * inspectable log — NOT real token/usage accounting, which does not exist
 * anywhere in src/ yet (RunResult has no usage field; src/config.ts cites
 * LIA-194) and is B6/LIA-406's job. Innermost per the canonical order,
 * architecturally correct: telemetry observes the actual call closest to
 * the real model boundary, unmutated by any outer layer's decision.
 */
export function buildTelemetryMiddleware(
  orderMarkers?: OrderMarker[],
): BuiltLayer<ModelCallRecord> {
  const log: ModelCallRecord[] = [];
  let requestIndex = 0;
  const middleware = createMiddleware({
    name: 'telemetry',
    wrapModelCall: (request, handler) => {
      log.push({
        requestIndex: requestIndex++,
        providerClass: request.model.constructor.name,
        modelId: (request.model as { model?: string }).model,
        messageCount: request.messages.length,
      });
      return handler(request);
    },
    ...orderMarkerHooks('telemetry', orderMarkers),
  });
  return { middleware, log };
}

/** Each enabled layer's inspectable log; a disabled layer's key is absent. */
export interface MiddlewareStackLogs {
  permissions?: ToolCallDecisionRecord[];
  wardens?: ToolCallDecisionRecord[];
  memory?: MemoryPassRecord[];
  telemetry?: ModelCallRecord[];
}

export interface BuildMiddlewareStackDeps {
  /** Shared ordered array for the AC4 entry/exit ordering proof — each
   *  enabled layer wires a marker-pushing beforeAgent/afterAgent pair when
   *  this is provided. Omit in production. */
  orderMarkers?: OrderMarker[];
  /** Named permission profile for the permissions layer (B7/LIA-407).
   *  Omitted => 'default' (allow-all, today's behavior); 'read-only' =>
   *  the fail-closed read-only preset. An unrecognized name THROWS during
   *  stack construction — before any agent exists — rather than silently
   *  weakening the requested restriction. */
  permissionProfile?: string;
  /** D1 (LIA-415): the submitted prompt + backend-scoped session id for the
   *  memory layer's one-per-turn retrieval. Supplied by deus-native's
   *  runTurn ONLY for control-group turns (the retrieval hook reads the
   *  user's personal vault and is not group-scoped — see AAG-014). Omitted
   *  => the memory layer stays a pass-through observer. */
  memoryRequest?: MemoryRetrievalRequest;
  /** Test-only override of the memory retrieval adapter — hermetic doubles
   *  instead of the real Python subprocess. Omit in production. */
  memoryRetrievalAdapter?: MemoryRetrievalAdapter;
}

export interface MiddlewareStackResult {
  /** Ordered array, ready for `createAgent({middleware})` — index 0
   *  (permissions) is the outermost layer. */
  middleware: AgentMiddleware[];
  logs: MiddlewareStackLogs;
}

/**
 * Builds the ordered middleware stack. Iterates CANONICAL_MIDDLEWARE_ORDER
 * and only SKIPS disabled layers — relative order of the remaining layers
 * is preserved by construction (AC3), with no separate sort step to get
 * wrong. A layer is disabled only by an explicit `false` (absent = enabled).
 */
export function buildMiddlewareStack(
  config: MiddlewareStackConfig = {},
  deps: BuildMiddlewareStackDeps = {},
): MiddlewareStackResult {
  const middleware: AgentMiddleware[] = [];
  const logs: MiddlewareStackLogs = {};

  // B7 (LIA-407): validate a requested profile name UP FRONT, even when the
  // permissions layer itself is toggled off — an invalid name must always
  // fail visibly, never be silently ignored while the caller believes a
  // restriction is in force.
  if (deps.permissionProfile !== undefined) {
    resolvePermissionProfile(deps.permissionProfile);
  }

  for (const layer of CANONICAL_MIDDLEWARE_ORDER) {
    if (config[layer] === false) continue;
    switch (layer) {
      case 'permissions': {
        const built = buildPermissionsMiddleware(
          deps.permissionProfile,
          deps.orderMarkers,
        );
        middleware.push(built.middleware);
        logs.permissions = built.log;
        break;
      }
      case 'wardens': {
        const built = buildWardensMiddleware(deps.orderMarkers);
        middleware.push(built.middleware);
        logs.wardens = built.log;
        break;
      }
      case 'memory': {
        const built = buildMemoryMiddleware(
          deps.memoryRequest,
          deps.memoryRetrievalAdapter,
          deps.orderMarkers,
        );
        middleware.push(built.middleware);
        logs.memory = built.log;
        break;
      }
      case 'telemetry': {
        const built = buildTelemetryMiddleware(deps.orderMarkers);
        middleware.push(built.middleware);
        logs.telemetry = built.log;
        break;
      }
    }
  }

  return { middleware, logs };
}

/**
 * AC5: deterministic validation/normalization of a raw (already-parsed)
 * config value.
 * - Unknown keys are DROPPED (normalized, not rejected) — forward-compat
 *   tolerance matching `parseAgentBackend`'s "a value we don't yet
 *   recognize" precedent in agent-runtimes/types.ts.
 * - A non-boolean value for a KNOWN key THROWS — a malformed value is a
 *   real config bug, not a forward-compat concern, so failing loud is
 *   correct here unlike the unknown-key case.
 * - A non-object (null, array, primitive) THROWS for the same reason.
 */
export function parseMiddlewareStackConfig(
  raw: unknown,
): MiddlewareStackConfig {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    const got = Array.isArray(raw)
      ? 'array'
      : raw === null
        ? 'null'
        : typeof raw;
    throw new Error(
      `parseMiddlewareStackConfig: expected a plain object of per-layer ` +
        `booleans (e.g. {"wardens": false}), got ${got}`,
    );
  }
  const config: MiddlewareStackConfig = {};
  for (const [key, value] of Object.entries(raw)) {
    if (!(CANONICAL_MIDDLEWARE_ORDER as readonly string[]).includes(key)) {
      continue; // unknown key: dropped (normalized)
    }
    if (typeof value !== 'boolean') {
      throw new Error(
        `parseMiddlewareStackConfig: layer "${key}" must be a boolean, ` +
          `got ${typeof value}`,
      );
    }
    config[key as MiddlewareLayerName] = value;
  }
  return config;
}

/**
 * Resolves the stack config from the DEUS_NATIVE_MIDDLEWARE_CONFIG env var
 * (JSON string, e.g. '{"wardens":false}'), mirroring deus-native-backend's
 * plain-env-var pattern (DEUS_NATIVE_WEB_FETCH_ALLOWED_HOSTS) — per-group
 * middleware config is out of B2's scope. Two deliberately different
 * failure modes:
 * - unset or malformed-as-JSON STRING: lenient, defaults to {} (all
 *   enabled) — an env-var typo must not crash the whole runtime;
 * - valid JSON that is a malformed config OBJECT: strict,
 *   parseMiddlewareStackConfig throws — same as any programmatic caller.
 */
export function resolveMiddlewareStackConfig(): MiddlewareStackConfig {
  const raw = process.env.DEUS_NATIVE_MIDDLEWARE_CONFIG;
  if (!raw) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return {};
  }
  return parseMiddlewareStackConfig(parsed);
}
