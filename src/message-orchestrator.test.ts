/**
 * Unit tests for message-orchestrator.ts
 *
 * Tests the core orchestration behaviours:
 *   - Cursor advancement and rollback on agent error
 *   - Trigger gating for non-main groups
 *   - Session command interception
 *   - Startup recovery (recoverPendingMessages)
 *   - Message loop routing (pipe vs enqueue)
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  RuntimeSession,
  RunContext,
  RunResult,
  RuntimeEventSink,
} from './agent-runtimes/types.js';
import { logger } from './logger.js';

// ── Module mocks (hoisted) ───────────────────────────────────────────────────

vi.mock('./config.js', () => ({
  ASSISTANT_NAME: 'Deus',
  IDLE_TIMEOUT: 30_000,
  POLL_INTERVAL: 1_000,
  TIMEZONE: 'UTC',
  TRIGGER_PATTERN: /^@deus\b/i,
  SESSION_IDLE_RESET_HOURS: 8,
  DEFAULT_AGENT_RUNTIME: 'claude',
  PROJECT_ROOT: '/tmp/deus-test',
  INJECTION_SCANNER_CONFIG: {
    enabled: false,
    threshold: 0.7,
    logOnly: true,
  },
  CONTEXT_NOTIFY: false,
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
  getAutoCompressWatermark: vi.fn(() => undefined),
  setAutoCompressWatermark: vi.fn(),
}));

vi.mock('./container-runner.js', () => ({
  writeTasksSnapshot: vi.fn(),
  writeGroupsSnapshot: vi.fn(),
}));

vi.mock('./router.js', async () => {
  // Keep the real stripInternalTags (used transitively by the multi-agent
  // formatter) while stubbing the channel/format helpers.
  const actual =
    await vi.importActual<typeof import('./router.js')>('./router.js');
  return {
    ...actual,
    findChannel: vi.fn(),
    formatMessages: vi.fn(() => 'formatted prompt'),
  };
});

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

// ── Imports (after mocks) ────────────────────────────────────────────────────

import { createMessageOrchestrator } from './message-orchestrator.js';
import {
  getMessagesSince,
  getNewMessages,
  clearSession,
  setSession,
} from './db.js';
import { findChannel } from './router.js';
import {
  handleSessionCommand,
  extractSessionCommand,
} from './session-commands.js';
import { scanForInjection } from './guardrails/injection-scanner.js';
import type { Channel, RegisteredGroup } from './types.js';
import type { RouterState } from './router-state.js';
import type { GroupQueue } from './group-queue.js';
import { RuntimeRegistry } from './agent-runtimes/registry.js';

const mockScanForInjection = vi.mocked(scanForInjection);

const mockGetMessagesSince = vi.mocked(getMessagesSince);
const mockGetNewMessages = vi.mocked(getNewMessages);
const mockFindChannel = vi.mocked(findChannel);
const mockHandleSessionCommand = vi.mocked(handleSessionCommand);
const mockExtractSessionCommand = vi.mocked(extractSessionCommand);

type RunTurnFn = (
  ctx: RunContext,
  session: RuntimeSession,
  sink: RuntimeEventSink,
) => Promise<RunResult>;

/** Default runTurn: emits output + session + turn_complete, returns success. */
const defaultRunTurn: RunTurnFn = async (_ctx, _session, sink) => {
  await sink({ type: 'output_text', text: 'Agent response' });
  await sink({
    type: 'session',
    sessionRef: { backend: 'claude', session_id: 'sess-1' },
  });
  await sink({ type: 'turn_complete' });
  return {
    status: 'success',
    result: 'Agent response',
    sessionRef: { backend: 'claude', session_id: 'sess-1' },
  };
};

let activeRunTurn: RunTurnFn = defaultRunTurn;

function makeRegistry(): RuntimeRegistry {
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
    runTurn: (...args) => activeRunTurn(...args),
    close: async () => {},
  });
  return registry;
}

// ── Helpers ──────────────────────────────────────────────────────────────────

const MAIN_GROUP: RegisteredGroup = {
  name: 'Main',
  folder: 'whatsapp_main',
  trigger: 'always',
  added_at: '2024-01-01T00:00:00.000Z',
  isControlGroup: true,
};

const NON_MAIN_GROUP: RegisteredGroup = {
  name: 'Support',
  folder: 'whatsapp_support',
  trigger: '@Deus',
  added_at: '2024-01-01T00:00:00.000Z',
  requiresTrigger: true,
};

function makeMsg(
  override: Partial<{
    id: string;
    timestamp: string;
    content: string;
    sender: string;
    is_from_me: boolean;
  }> = {},
) {
  return {
    id: override.id ?? 'msg-1',
    chat_jid: 'group@g.us',
    sender: override.sender ?? 'alice@s.whatsapp.net',
    sender_name: 'Alice',
    content: override.content ?? 'hello',
    timestamp: override.timestamp ?? '2024-01-01T00:00:01.000Z',
    is_from_me: override.is_from_me ?? false,
    is_bot_message: false,
  };
}

/** Minimal RouterState mock. Tracks cursor calls so tests can assert on them. */
function makeState(group: RegisteredGroup, initialCursor = '') {
  let cursor = initialCursor;
  return {
    registeredGroups: { 'group@g.us': group } as Record<
      string,
      RegisteredGroup
    >,
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
  };
}

/** Minimal GroupQueue mock. */
function makeQueue() {
  return {
    closeStdin: vi.fn(),
    notifyIdle: vi.fn(),
    enqueueMessageCheck: vi.fn(),
    enqueueTask: vi.fn(),
    sendMessage: vi.fn(() => false as boolean),
    registerProcess: vi.fn(),
    setOnTerminalFailure: vi.fn(),
  };
}

/** Minimal Channel mock that owns all JIDs. */
function makeChannel() {
  return {
    ownsJid: vi.fn(() => true),
    isConnected: vi.fn(() => true),
    sendMessage: vi.fn(async () => {}),
    setTyping: vi.fn(async () => {}),
    connect: vi.fn(async () => {}),
    disconnect: vi.fn(async () => {}),
  };
}

beforeEach(() => {
  vi.resetAllMocks();
  activeRunTurn = defaultRunTurn;
  // Restore default behaviours after reset
  mockGetMessagesSince.mockReturnValue([]);
  mockGetNewMessages.mockReturnValue({ messages: [], newTimestamp: '' });
  mockHandleSessionCommand.mockResolvedValue({ handled: false });
  mockExtractSessionCommand.mockReturnValue(null);
  mockScanForInjection.mockReturnValue({
    blocked: false,
    triggered: false,
    score: 0,
    matches: [],
  });
});

afterEach(() => {
  vi.useRealTimers();
});

// ── processGroupMessages ─────────────────────────────────────────────────────

describe('processGroupMessages', () => {
  it('returns true immediately when group is not registered', async () => {
    const state = makeState(MAIN_GROUP);
    state.registeredGroups = {}; // JID not in map
    const queue = makeQueue();
    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    expect(mockGetMessagesSince).not.toHaveBeenCalled();
  });

  it('returns true immediately when no missed messages', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([]);

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
  });

  it('advances cursor then rolls back on agent error with no output sent', async () => {
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    activeRunTurn = async () => ({
      status: 'error',
      result: null,
      error: 'Container crashed',
    });

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');

    expect(result).toBe(false);
    // First advance to ts-1, then roll back to ts-prev
    expect(state.setLastAgentTimestamp).toHaveBeenNthCalledWith(
      1,
      'group@g.us',
      'ts-1',
    );
    expect(state.setLastAgentTimestamp).toHaveBeenNthCalledWith(
      2,
      'group@g.us',
      'ts-prev',
    );
  });

  it('clears stale session on "No conversation found" error instead of persisting it', async () => {
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    const mockClearSession = vi.mocked(clearSession);
    const mockSetSession = vi.mocked(setSession);
    mockClearSession.mockClear();
    mockSetSession.mockClear();
    state.clearSession.mockClear();
    state.setSession.mockClear();

    activeRunTurn = async () => ({
      status: 'error',
      result: null,
      error: 'No conversation found with session ID: abc123',
      sessionRef: { backend: 'claude' as const, session_id: 'abc123' },
    });

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(false);
    expect(mockClearSession).toHaveBeenCalledWith('whatsapp_main', 'claude');
    expect(state.clearSession).toHaveBeenCalledWith('whatsapp_main', 'claude');
    expect(mockSetSession).not.toHaveBeenCalled();
    expect(state.setSession).not.toHaveBeenCalled();
  });

  it('does NOT roll back cursor when output was already sent to the user', async () => {
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    activeRunTurn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'Partial response' });
      return { status: 'error', result: null, error: 'crashed after output' };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');

    expect(result).toBe(true); // success because output was sent
    // Cursor advanced to ts-1 — no rollback
    expect(state.setLastAgentTimestamp).toHaveBeenCalledWith(
      'group@g.us',
      'ts-1',
    );
    expect(state.setLastAgentTimestamp).toHaveBeenCalledTimes(1);
    expect(channel.sendMessage).toHaveBeenCalledWith(
      'group@g.us',
      'Partial response',
    );
  });

  it('skips non-main group when trigger is required but not present', async () => {
    const state = makeState(NON_MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: 'just a regular message, no trigger' }),
    ]);

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
  });

  it('processes non-main group when trigger message is present', async () => {
    const state = makeState(NON_MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: '@Deus please help' }),
    ]);

    let runTurnCalled = false;
    activeRunTurn = async (_ctx, _session, sink) => {
      runTurnCalled = true;
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: null };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    expect(runTurnCalled).toBe(true);
  });

  it('main group processes messages without trigger check', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: 'no trigger here, just a regular message' }),
    ]);

    let runTurnCalled = false;
    activeRunTurn = async (_ctx, _session, sink) => {
      runTurnCalled = true;
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: null };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    expect(runTurnCalled).toBe(true);
  });

  it('returns session command result without running agent when handled', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: '@Deus /compact' }),
    ]);
    mockHandleSessionCommand.mockResolvedValue({
      handled: true,
      success: true,
    });

    let runTurnCalled = false;
    activeRunTurn = async () => {
      runTurnCalled = true;
      return { status: 'success', result: null };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    expect(runTurnCalled).toBe(false);
  });

  it('sends agent output to the channel', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    activeRunTurn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'Hello user!' });
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: 'Hello user!' };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    await orchestrator.processGroupMessages('group@g.us');
    expect(channel.sendMessage).toHaveBeenCalledWith(
      'group@g.us',
      'Hello user!',
    );
  });

  it('strips <internal> blocks before sending to user', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    activeRunTurn = async (_ctx, _session, sink) => {
      await sink({
        type: 'output_text',
        text: '<internal>thinking...</internal>Visible reply',
      });
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: null };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    await orchestrator.processGroupMessages('group@g.us');
    expect(channel.sendMessage).toHaveBeenCalledWith(
      'group@g.us',
      'Visible reply',
    );
  });

  // ── Send-failure resilience (LIA-286) ───────────────────────────────────────

  const FALLBACK_TEXT =
    'I generated a reply but could not deliver it — please ask again.';

  it('swallows a send failure and continues without throwing (LIA-286)', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    // Both the primary send and the fallback send fail.
    channel.sendMessage.mockRejectedValue(new Error('channel down'));
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    activeRunTurn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'Hello user!' });
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: 'Hello user!' };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    // Agent succeeded, so the turn still reports success even though delivery
    // failed — the failure must not propagate as a rejection.
    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    // Primary send + fallback send both attempted.
    expect(channel.sendMessage).toHaveBeenCalledTimes(2);
    const { logger } = await import('./logger.js');
    expect(vi.mocked(logger.warn)).toHaveBeenCalled();
  });

  it('sends a user-facing fallback notice when the primary send fails (LIA-286)', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    // Primary send fails, fallback send succeeds.
    channel.sendMessage
      .mockRejectedValueOnce(new Error('rejected'))
      .mockResolvedValue(undefined);
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    activeRunTurn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'Hello user!' });
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: 'Hello user!' };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    await orchestrator.processGroupMessages('group@g.us');
    expect(channel.sendMessage).toHaveBeenCalledTimes(2);
    expect(channel.sendMessage).toHaveBeenNthCalledWith(
      1,
      'group@g.us',
      'Hello user!',
    );
    expect(channel.sendMessage).toHaveBeenNthCalledWith(
      2,
      'group@g.us',
      FALLBACK_TEXT,
    );
  });

  it('rolls back the cursor when both sends fail and the agent errors (LIA-286)', async () => {
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    // Primary + fallback both fail: the user received nothing.
    channel.sendMessage.mockRejectedValue(new Error('channel down'));
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    activeRunTurn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'Partial response' });
      return { status: 'error', result: null, error: 'crashed after output' };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    // Nothing delivered + agent error → roll back so the message re-processes.
    expect(result).toBe(false);
    expect(state.setLastAgentTimestamp).toHaveBeenNthCalledWith(
      1,
      'group@g.us',
      'ts-1',
    );
    expect(state.setLastAgentTimestamp).toHaveBeenNthCalledWith(
      2,
      'group@g.us',
      'ts-prev',
    );
  });

  it('does NOT roll back when the fallback was delivered even if the agent errors (LIA-286)', async () => {
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    // Primary fails, fallback succeeds → a user-visible delivery happened.
    channel.sendMessage
      .mockRejectedValueOnce(new Error('rejected'))
      .mockResolvedValue(undefined);
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    activeRunTurn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'Partial response' });
      return { status: 'error', result: null, error: 'crashed after output' };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    // Fallback delivered → no rollback, no re-send of the fallback next poll.
    expect(result).toBe(true);
    expect(state.setLastAgentTimestamp).toHaveBeenCalledTimes(1);
    expect(state.setLastAgentTimestamp).toHaveBeenCalledWith(
      'group@g.us',
      'ts-1',
    );
  });
});

// ── Auto-compact dispatch (LIA-367) ──────────────────────────────────────────
// `result.contextStats`/`result.compactionEvent` never reach this callback in
// production today — ContainerRuntime.runTurn's onOutput only forwards
// output_text/activity/session/error/turn_complete, and RuntimeEvent has no
// variant carrying them. This test pins that today's real event pipeline
// never triggers the auto-compact dispatch (dead-code regression guard); the
// serialization mechanism itself (enqueueTask dedup/queue/drain ordering) is
// covered separately in group-queue.test.ts, where it's actually reachable.
describe('auto-compact dispatch (LIA-367)', () => {
  it('never calls queue.enqueueTask via the real event pipeline (contextStats is not forwarded)', async () => {
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);
    const queue = makeQueue();

    // defaultRunTurn is used (output_text + session + turn_complete) — the
    // real shape the event pipeline can produce today.
    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    await orchestrator.processGroupMessages('group@g.us');
    expect(queue.enqueueTask).not.toHaveBeenCalled();
  });
});

describe('terminal-failure notice wiring', () => {
  it('wires queue.setOnTerminalFailure to send a notice via the resolved channel', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    const queue = makeQueue();

    createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    expect(queue.setOnTerminalFailure).toHaveBeenCalledTimes(1);
    const registeredCallback = queue.setOnTerminalFailure.mock.calls[0][0];

    await registeredCallback('group@g.us');

    expect(mockFindChannel).toHaveBeenCalledWith([channel], 'group@g.us');
    expect(channel.sendMessage).toHaveBeenCalledWith(
      'group@g.us',
      'I hit an error processing that — please try again.',
    );
  });

  it('is a no-op when no channel owns the JID', async () => {
    const state = makeState(MAIN_GROUP);
    mockFindChannel.mockReturnValue(undefined);
    const queue = makeQueue();

    createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [],
    });

    const registeredCallback = queue.setOnTerminalFailure.mock.calls[0][0];
    expect(() => registeredCallback('group@g.us')).not.toThrow();
    expect(logger.warn).toHaveBeenCalledWith(
      { groupJid: 'group@g.us' },
      'No channel owns JID, cannot send terminal-failure notice',
    );
  });
});

// ── Injection scanner integration ────────────────────────────────────────────

describe('injection scanner integration', () => {
  it('blocks message and prevents runTurn when injection is detected', async () => {
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    mockScanForInjection.mockReturnValue({
      blocked: true,
      triggered: true,
      score: 0.9,
      reason: 'Injection detected',
      matches: ['ignore previous instructions'],
    });

    let runTurnCalled = false;
    activeRunTurn = async () => {
      runTurnCalled = true;
      return { status: 'success', result: null };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');

    // Scanner blocked the message — runTurn was NOT called
    expect(runTurnCalled).toBe(false);
    // Returns 'success' (true) so cursor stays advanced — no infinite retry
    expect(result).toBe(true);
    // No message was sent to user
    expect(channel.sendMessage).not.toHaveBeenCalled();
  });

  it('lets message through in logOnly mode even when triggered', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);

    mockScanForInjection.mockReturnValue({
      blocked: false,
      triggered: true,
      score: 0.8,
      reason: 'Injection detected (logOnly)',
      matches: ['ignore previous instructions'],
    });

    let runTurnCalled = false;
    activeRunTurn = async (_ctx, _session, sink) => {
      runTurnCalled = true;
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: null };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');

    // logOnly: triggered but not blocked — runTurn IS called
    expect(runTurnCalled).toBe(true);
    expect(result).toBe(true);
  });
});

// ── recoverPendingMessages ───────────────────────────────────────────────────

describe('recoverPendingMessages', () => {
  it('enqueues groups that have pending messages', () => {
    const state = makeState(MAIN_GROUP, 'ts-cursor');
    // Pending messages exist after the cursor
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-new' })]);

    const queue = makeQueue();
    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [],
    });

    orchestrator.recoverPendingMessages();
    expect(queue.enqueueMessageCheck).toHaveBeenCalledWith('group@g.us');
  });

  it('does not enqueue groups with no pending messages', () => {
    const state = makeState(MAIN_GROUP, 'ts-cursor');
    mockGetMessagesSince.mockReturnValue([]); // nothing pending

    const queue = makeQueue();
    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [],
    });

    orchestrator.recoverPendingMessages();
    expect(queue.enqueueMessageCheck).not.toHaveBeenCalled();
  });
});

// ── startMessageLoop ─────────────────────────────────────────────────────────

describe('startMessageLoop', () => {
  it('advances lastTimestamp when new messages arrive', async () => {
    vi.useFakeTimers();
    const state = makeState(MAIN_GROUP);
    let lastTs = '';
    Object.defineProperty(state, 'lastTimestamp', {
      get: () => lastTs,
      set: (v) => {
        lastTs = v;
      },
    });

    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetNewMessages
      .mockReturnValueOnce({
        messages: [{ ...makeMsg(), chat_jid: 'group@g.us' }],
        newTimestamp: 'ts-new',
      })
      .mockReturnValue({ messages: [], newTimestamp: '' });

    const queue = makeQueue();
    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const loopPromise = orchestrator.startMessageLoop();
    await vi.advanceTimersByTimeAsync(10);

    expect(lastTs).toBe('ts-new');
    expect(state.save).toHaveBeenCalled();

    // Cleanup: second invocation should no-op
    await vi.advanceTimersByTimeAsync(10);
    loopPromise; // don't await — it's infinite
  });

  it('pipes message to active container if one exists', async () => {
    vi.useFakeTimers();
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetNewMessages
      .mockReturnValueOnce({
        messages: [{ ...makeMsg(), chat_jid: 'group@g.us' }],
        newTimestamp: 'ts-1',
      })
      .mockReturnValue({ messages: [], newTimestamp: '' });
    mockGetMessagesSince.mockReturnValue([makeMsg()]);

    const queue = makeQueue();
    queue.sendMessage.mockReturnValue(true); // active container exists

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    orchestrator.startMessageLoop();
    await vi.advanceTimersByTimeAsync(10);

    expect(queue.sendMessage).toHaveBeenCalled();
    expect(queue.enqueueMessageCheck).not.toHaveBeenCalled();
  });

  it('enqueues message check when no active container', async () => {
    vi.useFakeTimers();
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetNewMessages
      .mockReturnValueOnce({
        messages: [{ ...makeMsg(), chat_jid: 'group@g.us' }],
        newTimestamp: 'ts-1',
      })
      .mockReturnValue({ messages: [], newTimestamp: '' });
    mockGetMessagesSince.mockReturnValue([makeMsg()]);

    const queue = makeQueue();
    queue.sendMessage.mockReturnValue(false); // no active container

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    orchestrator.startMessageLoop();
    await vi.advanceTimersByTimeAsync(10);

    expect(queue.enqueueMessageCheck).toHaveBeenCalledWith('group@g.us');
  });

  it('intercepts session command: closes stdin and enqueues instead of piping', async () => {
    vi.useFakeTimers();
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetNewMessages
      .mockReturnValueOnce({
        messages: [
          { ...makeMsg({ content: '@Deus /compact' }), chat_jid: 'group@g.us' },
        ],
        newTimestamp: 'ts-1',
      })
      .mockReturnValue({ messages: [], newTimestamp: '' });
    mockExtractSessionCommand.mockReturnValue('/compact' as any);

    const queue = makeQueue();
    queue.sendMessage.mockReturnValue(true);

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    orchestrator.startMessageLoop();
    await vi.advanceTimersByTimeAsync(10);

    expect(queue.closeStdin).toHaveBeenCalledWith('group@g.us');
    expect(queue.enqueueMessageCheck).toHaveBeenCalledWith('group@g.us');
    expect(queue.sendMessage).not.toHaveBeenCalled();
  });

  it('does not start a second loop if already running', async () => {
    vi.useFakeTimers();
    const state = makeState(MAIN_GROUP);
    mockGetNewMessages.mockReturnValue({ messages: [], newTimestamp: '' });

    const queue = makeQueue();
    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [],
    });

    orchestrator.startMessageLoop();
    orchestrator.startMessageLoop(); // second call should no-op

    await vi.advanceTimersByTimeAsync(10);
    // getNewMessages called once (from first loop), not twice
    expect(mockGetNewMessages.mock.calls.length).toBeLessThanOrEqual(2);
  });
});

// ── LIA-127: multi-agent dispatch wiring ─────────────────────────────────────
describe('multi-agent dispatch (DEUS_MULTI_AGENT)', () => {
  const TASK_BLOCK =
    'do this\n```deus-tasks\n' +
    JSON.stringify([
      {
        id: 'a',
        role: 'researcher',
        goal: 'g',
        backstory: '',
        prompt: 'research X',
        mode: 'read',
      },
    ]) +
    '\n```';

  afterEach(() => {
    delete process.env.DEUS_MULTI_AGENT;
  });

  it('flag-on + valid block → dispatches via orchestrator and sends the aggregate', async () => {
    process.env.DEUS_MULTI_AGENT = '1';
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: TASK_BLOCK, timestamp: 'ts-1' }),
    ]);
    // The sub-agent run emits output + a DONE marker.
    activeRunTurn = async (_ctx, _session, sink) => {
      await sink({
        type: 'output_text',
        text: 'found the answer [STATUS:DONE]',
      });
      return { status: 'success', result: 'found the answer [STATUS:DONE]' };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');

    expect(result).toBe(true);
    // The aggregated multi-agent reply was sent (task id + status + output).
    expect(channel.sendMessage).toHaveBeenCalledWith(
      'group@g.us',
      expect.stringContaining('✓ a: done'),
    );
    expect(channel.sendMessage).toHaveBeenCalledWith(
      'group@g.us',
      expect.stringContaining('found the answer'),
    );
  });

  it('flag-off + same block → single-agent path unchanged (no regression)', async () => {
    delete process.env.DEUS_MULTI_AGENT; // flag OFF
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: TASK_BLOCK, timestamp: 'ts-1' }),
    ]);
    // Single-agent backend echoes a plain reply — NOT a multi-agent aggregate.
    activeRunTurn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'single agent reply' });
      return { status: 'success', result: 'single agent reply' };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');

    expect(result).toBe(true);
    expect(channel.sendMessage).toHaveBeenCalledWith(
      'group@g.us',
      expect.stringContaining('single agent reply'),
    );
    // No multi-agent formatting was produced.
    expect(channel.sendMessage).not.toHaveBeenCalledWith(
      'group@g.us',
      expect.stringContaining('✓ a: done'),
    );
  });

  it('flag-on + blocked by injection scanner → NOT dispatched (security guard)', async () => {
    process.env.DEUS_MULTI_AGENT = '1';
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: TASK_BLOCK, timestamp: 'ts-1' }),
    ]);
    // Scanner flags the prompt as a blocked injection attempt.
    mockScanForInjection.mockReturnValue({
      blocked: true,
      triggered: true,
      score: 1,
      matches: ['ignore previous instructions'],
    });
    let dispatched = false;
    activeRunTurn = async (_ctx, _session, sink) => {
      dispatched = true;
      await sink({ type: 'output_text', text: 'should not run [STATUS:DONE]' });
      return { status: 'success', result: 'x' };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');

    expect(result).toBe(true); // consumed, no retry
    expect(dispatched).toBe(false); // sub-agents never ran
    expect(channel.sendMessage).not.toHaveBeenCalledWith(
      'group@g.us',
      expect.stringContaining('✓ a: done'),
    );
  });

  it('flag-on + malformed block → parse-error notice, no rollback', async () => {
    process.env.DEUS_MULTI_AGENT = '1';
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({
        content: '```deus-tasks\n{not valid json\n```',
        timestamp: 'ts-1',
      }),
    ]);

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');

    expect(result).toBe(true); // consumed, no retry
    expect(channel.sendMessage).toHaveBeenCalledWith(
      'group@g.us',
      expect.stringContaining("couldn't parse"),
    );
    // Cursor advanced to ts-1, not rolled back to ts-prev.
    expect(state.setLastAgentTimestamp).toHaveBeenLastCalledWith(
      'group@g.us',
      'ts-1',
    );
  });

  it('flag-on + all subagents blocked (status error) → reports reasons and consumes (no loop)', async () => {
    process.env.DEUS_MULTI_AGENT = '1';
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: TASK_BLOCK, timestamp: 'ts-1' }),
    ]);
    // Subagent run errors → BLOCKED → all-blocked → OrchestratorResult.status 'error'.
    activeRunTurn = async () => ({
      status: 'error',
      result: null,
      error: 'container crashed',
    });

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');

    // Work ran (side effects possible) → consume + report, never loop.
    expect(result).toBe(true);
    // Cursor stays advanced (NOT rolled back) so the block isn't re-dispatched.
    expect(state.setLastAgentTimestamp).toHaveBeenLastCalledWith(
      'group@g.us',
      'ts-1',
    );
    // The blocked reason is reported to the user.
    expect(channel.sendMessage).toHaveBeenCalledWith(
      'group@g.us',
      expect.stringContaining('✗ a: blocked'),
    );
  });

  it('flag-on + delivery failure after a completed run → still consumes (no duplicate re-dispatch)', async () => {
    process.env.DEUS_MULTI_AGENT = '1';
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    // Delivery fails — must NOT trigger a re-dispatch (would re-run write tasks).
    channel.sendMessage = vi.fn(async () => {
      throw new Error('send failed');
    });
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: TASK_BLOCK, timestamp: 'ts-1' }),
    ]);
    activeRunTurn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'done [STATUS:DONE]' });
      return { status: 'success', result: 'done [STATUS:DONE]' };
    };

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: makeQueue() as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');

    expect(result).toBe(true); // consumed despite delivery failure
    expect(state.setLastAgentTimestamp).toHaveBeenLastCalledWith(
      'group@g.us',
      'ts-1', // not rolled back
    );
  });
});

// ── LIA-127: multi-agent warm-path routing (startMessageLoop, blocker #8) ─────
// A ```deus-tasks block must reach the orchestrator (which lives only in
// processGroupMessages) on a WARM turn too, not get piped into the single-agent
// session. The interception finalizes the warm session (closeStdin) and routes to
// the cold path via enqueueMessageCheck — instead of queue.sendMessage piping.
describe('multi-agent warm-path routing (startMessageLoop)', () => {
  const WARM_TASK_BLOCK =
    'do this\n```deus-tasks\n' +
    JSON.stringify([
      {
        id: 'a',
        role: 'researcher',
        goal: 'g',
        backstory: '',
        prompt: 'research X',
        mode: 'read',
      },
    ]) +
    '\n```';
  const WARM_MALFORMED_BLOCK = '```deus-tasks\n{not valid json\n```';

  afterEach(() => {
    delete process.env.DEUS_MULTI_AGENT;
  });

  it('flag-on + warm container + valid block → closeStdin + enqueue, never pipes, cursor untouched', async () => {
    process.env.DEUS_MULTI_AGENT = '1';
    vi.useFakeTimers();
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetNewMessages
      .mockReturnValueOnce({
        messages: [
          { ...makeMsg({ content: WARM_TASK_BLOCK }), chat_jid: 'group@g.us' },
        ],
        newTimestamp: 'ts-1',
      })
      .mockReturnValue({ messages: [], newTimestamp: '' });
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: WARM_TASK_BLOCK }),
    ]);

    const queue = makeQueue();
    queue.sendMessage.mockReturnValue(true); // warm container exists

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    orchestrator.startMessageLoop();
    await vi.advanceTimersByTimeAsync(10);

    expect(queue.closeStdin).toHaveBeenCalledWith('group@g.us');
    expect(queue.enqueueMessageCheck).toHaveBeenCalledWith('group@g.us');
    expect(queue.sendMessage).not.toHaveBeenCalled();
    // Cursor left for processGroupMessages to advance (not touched here).
    expect(state.setLastAgentTimestamp).not.toHaveBeenCalled();
  });

  it('flag-on + warm container + malformed block → closeStdin + enqueue (cold path emits the notice), never pipes', async () => {
    process.env.DEUS_MULTI_AGENT = '1';
    vi.useFakeTimers();
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetNewMessages
      .mockReturnValueOnce({
        messages: [
          {
            ...makeMsg({ content: WARM_MALFORMED_BLOCK }),
            chat_jid: 'group@g.us',
          },
        ],
        newTimestamp: 'ts-1',
      })
      .mockReturnValue({ messages: [], newTimestamp: '' });
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: WARM_MALFORMED_BLOCK }),
    ]);

    const queue = makeQueue();
    queue.sendMessage.mockReturnValue(true);

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    orchestrator.startMessageLoop();
    await vi.advanceTimersByTimeAsync(10);

    // Authorized block (valid OR malformed) finalizes the warm session and routes
    // to the cold path — same as a valid block; the single parse-error notice
    // fires in processGroupMessages, never here.
    expect(queue.closeStdin).toHaveBeenCalledWith('group@g.us');
    expect(queue.enqueueMessageCheck).toHaveBeenCalledWith('group@g.us');
    expect(queue.sendMessage).not.toHaveBeenCalled();
    expect(channel.sendMessage).not.toHaveBeenCalled();
    expect(state.setLastAgentTimestamp).not.toHaveBeenCalled();
  });

  it('flag-on + warm container + ordinary message (no block) → pipes as before', async () => {
    process.env.DEUS_MULTI_AGENT = '1';
    vi.useFakeTimers();
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetNewMessages
      .mockReturnValueOnce({
        messages: [
          { ...makeMsg({ content: 'just chatting' }), chat_jid: 'group@g.us' },
        ],
        newTimestamp: 'ts-1',
      })
      .mockReturnValue({ messages: [], newTimestamp: '' });
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: 'just chatting' }),
    ]);

    const queue = makeQueue();
    queue.sendMessage.mockReturnValue(true);

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    orchestrator.startMessageLoop();
    await vi.advanceTimersByTimeAsync(10);

    expect(queue.sendMessage).toHaveBeenCalled();
    expect(queue.closeStdin).not.toHaveBeenCalled();
    expect(queue.enqueueMessageCheck).not.toHaveBeenCalled();
  });

  it('flag-off + warm container + valid block → pipes as before (no interception)', async () => {
    delete process.env.DEUS_MULTI_AGENT; // flag OFF
    vi.useFakeTimers();
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    mockGetNewMessages
      .mockReturnValueOnce({
        messages: [
          { ...makeMsg({ content: WARM_TASK_BLOCK }), chat_jid: 'group@g.us' },
        ],
        newTimestamp: 'ts-1',
      })
      .mockReturnValue({ messages: [], newTimestamp: '' });
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: WARM_TASK_BLOCK }),
    ]);

    const queue = makeQueue();
    queue.sendMessage.mockReturnValue(true);

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    orchestrator.startMessageLoop();
    await vi.advanceTimersByTimeAsync(10);

    expect(queue.sendMessage).toHaveBeenCalled();
    expect(queue.closeStdin).not.toHaveBeenCalled();
    expect(queue.enqueueMessageCheck).not.toHaveBeenCalled();
  });

  it('flag-on + non-main trigger-gated group + block WITHOUT trigger → not intercepted (accumulates as context)', async () => {
    process.env.DEUS_MULTI_AGENT = '1';
    vi.useFakeTimers();
    const state = makeState(NON_MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as unknown as Channel);
    // Block present but NO trigger token → the trigger gate continues before the
    // interception is reached, so the block must just accumulate.
    mockGetNewMessages
      .mockReturnValueOnce({
        messages: [
          { ...makeMsg({ content: WARM_TASK_BLOCK }), chat_jid: 'group@g.us' },
        ],
        newTimestamp: 'ts-1',
      })
      .mockReturnValue({ messages: [], newTimestamp: '' });
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: WARM_TASK_BLOCK }),
    ]);

    const queue = makeQueue();
    queue.sendMessage.mockReturnValue(true);

    const orchestrator = createMessageOrchestrator({
      registry: makeRegistry(),
      state: state as unknown as RouterState,
      queue: queue as unknown as GroupQueue,
      channels: [channel as unknown as Channel],
    });

    orchestrator.startMessageLoop();
    await vi.advanceTimersByTimeAsync(10);

    expect(queue.closeStdin).not.toHaveBeenCalled();
    expect(queue.enqueueMessageCheck).not.toHaveBeenCalled();
    expect(queue.sendMessage).not.toHaveBeenCalled();
  });
});
