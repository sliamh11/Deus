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
 * via `BuildMiddlewareStackDeps.permissionProfile`. The MEMORY layer is real:
 * D1/LIA-415 performs one `beforeModel` retrieval per control-group turn
 * through the unchanged `scripts/memory_retrieval_hook.py` (other groups keep
 * the pass-through observer — AAG-014), and D3/LIA-417 adds a post-success
 * `wrapToolCall` re-embedding mechanism through the unchanged
 * `scripts/memory_tree_hook.py`. The latter is currently dormant because the
 * live deus-native tool surface contains no supported edit tool. The WARDENS
 * layer is real as of C1/LIA-409: `buildWardensMiddleware` now invokes the
 * unchanged `scripts/codex_warden_hooks.py` gate runners (plan-review-gate,
 * code-review-gate, ai-eng-gate, verification-gate) over the exact
 * `apply_patch`/commit-shaped-`Bash` tool contract the independent oracle
 * (`middleware-stack.warden-gates.oracle.test.ts`) pins — this is the exact
 * remediation the Accepted ADR
 * docs/decisions/hook-dispatch-facade-correction.md previously held as
 * "deferred — NOT greenlit" pending its own separately-approved decision,
 * which LIA-409 is. Like D3's re-embedding mechanism, the wardens trigger is
 * currently dormant in production: `apply_patch`/`Bash` are never registered
 * on the live `deus-native` tool surface (`SAFE_TOOL_NAMES`), so it activates
 * automatically once a future, separately-reviewed ticket widens that
 * surface. TELEMETRY remains an explicit observe-only placeholder — its
 * factory's doc comment names the future work that replaces its substance.
 */

import { execFile, execFileSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

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
import {
  triggerMemoryReembed,
  type MemoryReembedAdapter,
  type MemoryReembedRequest,
  type MemoryReembedToolName,
} from './memory-reembed.js';

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
  /** Both the permissions layer (B7/LIA-407) and the wardens layer
   *  (C1/LIA-409) record real allow/deny outcomes. */
  decision: 'allow' | 'deny';
  /** Evaluation provenance (AC1): 'rule' for an explicit rule match,
   *  'default' for the policy fallback. Populated by the permissions layer
   *  only; the wardens layer's decision comes from an external gate process,
   *  not a rule/default distinction, so this stays absent there. */
  source?: 'rule' | 'default';
  /** Model-safe evaluation reason — never includes tool-call arguments.
   *  Populated by the permissions layer (policy reason) and the wardens
   *  layer (the Python gate's exact `permissionDecisionReason`, or the
   *  sanitized fail-closed REVISE message on an infrastructure failure). */
  reason?: string;
}

/** One memory-layer hook firing. Records every beforeModel/afterModel pass
 *  (unchanged shape from the B2 placeholder era — D1's retrieval substance
 *  kept the record contract intact). */
export interface MemoryPassRecord {
  hook: 'beforeModel' | 'afterModel';
}

/**
 * Maps supported broker-shaped and existing-hook-shaped edit calls onto the
 * singular canonical PostToolUse request understood by memory_tree_hook.py.
 * Accepted path values are preserved exactly; blank or non-string values do
 * not qualify as edits.
 */
function memoryReembedRequestForToolCall(toolCall: {
  name: string;
  args: unknown;
}): MemoryReembedRequest | undefined {
  const mapping: Record<
    string,
    { toolName: MemoryReembedToolName; pathKey: 'path' | 'file_path' }
  > = {
    write_file: { toolName: 'Write', pathKey: 'path' },
    edit_file: { toolName: 'Edit', pathKey: 'path' },
    Write: { toolName: 'Write', pathKey: 'file_path' },
    Edit: { toolName: 'Edit', pathKey: 'file_path' },
    MultiEdit: { toolName: 'MultiEdit', pathKey: 'file_path' },
  };
  const supported = mapping[toolCall.name];
  if (supported === undefined) return;
  if (
    typeof toolCall.args !== 'object' ||
    toolCall.args === null ||
    Array.isArray(toolCall.args)
  ) {
    return;
  }
  const filePath = (toolCall.args as Record<string, unknown>)[
    supported.pathKey
  ];
  if (typeof filePath !== 'string' || filePath.trim() === '') return;
  return { toolName: supported.toolName, filePath };
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
 * The Python `GIT_COMMIT_RE` prefilter, mirrored exactly
 * (`scripts/codex_warden_hooks.py:241`). This is a cheap TypeScript-side
 * prefilter only — the unchanged Python runners repeat the authoritative
 * check themselves (`scripts/codex_warden_hooks.py:1577`, `:1600`), so a
 * false positive here can never force an allow. A future regex edit must
 * update both copies together (see PLAN.md's commit-prefilter-drift risk).
 */
const GIT_COMMIT_RE = /(^|[;&|]\s*)git(?:\s+-C\s+\S+)?\s+commit(\s|$)/;

/** Commit-path behaviors, in the exact order `.claude/settings.json`'s
 *  `Bash` matcher wires them (`code-review-gate`, `ai-eng-gate`,
 *  `verification-gate`) — a fixed tuple so dispatch is constant bounded
 *  work and order is never accidentally reshuffled. */
const COMMIT_BEHAVIORS = [
  'code-review-gate',
  'ai-eng-gate',
  'verification-gate',
] as const;

/** The plan-review behavior, triggered by the exact (unchanged, untranslated)
 *  `apply_patch` tool name — `codex_warden_hooks.py`'s own
 *  `run_plan_review_gate` matcher list already recognizes this literal tool
 *  name (`scripts/codex_warden_hooks.py:1417-1420`). */
const PLAN_REVIEW_BEHAVIOR = 'plan-review-gate';

/**
 * Selects which unchanged Python gate behaviors apply to a native tool call,
 * in dispatch order. Returns an empty array for any tool call outside the
 * frozen oracle's exact trigger contract — no `write_file`/`edit_file` →
 * `Write`/`Edit` translation and no `bash_exec` → `Bash` translation (PLAN.md
 * Non-Goals): those names are simply not in scope, so they fall through to
 * the empty-array (no gate invocation, single delegate) path below.
 */
function selectWardenBehaviors(
  toolName: string,
  toolInput: Record<string, unknown>,
): readonly string[] {
  if (toolName === 'apply_patch') {
    return [PLAN_REVIEW_BEHAVIOR];
  }
  if (toolName === 'Bash') {
    const command = toolInput['command'];
    if (typeof command === 'string' && GIT_COMMIT_RE.test(command)) {
      return COMMIT_BEHAVIORS;
    }
  }
  return [];
}

/**
 * Resolves the shared `.claude/wardens/` repo root from the warden event
 * `cwd`, following the exact worktree-safe authority `.claude/hooks/
 * warden-shim.sh` already uses (its own comment: "When Claude Code runs
 * inside a git worktree, CLAUDE_PROJECT_DIR points to the worktree path, not
 * the main repo root. Hooks and wardens need the main repo root..."):
 * `git rev-parse --path-format=absolute --git-common-dir`, then the parent
 * directory of that path. This is a structured, cross-platform Git query —
 * no shell string parsing, no `sed`.
 *
 * Falls back to the old module-relative computation
 * (`fileURLToPath(new URL('../..', import.meta.url))`) only when `cwd` is
 * not inside any Git repository or Git itself is unavailable; the frozen
 * oracle never exercises this fallback. A bad fallback path cannot silently
 * allow a protected tool: a missing/wrong script path becomes an `execFile`
 * spawn failure, which `runWardenBehavior` below treats as a fail-closed
 * gate infrastructure error, never an allow.
 *
 * Resolved once per `buildWardensMiddleware` construction (i.e. once per
 * `runTurn` in production, not once per tool call) and closed over for the
 * lifetime of that middleware instance — never re-queried per protected
 * call.
 */
function resolveWardenRepoRoot(wardenCwd: string): string {
  try {
    const commonGitDir = execFileSync(
      'git',
      ['rev-parse', '--path-format=absolute', '--git-common-dir'],
      { cwd: wardenCwd, encoding: 'utf8', timeout: 5000 },
    ).trim();
    if (commonGitDir === '') {
      throw new Error('git rev-parse --git-common-dir returned empty output');
    }
    return dirname(commonGitDir);
  } catch {
    // Non-git cwd, or git unavailable: explicit non-git fallback. Both
    // src/agent-runtimes and dist/agent-runtimes sit two directories below
    // the checkout root today.
    return fileURLToPath(new URL('../..', import.meta.url));
  }
}

/** The two possible outcomes `codex_warden_hooks.py` can ever produce for
 *  these four runners, per `_block_pre_tool`
 *  (`scripts/codex_warden_hooks.py:405-414`) and each runner's early-return
 *  no-op path — plus a third, TypeScript-side-only outcome for anything that
 *  doesn't fit that two-outcome contract (subprocess failure, malformed
 *  stdout, or protocol drift), which fails closed exactly like a real deny. */
type WardenGateOutcome =
  | { readonly kind: 'allow' }
  | { readonly kind: 'deny'; readonly reason: string }
  | { readonly kind: 'error' };

/**
 * Extracts the deny reason from parsed stdout JSON, or `undefined` if the
 * object doesn't match the exact `_block_pre_tool` deny contract — malformed
 * JSON, arrays/primitives, a deny without a usable reason, or any other
 * unexpected non-empty object all return `undefined` here and are treated as
 * protocol drift by the caller, never silently accepted as an allow.
 */
function extractDenyReason(parsed: unknown): string | undefined {
  if (typeof parsed !== 'object' || parsed === null) return undefined;
  const hookSpecificOutput = (parsed as Record<string, unknown>)[
    'hookSpecificOutput'
  ];
  if (typeof hookSpecificOutput !== 'object' || hookSpecificOutput === null) {
    return undefined;
  }
  const record = hookSpecificOutput as Record<string, unknown>;
  const decision = record['permissionDecision'];
  const reason = record['permissionDecisionReason'];
  if (decision === 'deny' && typeof reason === 'string' && reason !== '') {
    return reason;
  }
  return undefined;
}

/**
 * Invokes one unchanged Python gate behavior over stdin, following
 * `src/evolution-client.ts:_runPython`'s `execFile` shape
 * (`src/evolution-client.ts:248-259`) while adding stdin because this CLI
 * reads the event with `_read_stdin_json()`
 * (`scripts/codex_warden_hooks.py:272-280`). Never inspects stderr or the
 * exit code to infer allow/deny: all four relevant gate functions return 0
 * unconditionally (`scripts/codex_warden_hooks.py:3795-3810`) — the stdout
 * JSON is the only deny signal (empty stdout is allow/no-op).
 *
 * Any `execFile` callback error (spawn failure, missing `python3`, non-zero
 * exit, timeout, max-buffer overrun), any stdin transport error (e.g. an
 * early child exit producing EPIPE), and any non-empty stdout that isn't
 * exactly the deny contract, all resolve to `{ kind: 'error' }` — fail
 * closed, never a silent allow. A single-settlement guard means a stdin
 * error and a callback error can never both resolve this promise.
 */
function runWardenBehavior(
  behavior: string,
  event: { cwd: string; tool_name: string; tool_input: unknown },
  repoRoot: string,
): Promise<WardenGateOutcome> {
  return new Promise((settlePromise) => {
    // Named `settlePromise`, not `resolve`: this file also imports `resolve`
    // from `node:path` for wardenCwd normalization, and reusing that name
    // here would shadow it — harmless (JS scoping still resolves correctly)
    // but a needless trap for a future edit in this exact function.
    let settled = false;
    const settle = (outcome: WardenGateOutcome) => {
      if (settled) return;
      settled = true;
      settlePromise(outcome);
    };

    // Node's child_process methods essentially never throw synchronously for
    // valid string/array args, but this try/catch guarantees the graceful
    // fail-closed `{ kind: 'error' }` path even in that case — a thrown
    // exception here would otherwise reject this Promise directly (an
    // unhandled-rejection risk) instead of resolving through `settle` like
    // every other failure mode in this function.
    try {
      const scriptPath = join(repoRoot, 'scripts', 'codex_warden_hooks.py');
      const child = execFile(
        'python3',
        [scriptPath, 'run', behavior, '--repo-root', repoRoot],
        { timeout: 5000, maxBuffer: 64 * 1024 },
        (err, stdout) => {
          if (err) {
            settle({ kind: 'error' });
            return;
          }
          const trimmed = stdout.trim();
          if (trimmed === '') {
            settle({ kind: 'allow' });
            return;
          }
          let parsed: unknown;
          try {
            parsed = JSON.parse(trimmed);
          } catch {
            settle({ kind: 'error' });
            return;
          }
          const reason = extractDenyReason(parsed);
          if (reason === undefined) {
            settle({ kind: 'error' });
            return;
          }
          settle({ kind: 'deny', reason });
        },
      );

      child.stdin?.on('error', () => {
        settle({ kind: 'error' });
      });

      child.stdin?.end(JSON.stringify(event));
    } catch {
      settle({ kind: 'error' });
    }
  });
}

/** The stable, sanitized fail-closed feedback for a gate infrastructure
 *  failure — deliberately excludes stderr, stack traces, tool arguments,
 *  patch contents, and the attempted shell command. The layer-wide
 *  `wardens: false` toggle is the explicit operational recovery switch;
 *  this message must never invent an implicit bypass. */
function fallbackRevisedMessage(behavior: string): string {
  return (
    `[${behavior}] REVISE: the warden gate could not be evaluated; the ` +
    `protected action was not executed. Restore the gate runner and retry.`
  );
}

/**
 * Wardens layer — REAL enforcement (C1/LIA-409). A `wrapToolCall` adapter
 * over the unchanged `scripts/codex_warden_hooks.py` gate runners:
 * - `selectWardenBehaviors` recognizes exactly two frozen-oracle trigger
 *   shapes: literal `apply_patch` (→ `plan-review-gate`) and commit-shaped
 *   literal `Bash` (→ `code-review-gate`, `ai-eng-gate`, `verification-gate`
 *   in that fixed order). Every other tool call produces no gate invocation
 *   and delegates exactly once with the ORIGINAL request object — same
 *   no-rewrite guarantee `buildPermissionsMiddleware` already follows.
 * - The serialized event's `tool_name`/`tool_input` are value-equal to the
 *   native call's `toolCall.name`/`toolCall.args` — no renaming, no field
 *   filtering. The Python gate logic remains the sole authority for
 *   worktree membership, per-warden configuration, review-marker state,
 *   LLM-diff detection, and allow/deny reasons.
 * - Selected behaviors run sequentially; an allow/no-op continues to the
 *   next one, while the first deny or infrastructure failure stops
 *   immediately (the protected action is already blocked) and returns a
 *   synthetic error-status `ToolMessage` — the handler is never invoked on
 *   either kind of block. A genuine Python deny returns its exact
 *   `permissionDecisionReason` verbatim; an infrastructure/protocol failure
 *   returns the stable sanitized `REVISE` message instead — both fail
 *   closed, unlike best-effort memory retrieval.
 * - On complete allow (including the trivial zero-behavior case), one
 *   aggregate `{ toolName, decision: 'allow' }` record is logged and
 *   `handler(request)` is called exactly once with the original request.
 */
export function buildWardensMiddleware(
  wardenCwd?: string,
  orderMarkers?: OrderMarker[],
): BuiltLayer<ToolCallDecisionRecord> {
  const log: ToolCallDecisionRecord[] = [];
  // Normalized here (not just at the deus-native-backend.ts call site):
  // buildWardensMiddleware/buildMiddlewareStack are public entry points, and
  // a relative wardenCwd would otherwise serialize a relative `cwd` into the
  // PreToolUse event — violating the Python hook's absolute-path contract
  // and potentially misdirecting worktree/config resolution — even though
  // today's one production caller already resolves it first.
  const resolvedCwd = resolve(wardenCwd ?? process.cwd());
  const repoRoot = resolveWardenRepoRoot(resolvedCwd);

  const middleware = createMiddleware({
    name: 'wardens',
    wrapToolCall: async (request, handler) => {
      const toolName = request.toolCall.name;
      const toolInput = request.toolCall.args;
      const behaviors = selectWardenBehaviors(toolName, toolInput);

      if (behaviors.length === 0) {
        log.push({ toolName, decision: 'allow' });
        // AC5-style no-rewrite guarantee: the original request object,
        // delegated exactly once.
        return handler(request);
      }

      const event = {
        cwd: resolvedCwd,
        tool_name: toolName,
        tool_input: toolInput,
      };

      for (const behavior of behaviors) {
        const outcome = await runWardenBehavior(behavior, event, repoRoot);
        if (outcome.kind === 'allow') continue;

        const reason =
          outcome.kind === 'deny'
            ? outcome.reason
            : fallbackRevisedMessage(behavior);
        log.push({ toolName, decision: 'deny', reason });
        return new ToolMessage({
          tool_call_id: request.toolCall.id ?? '',
          name: toolName,
          status: 'error',
          content: reason,
        });
      }

      log.push({ toolName, decision: 'allow' });
      return handler(request);
    },
    ...orderMarkerHooks('wardens', orderMarkers),
  });
  return { middleware, log };
}

/**
 * Memory layer — REAL retrieval for control-group turns as of D1/LIA-415,
 * plus a unit-complete but currently unreachable edit re-embedding mechanism
 * as of D3/LIA-417. On the FIRST `beforeModel` firing of a turn that supplied
 * `memoryRequest`, it runs one retrieval through the unchanged
 * `scripts/memory_retrieval_hook.py` (via memory-retrieval.ts) and, when the
 * hook returns non-empty context, appends ONE user-role context message
 * through LangGraph's default messages append-reducer — so the recalled
 * context is visible in the model input for this and every later cycle.
 *
 * `wrapToolCall` separately recognizes supported broker/hook edit names,
 * delegates the original request exactly once, and invokes the unchanged
 * `scripts/memory_tree_hook.py` protocol only after a successful result. The
 * live deus-native inclusion filter still exposes only web_search/web_fetch,
 * so no production tool can currently activate that branch; direct tests pin
 * the mechanism while deus-native-tool-scope.oracle.test.ts pins reachability.
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
 * - Re-embedding is independent of `memoryRequest`: memory_tree_hook.py owns
 *   vault membership checks, while D1's control-group restriction governs
 *   personal-memory retrieval only.
 */
export function buildMemoryMiddleware(
  memoryRequest?: MemoryRetrievalRequest,
  retrievalAdapter: MemoryRetrievalAdapter = retrieveMemoryContext,
  reembedAdapter: MemoryReembedAdapter = triggerMemoryReembed,
  orderMarkers?: OrderMarker[],
): BuiltLayer<MemoryPassRecord> {
  const log: MemoryPassRecord[] = [];
  // One-shot gate: this factory runs once per turn, so a plain closure
  // boolean is the whole "first applicable firing" mechanism.
  let retrievalAttempted = false;
  const middleware = createMiddleware({
    name: 'memory',
    wrapToolCall: async (request, handler) => {
      const reembedRequest = memoryReembedRequestForToolCall(request.toolCall);
      const result = await handler(request);
      if (ToolMessage.isInstance(result) && result.status === 'error') {
        return result;
      }
      if (reembedRequest !== undefined) {
        try {
          await reembedAdapter(reembedRequest);
        } catch {
          // The production adapter never rejects; keep post-success
          // re-embedding fail-open even for an injected adapter.
        }
      }
      return result;
    },
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
  /** D3 (LIA-417): test-only override of the post-success edit
   *  re-embedding adapter. Omit in production. */
  memoryReembedAdapter?: MemoryReembedAdapter;
  /** The `cwd` serialized into every wardens-layer PreToolUse event and used
   *  to resolve the shared `.claude/wardens/` repo root (C1/LIA-409).
   *  Omitted => `process.cwd()`, matching the frozen oracle's own default.
   *  Production wiring resolves this from `runContext.worktreePath ??
   *  runContext.cwd ?? process.cwd()` (see `deus-native-backend.ts`). */
  wardenCwd?: string;
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
        const built = buildWardensMiddleware(deps.wardenCwd, deps.orderMarkers);
        middleware.push(built.middleware);
        logs.wardens = built.log;
        break;
      }
      case 'memory': {
        const built = buildMemoryMiddleware(
          deps.memoryRequest,
          deps.memoryRetrievalAdapter,
          deps.memoryReembedAdapter,
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
