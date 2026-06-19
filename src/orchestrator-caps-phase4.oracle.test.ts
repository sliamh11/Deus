/**
 * Oracle tests for Ingress Phase 4 — Part D: Caps wiring in createMessageOrchestrator
 * (the leak-free IngressCaps seam for publicIngress groups)
 *
 * Authored from the SPEC (LIA-315 Phase 4 contract), BEFORE any implementation
 * exists (oracle-author warden). The orchestrator currently has NO `ingressCaps`
 * parameter, so these tests are RED: the import of the extended
 * `createMessageOrchestrator` (or `OrchestratorDeps`) with the new dep fails /
 * the tested behaviors are absent.
 *
 * Independence: written blind to any implementation. Every expected value traces
 * to the spec. @oracle tags protect this file from silent weakening after the
 * implementation ships.
 *
 * Design note: the real orchestrator has a complex async loop that is hard to
 * drive directly. We inject a fake "run executor" (the `runAgent`-equivalent
 * seam) so the assertions can focus on the caps wiring (tryAdmit / release /
 * recordSpend / cursor advancement) without needing a real container runtime.
 * Where a behavior can only be asserted partially because of the harness, this
 * is noted in-line.
 *
 * The four security-critical cases are covered at minimum:
 *   D1 — admitted run: release() + recordSpend() both called exactly once (finally semantics)
 *   D2 — tryAdmit {ok:false}: NO run, cursor IS advanced (load-shed)
 *   D3 — token-dark run: recordSpend called with NON-ZERO fallback
 *   D4 — no ingressCaps dep on a publicIngress group: run REFUSED, cursor advanced
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks — must be declared before any imports that consume them ──────

vi.mock('./config.js', () => ({
  ASSISTANT_NAME: 'Deus',
  IDLE_TIMEOUT: 30_000,
  POLL_INTERVAL: 1_000,
  TIMEZONE: 'UTC',
  TRIGGER_PATTERN: /^@deus\b/i,
  SESSION_IDLE_RESET_HOURS: 8,
  DEFAULT_AGENT_RUNTIME: 'claude',
  PROJECT_ROOT: '/tmp/deus-oracle-test',
  INJECTION_SCANNER_CONFIG: {
    enabled: false,
    threshold: 0.7,
    logOnly: true,
  },
  CONTEXT_NOTIFY: false,
  // LIA-315 Phase 4: fixed per-run spend charge (non-zero so the daily cap is
  // real). Harness stub only — does not weaken any @oracle assertion.
  INGRESS_WEBHOOK_RUN_COST: 50_000,
}));

vi.mock('./guardrails/injection-scanner.js', () => ({
  scanForInjection: vi.fn(() => ({
    blocked: false,
    triggered: false,
    score: 0,
    matches: [],
  })),
}));

vi.mock('./logger.js', () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    fatal: vi.fn(),
  },
}));

vi.mock('./db.js', () => ({
  getMessagesSince: vi.fn(() => []),
  getNewMessages: vi.fn(() => ({ messages: [], newTimestamp: '' })),
  getAllTasks: vi.fn(() => []),
  setSession: vi.fn(),
  clearSession: vi.fn(),
  getSessionLastUsedAt: vi.fn(() => undefined),
  setRegisteredGroup: vi.fn(),
  getLastCompactedAt: vi.fn(() => undefined),
  setLastCompactedAt: vi.fn(),
}));

vi.mock('./container-runner.js', () => ({
  writeTasksSnapshot: vi.fn(),
  writeGroupsSnapshot: vi.fn(),
}));

vi.mock('./router.js', () => ({
  findChannel: vi.fn(),
  formatMessages: vi.fn(() => 'formatted prompt for oracle test'),
}));

vi.mock('./session-commands.js', () => ({
  handleSessionCommand: vi.fn(async () => ({ handled: false, success: false })),
  extractSessionCommand: vi.fn(() => null),
  isSessionCommandAllowed: vi.fn(() => true),
  dispatchHostCommand: vi.fn(() => ({ matched: false })),
  HOST_COMMAND_HANDLERS: [],
}));

vi.mock('./sender-allowlist.js', () => ({
  loadSenderAllowlist: vi.fn(() => ({})),
  isTriggerAllowed: vi.fn(() => true),
}));

vi.mock('./image.js', () => ({
  parseImageReferences: vi.fn(() => []),
}));

vi.mock('./router-state.js', () => ({
  getAvailableGroups: vi.fn(() => []),
}));

vi.mock('./evolution-client.js', () => ({
  getReflections: vi.fn(async () => ({ block: '', reflectionIds: [] })),
  logInteraction: vi.fn(),
}));

vi.mock('./user-signal.js', () => ({
  detectUserSignal: vi.fn(() => null),
}));

vi.mock('./domain-presets.js', () => ({
  detectDomains: vi.fn(() => []),
}));

vi.mock('./project-registry.js', () => ({
  SENSITIVE_FILE_PATTERNS: [],
  SENSITIVE_DIR_PATTERNS: [],
  getProjectById: vi.fn(() => null),
}));

vi.mock('./mount-security.js', () => ({
  validateAdditionalMounts: vi.fn(() => []),
}));

vi.mock('./group-folder.js', () => ({
  resolveGroupFolderPath: vi.fn((f: string) => `/tmp/groups/${f}`),
  resolveGroupIpcPath: vi.fn((f: string) => `/tmp/ipc/${f}`),
}));

vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      existsSync: vi.fn(() => false),
      mkdirSync: vi.fn(),
      writeFileSync: vi.fn(),
      readdirSync: vi.fn(() => []),
      statSync: vi.fn(() => ({ isDirectory: () => false })),
      cpSync: vi.fn(),
    },
  };
});

// ── Imports (after mocks) ─────────────────────────────────────────────────────

// The NEW interface — DOES NOT EXIST YET in this form. Import failure = RED (expected).
import {
  createMessageOrchestrator,
  type OrchestratorDeps,
} from './message-orchestrator.js';
import type {
  IngressCaps,
  AdmitResult,
  AdmitEventInput,
} from './ingress/caps.js';

import { getMessagesSince, getNewMessages } from './db.js';
import { findChannel } from './router.js';
import { RuntimeRegistry } from './agent-runtimes/registry.js';
import type {
  RuntimeSession,
  RunContext,
  RunResult,
  RuntimeEventSink,
} from './agent-runtimes/types.js';
import type { RegisteredGroup } from './types.js';

const mockGetMessagesSince = vi.mocked(getMessagesSince);
const _mockGetNewMessages = vi.mocked(getNewMessages);
const mockFindChannel = vi.mocked(findChannel);

// ─── Test doubles ─────────────────────────────────────────────────────────────

/** Token count reported by the run — null simulates the "dark" (no usage) case. */
type TokenCount = number | null;

type RunTurnFn = (
  ctx: RunContext,
  session: RuntimeSession,
  sink: RuntimeEventSink,
) => Promise<RunResult>;

/**
 * Make a fake IngressCaps that records every call.
 * `admitResult` controls what tryAdmit returns.
 * `runTokens` controls what token count the run executor reports (null = dark).
 */
function makeFakeCaps(
  admitResult: AdmitResult,
  _runTokens: TokenCount = 42,
): IngressCaps & {
  tryAdmitSpy: ReturnType<typeof vi.fn>;
  releaseSpy: ReturnType<typeof vi.fn>;
  recordSpendSpy: ReturnType<typeof vi.fn>;
  capturedNow: number[];
} {
  const tryAdmitSpy = vi.fn(
    async (_event: AdmitEventInput, _now: number) => admitResult,
  );
  const releaseSpy = vi.fn();
  const recordSpendSpy = vi.fn();
  const capturedNow: number[] = [];

  return {
    tryAdmitSpy,
    releaseSpy,
    recordSpendSpy,
    capturedNow,
    async tryAdmit(event, now) {
      capturedNow.push(now);
      return tryAdmitSpy(event, now);
    },
    release() {
      releaseSpy();
    },
    recordSpend(cost, now) {
      recordSpendSpy(cost, now);
    },
    snapshot() {
      return { inUse: 0 };
    },
  };
}

function makeRegistry(runTurn?: RunTurnFn): RuntimeRegistry {
  const effective: RunTurnFn =
    runTurn ??
    (async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'oracle response' });
      await sink({
        type: 'session',
        sessionRef: { backend: 'claude', session_id: 'sess-oracle' },
      });
      await sink({ type: 'turn_complete' });
      return {
        status: 'success',
        result: 'oracle response',
        sessionRef: { backend: 'claude', session_id: 'sess-oracle' },
        // NOTE: Phase 4 spec expects a `tokenUsage` or equivalent field on RunResult.
        // The exact field name is left to the implementer; the oracle only asserts the
        // caps behaviour that observably results from the token count.
      };
    });

  const registry = new RuntimeRegistry();
  registry.register({
    name: () => 'claude' as const,
    capabilities: () => ({
      shell: true,
      filesystem: true,
      web: true,
      multimodal: true,
      handoffs: false,
      persistent_sessions: true,
      tool_streaming: true,
    }),
    startOrResume: async () => ({ backend: 'claude' as const, session_id: '' }),
    runTurn: effective,
    close: async () => {},
  });
  return registry;
}

const PUBLIC_INGRESS_JID = 'webhook:github';

const PUBLIC_INGRESS_GROUP: RegisteredGroup = {
  name: 'github-sandbox',
  folder: 'webhook-sandbox-github',
  trigger: 'always',
  added_at: '2026-01-01T00:00:00.000Z',
  containerConfig: {
    publicIngress: true,
    curatedTools: [],
  },
};

const NON_PUBLIC_GROUP: RegisteredGroup = {
  name: 'Main',
  folder: 'whatsapp_main',
  trigger: 'always',
  added_at: '2026-01-01T00:00:00.000Z',
  isControlGroup: true,
  // publicIngress absent / falsy
};

function makeMsg(
  override: Partial<{
    id: string;
    timestamp: string;
    content: string;
    sender: string;
  }> = {},
) {
  return {
    id: override.id ?? 'msg-1',
    chat_jid: PUBLIC_INGRESS_JID,
    sender: override.sender ?? 'system@webhook',
    sender_name: 'Webhook',
    content: override.content ?? '{"event":"push"}',
    timestamp: override.timestamp ?? '2026-06-19T10:00:01.000Z',
    is_from_me: false,
    is_bot_message: false,
  };
}

/** Minimal RouterState mock for a single group. */
function makeState(jid: string, group: RegisteredGroup, initialCursor = '') {
  let cursor = initialCursor;
  return {
    registeredGroups: { [jid]: group } as Record<string, RegisteredGroup>,
    getLastAgentTimestamp: vi.fn(() => cursor),
    setLastAgentTimestamp: vi.fn((_jid: string, ts: string) => {
      cursor = ts;
    }),
    save: vi.fn(),
    getSession: vi.fn(() => undefined as string | undefined),
    setSession: vi.fn(),
    clearSession: vi.fn(),
    get lastTimestamp() {
      return '';
    },
    set lastTimestamp(_: string) {},
    sessions: {} as Record<string, string>,
    getContextStats: vi.fn(() => undefined),
  };
}

function makeQueue() {
  return {
    closeStdin: vi.fn(),
    notifyIdle: vi.fn(),
    enqueueMessageCheck: vi.fn(),
    sendMessage: vi.fn(() => false as boolean),
    registerProcess: vi.fn(),
  };
}

function makeChannel(jid: string) {
  return {
    name: 'webhook',
    ownsJid: vi.fn((j: string) => j === jid),
    isConnected: vi.fn(() => true),
    sendMessage: vi.fn(async () => {}),
    setTyping: vi.fn(async () => {}),
    connect: vi.fn(async () => {}),
    disconnect: vi.fn(async () => {}),
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

// =============================================================================
// CASE D1 — admitted run: release() + recordSpend() both called exactly once
//            (finally semantics — even if the run throws)
// =============================================================================
describe('@oracle orchestrator caps wiring — admitted run calls release and recordSpend exactly once', () => {
  it('@oracle release() and recordSpend() are called exactly once after a successful admitted run', async () => {
    // @oracle: spec D — admitted run (tryAdmit → {ok:true}) → release() once AND recordSpend() once
    const caps = makeFakeCaps({ ok: true });
    const state = makeState(PUBLIC_INGRESS_JID, PUBLIC_INGRESS_GROUP);
    const channel = makeChannel(PUBLIC_INGRESS_JID);

    mockGetMessagesSince.mockReturnValue([makeMsg()]);
    mockFindChannel.mockReturnValue(channel as never);

    const deps: OrchestratorDeps & { ingressCaps?: IngressCaps } = {
      state: state as never,
      queue: makeQueue() as never,
      registry: makeRegistry(),
      channels: [channel as never],
      ingressCaps: caps, // the new optional dep
    };

    const orchestrator = createMessageOrchestrator(deps);
    // Drive processGroupMessages for the publicIngress group
    await (
      orchestrator as { processGroupMessages(jid: string): Promise<boolean> }
    ).processGroupMessages(PUBLIC_INGRESS_JID);

    expect(caps.releaseSpy).toHaveBeenCalledTimes(1);
    expect(caps.recordSpendSpy).toHaveBeenCalledTimes(1);
  });

  it('@oracle release() and recordSpend() are called even when the underlying run throws', async () => {
    // @oracle: spec D — finally semantics: release/recordSpend happen even on run error
    const caps = makeFakeCaps({ ok: true });

    const throwingRunTurn: RunTurnFn = async () => {
      throw new Error('simulated run failure');
    };

    const state = makeState(PUBLIC_INGRESS_JID, PUBLIC_INGRESS_GROUP);
    const channel = makeChannel(PUBLIC_INGRESS_JID);

    mockGetMessagesSince.mockReturnValue([makeMsg()]);
    mockFindChannel.mockReturnValue(channel as never);

    const deps: OrchestratorDeps & { ingressCaps?: IngressCaps } = {
      state: state as never,
      queue: makeQueue() as never,
      registry: makeRegistry(throwingRunTurn),
      channels: [channel as never],
      ingressCaps: caps,
    };

    const orchestrator = createMessageOrchestrator(deps);
    // Must not throw out of processGroupMessages
    await expect(
      (
        orchestrator as { processGroupMessages(jid: string): Promise<boolean> }
      ).processGroupMessages(PUBLIC_INGRESS_JID),
    ).resolves.toBeDefined();

    // finally block must have fired
    expect(caps.releaseSpy).toHaveBeenCalledTimes(1);
    expect(caps.recordSpendSpy).toHaveBeenCalledTimes(1);
  });

  it('@oracle the same `now` value is passed to both tryAdmit and recordSpend', async () => {
    // @oracle: spec D — SAME `now` for tryAdmit and recordSpend to prevent UTC-day drift
    const caps = makeFakeCaps({ ok: true });

    const state = makeState(PUBLIC_INGRESS_JID, PUBLIC_INGRESS_GROUP);
    const channel = makeChannel(PUBLIC_INGRESS_JID);

    mockGetMessagesSince.mockReturnValue([makeMsg()]);
    mockFindChannel.mockReturnValue(channel as never);

    const deps: OrchestratorDeps & { ingressCaps?: IngressCaps } = {
      state: state as never,
      queue: makeQueue() as never,
      registry: makeRegistry(),
      channels: [channel as never],
      ingressCaps: caps,
    };

    const orchestrator = createMessageOrchestrator(deps);
    await (
      orchestrator as { processGroupMessages(jid: string): Promise<boolean> }
    ).processGroupMessages(PUBLIC_INGRESS_JID);

    expect(caps.tryAdmitSpy).toHaveBeenCalledTimes(1);
    expect(caps.recordSpendSpy).toHaveBeenCalledTimes(1);

    const nowFromAdmit: number = caps.capturedNow[0]!;
    const [, nowFromSpend] = caps.recordSpendSpy.mock.calls[0] as [
      number,
      number,
    ];
    expect(nowFromSpend).toBe(nowFromAdmit);
  });
});

// =============================================================================
// CASE D2 — tryAdmit → {ok:false, reason:'inflight-cap'}: NO run, cursor IS advanced
// =============================================================================
describe('@oracle orchestrator caps wiring — rejected admit does not spawn run, cursor advances', () => {
  it('@oracle no container run when tryAdmit returns {ok:false, reason:"inflight-cap"}', async () => {
    // @oracle: spec D — tryAdmit {ok:false} → NO agent spawn; event is load-shed (cursor advanced)
    const caps = makeFakeCaps({ ok: false, reason: 'inflight-cap' });

    const runTurnSpy = vi.fn(
      async (
        _ctx: RunContext,
        _session: RuntimeSession,
        _sink: RuntimeEventSink,
      ): Promise<RunResult> => ({
        status: 'success',
        result: 'should not be called',
      }),
    );

    const state = makeState(PUBLIC_INGRESS_JID, PUBLIC_INGRESS_GROUP);
    const channel = makeChannel(PUBLIC_INGRESS_JID);
    const msg = makeMsg({ timestamp: '2026-06-19T10:00:01.000Z' });

    mockGetMessagesSince.mockReturnValue([msg]);
    mockFindChannel.mockReturnValue(channel as never);

    const deps: OrchestratorDeps & { ingressCaps?: IngressCaps } = {
      state: state as never,
      queue: makeQueue() as never,
      registry: makeRegistry(runTurnSpy),
      channels: [channel as never],
      ingressCaps: caps,
    };

    const orchestrator = createMessageOrchestrator(deps);
    await (
      orchestrator as { processGroupMessages(jid: string): Promise<boolean> }
    ).processGroupMessages(PUBLIC_INGRESS_JID);

    // No run must have been dispatched
    expect(runTurnSpy).not.toHaveBeenCalled();

    // release() must NOT be called (no slot was acquired)
    expect(caps.releaseSpy).not.toHaveBeenCalled();

    // Cursor must have advanced (the message is dropped, not retried)
    expect(state.setLastAgentTimestamp).toHaveBeenCalled();
  });

  it('@oracle release() is NOT called when tryAdmit returns {ok:false} (no slot acquired)', async () => {
    // @oracle: spec D — tryAdmit {ok:false} means no slot was acquired; release() on a never-acquired slot would be an over-release bug
    const caps = makeFakeCaps({ ok: false, reason: 'inflight-cap' });

    const state = makeState(PUBLIC_INGRESS_JID, PUBLIC_INGRESS_GROUP);
    const channel = makeChannel(PUBLIC_INGRESS_JID);

    mockGetMessagesSince.mockReturnValue([makeMsg()]);
    mockFindChannel.mockReturnValue(channel as never);

    const deps: OrchestratorDeps & { ingressCaps?: IngressCaps } = {
      state: state as never,
      queue: makeQueue() as never,
      registry: makeRegistry(),
      channels: [channel as never],
      ingressCaps: caps,
    };

    const orchestrator = createMessageOrchestrator(deps);
    await (
      orchestrator as { processGroupMessages(jid: string): Promise<boolean> }
    ).processGroupMessages(PUBLIC_INGRESS_JID);

    expect(caps.releaseSpy).not.toHaveBeenCalled();
  });
});

// =============================================================================
// CASE D3 — token-dark run: recordSpend called with NON-ZERO fallback
// =============================================================================
describe('@oracle orchestrator caps wiring — token-dark run uses non-zero fallback spend', () => {
  it('@oracle recordSpend is called with a value > 0 when run token usage is null/unknown', async () => {
    // @oracle: spec D — when the run reports null/unknown token usage, recordSpend(FALLBACK) where
    // FALLBACK > 0; a zero spend would allow an unbounded stream of usage-less runs to bypass the daily cap
    const caps = makeFakeCaps({ ok: true }, null /* dark — no token count */);

    // Run that reports no token usage (null/undefined on result)
    const darkRunTurn: RunTurnFn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'dark run response' });
      await sink({
        type: 'session',
        sessionRef: { backend: 'claude', session_id: 'dark-sess' },
      });
      await sink({ type: 'turn_complete' });
      return {
        status: 'success',
        result: 'dark run response',
        sessionRef: { backend: 'claude', session_id: 'dark-sess' },
        // intentionally NO tokenUsage / tokensUsed field — simulates dark run
      };
    };

    const state = makeState(PUBLIC_INGRESS_JID, PUBLIC_INGRESS_GROUP);
    const channel = makeChannel(PUBLIC_INGRESS_JID);

    mockGetMessagesSince.mockReturnValue([makeMsg()]);
    mockFindChannel.mockReturnValue(channel as never);

    const deps: OrchestratorDeps & { ingressCaps?: IngressCaps } = {
      state: state as never,
      queue: makeQueue() as never,
      registry: makeRegistry(darkRunTurn),
      channels: [channel as never],
      ingressCaps: caps,
    };

    const orchestrator = createMessageOrchestrator(deps);
    await (
      orchestrator as { processGroupMessages(jid: string): Promise<boolean> }
    ).processGroupMessages(PUBLIC_INGRESS_JID);

    expect(caps.recordSpendSpy).toHaveBeenCalledTimes(1);
    const [cost] = caps.recordSpendSpy.mock.calls[0] as [number, number];
    // The fallback must be a positive non-zero number so the ledger actually accumulates spend
    expect(cost).toBeGreaterThan(0);
  });
});

// =============================================================================
// CASE D4 — fail-closed: publicIngress group + no ingressCaps dep → REFUSED
// =============================================================================
describe('@oracle orchestrator caps wiring — fail-closed when ingressCaps is absent', () => {
  it('@oracle publicIngress group with no ingressCaps dep: run is REFUSED (no agent spawn), cursor advances', async () => {
    // @oracle: spec D — fail-closed: publicIngress group but ingressCaps is undefined →
    // no run spawned; the message cursor is advanced (load-shed, not retry)
    const runTurnSpy = vi.fn(
      async (
        _ctx: RunContext,
        _session: RuntimeSession,
        _sink: RuntimeEventSink,
      ): Promise<RunResult> => ({
        status: 'success',
        result: 'should not be called',
      }),
    );

    const state = makeState(PUBLIC_INGRESS_JID, PUBLIC_INGRESS_GROUP);
    const channel = makeChannel(PUBLIC_INGRESS_JID);

    mockGetMessagesSince.mockReturnValue([makeMsg()]);
    mockFindChannel.mockReturnValue(channel as never);

    // No ingressCaps provided
    const deps: OrchestratorDeps = {
      state: state as never,
      queue: makeQueue() as never,
      registry: makeRegistry(runTurnSpy),
      channels: [channel as never],
      // ingressCaps intentionally absent
    };

    const orchestrator = createMessageOrchestrator(deps);
    await (
      orchestrator as { processGroupMessages(jid: string): Promise<boolean> }
    ).processGroupMessages(PUBLIC_INGRESS_JID);

    // No run must have been dispatched (fail-closed)
    expect(runTurnSpy).not.toHaveBeenCalled();

    // Cursor must have advanced (event is dropped, not retried)
    expect(state.setLastAgentTimestamp).toHaveBeenCalled();
  });
});

// =============================================================================
// CASE D5 — tryAdmit source argument === human-readable source name
// =============================================================================
describe('@oracle orchestrator caps wiring — tryAdmit source is the human-readable name', () => {
  it('@oracle tryAdmit is called with source === "github" (not the folder or jid)', async () => {
    // @oracle: spec D — tryAdmit source argument must be the human-readable source name
    // (e.g. 'github', derived from 'webhook:github' jid), NOT the folder name
    const caps = makeFakeCaps({ ok: true });

    const state = makeState(PUBLIC_INGRESS_JID, PUBLIC_INGRESS_GROUP);
    const channel = makeChannel(PUBLIC_INGRESS_JID);

    mockGetMessagesSince.mockReturnValue([makeMsg()]);
    mockFindChannel.mockReturnValue(channel as never);

    const deps: OrchestratorDeps & { ingressCaps?: IngressCaps } = {
      state: state as never,
      queue: makeQueue() as never,
      registry: makeRegistry(),
      channels: [channel as never],
      ingressCaps: caps,
    };

    const orchestrator = createMessageOrchestrator(deps);
    await (
      orchestrator as { processGroupMessages(jid: string): Promise<boolean> }
    ).processGroupMessages(PUBLIC_INGRESS_JID);

    expect(caps.tryAdmitSpy).toHaveBeenCalledTimes(1);
    const [event] = caps.tryAdmitSpy.mock.calls[0] as [
      { source: string },
      number,
    ];
    // Source must be 'github' (the name after 'webhook:'), NOT the folder
    expect(event.source).toBe('github');
    expect(event.source).not.toBe(PUBLIC_INGRESS_GROUP.folder);
    expect(event.source).not.toBe(PUBLIC_INGRESS_JID); // not the full jid
  });
});

// =============================================================================
// CASE D7 — per-event admission: N events in a batch → N tryAdmit calls, each
//            under its own requestId, N release calls, N recordSpend calls.
//            Batching N events into a single tryAdmit would leave N-1 events
//            unaudited (R6 violation) and charge them as one (R5 bypass).
// =============================================================================
describe('@oracle orchestrator caps wiring — per-event admission for multi-event batches', () => {
  it('@oracle two publicIngress events drive tryAdmit, release, and recordSpend exactly twice, each event under its own requestId', async () => {
    // @oracle: spec LIA-315 Phase 4 per-event invariant — "Each webhook event in a pending
    // batch must be admitted, audited, and spend-charged INDIVIDUALLY — never batched into a
    // single admission." Two events → tryAdmit ×2, release ×2, recordSpend ×2.  Each tryAdmit
    // call must carry the event's own requestId so the audit log and spend ledger are per-event,
    // not per-batch. Batching would be an R5 spend bypass and R6 audit gap.
    const caps = makeFakeCaps({ ok: true });

    const evt1 = makeMsg({
      id: 'evt-1',
      timestamp: '2026-06-19T10:00:01.000Z',
    });
    const evt2 = makeMsg({
      id: 'evt-2',
      timestamp: '2026-06-19T10:00:02.000Z',
    });

    const state = makeState(PUBLIC_INGRESS_JID, PUBLIC_INGRESS_GROUP);
    const channel = makeChannel(PUBLIC_INGRESS_JID);

    mockGetMessagesSince.mockReturnValue([evt1, evt2]);
    mockFindChannel.mockReturnValue(channel as never);

    const deps: OrchestratorDeps & { ingressCaps?: IngressCaps } = {
      state: state as never,
      queue: makeQueue() as never,
      registry: makeRegistry(),
      channels: [channel as never],
      ingressCaps: caps,
    };

    const orchestrator = createMessageOrchestrator(deps);
    await (
      orchestrator as { processGroupMessages(jid: string): Promise<boolean> }
    ).processGroupMessages(PUBLIC_INGRESS_JID);

    // Each event must be individually admitted — NEVER batched into one tryAdmit call
    expect(caps.tryAdmitSpy).toHaveBeenCalledTimes(2);

    // Each tryAdmit call must carry its event's own requestId
    const [firstCall, secondCall] = caps.tryAdmitSpy.mock.calls as [
      [AdmitEventInput, number],
      [AdmitEventInput, number],
    ];
    expect(firstCall[0].requestId).toBe('evt-1');
    expect(secondCall[0].requestId).toBe('evt-2');

    // One release per event (slot is never held across the batch)
    expect(caps.releaseSpy).toHaveBeenCalledTimes(2);

    // One spend charge per event (not one charge for the whole batch)
    expect(caps.recordSpendSpy).toHaveBeenCalledTimes(2);
  });
});

// =============================================================================
// CASE D6 — NON-publicIngress group: tryAdmit/release/recordSpend NEVER called
// =============================================================================
describe('@oracle orchestrator caps wiring — non-publicIngress group bypasses caps entirely', () => {
  it('@oracle tryAdmit/release/recordSpend are never called for a non-publicIngress group', async () => {
    // @oracle: spec D — a NON-publicIngress group must use the byte-identical existing path;
    // caps must never be called so normal agent runs are not gated by the ingress caps
    const caps = makeFakeCaps({ ok: true });

    const nonPublicJid = 'group@g.us';
    const state = makeState(nonPublicJid, NON_PUBLIC_GROUP);
    const channel = {
      name: 'whatsapp',
      ownsJid: vi.fn((j: string) => j === nonPublicJid),
      isConnected: vi.fn(() => true),
      sendMessage: vi.fn(async () => {}),
      setTyping: vi.fn(async () => {}),
      connect: vi.fn(async () => {}),
      disconnect: vi.fn(async () => {}),
    };

    mockGetMessagesSince.mockReturnValue([
      {
        id: 'msg-non-public',
        chat_jid: nonPublicJid,
        sender: 'alice@s.whatsapp.net',
        sender_name: 'Alice',
        content: 'hello',
        timestamp: '2026-06-19T10:00:01.000Z',
        is_from_me: false,
        is_bot_message: false,
      },
    ]);
    mockFindChannel.mockReturnValue(channel as never);

    const deps: OrchestratorDeps & { ingressCaps?: IngressCaps } = {
      state: state as never,
      queue: makeQueue() as never,
      registry: makeRegistry(),
      channels: [channel as never],
      ingressCaps: caps, // provided, but must NOT be called for non-public group
    };

    const orchestrator = createMessageOrchestrator(deps);
    await (
      orchestrator as { processGroupMessages(jid: string): Promise<boolean> }
    ).processGroupMessages(nonPublicJid);

    // Caps must never be consulted for a normal group
    expect(caps.tryAdmitSpy).not.toHaveBeenCalled();
    expect(caps.releaseSpy).not.toHaveBeenCalled();
    expect(caps.recordSpendSpy).not.toHaveBeenCalled();
  });
});
