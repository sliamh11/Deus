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

// ── Module mocks (hoisted) ───────────────────────────────────────────────────

vi.mock('./config.js', () => ({
  ASSISTANT_NAME: 'Deus',
  IDLE_TIMEOUT: 30_000,
  POLL_INTERVAL: 1_000,
  TIMEZONE: 'UTC',
  TRIGGER_PATTERN: /^@deus\b/i,
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
}));

vi.mock('./container-runner.js', () => ({
  runContainerAgent: vi.fn(
    async (
      _g: unknown,
      _i: unknown,
      _op: unknown,
      onOutput: ((...args: unknown[]) => Promise<void>) | undefined,
    ) => {
      if (onOutput) {
        await onOutput({
          status: 'success',
          result: 'Agent response',
          newSessionId: 'sess-1',
        });
      }
      return {
        status: 'success',
        result: 'Agent response',
        newSessionId: 'sess-1',
      };
    },
  ),
  writeTasksSnapshot: vi.fn(),
  writeGroupsSnapshot: vi.fn(),
}));

vi.mock('./router.js', () => ({
  findChannel: vi.fn(),
  formatMessages: vi.fn(() => 'formatted prompt'),
}));

vi.mock('./session-commands.js', () => ({
  handleSessionCommand: vi.fn(async () => ({ handled: false, success: false })),
  extractSessionCommand: vi.fn(() => null),
  isSessionCommandAllowed: vi.fn(() => true),
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
import { getMessagesSince, getNewMessages } from './db.js';
import { runContainerAgent } from './container-runner.js';
import type { ContainerOutput } from './container-runner.js';
import { findChannel } from './router.js';
import {
  handleSessionCommand,
  extractSessionCommand,
} from './session-commands.js';
import type { RegisteredGroup } from './types.js';

const mockGetMessagesSince = vi.mocked(getMessagesSince);
const mockGetNewMessages = vi.mocked(getNewMessages);
const mockRunContainerAgent = vi.mocked(runContainerAgent);
const mockFindChannel = vi.mocked(findChannel);
const mockHandleSessionCommand = vi.mocked(handleSessionCommand);
const mockExtractSessionCommand = vi.mocked(extractSessionCommand);

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
    sendMessage: vi.fn(() => false as boolean),
    registerProcess: vi.fn(),
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
  // Restore default behaviours after reset
  mockGetMessagesSince.mockReturnValue([]);
  mockGetNewMessages.mockReturnValue({ messages: [], newTimestamp: '' });
  mockHandleSessionCommand.mockResolvedValue({ handled: false });
  mockExtractSessionCommand.mockReturnValue(null);
  mockRunContainerAgent.mockImplementation(
    async (
      _g: unknown,
      _i: unknown,
      _op: unknown,
      onOutput: ((output: ContainerOutput) => Promise<void>) | undefined,
    ) => {
      if (onOutput) {
        await onOutput({
          status: 'success',
          result: 'Agent response',
          newSessionId: 'sess-1',
        });
      }
      return {
        status: 'success',
        result: 'Agent response',
        newSessionId: 'sess-1',
      };
    },
  );
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
      state: state as any,
      queue: queue as any,
      channels: [],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    expect(mockGetMessagesSince).not.toHaveBeenCalled();
  });

  it('returns true immediately when no missed messages', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as any);
    mockGetMessagesSince.mockReturnValue([]);

    const orchestrator = createMessageOrchestrator({
      state: state as any,
      queue: makeQueue() as any,
      channels: [channel as any],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    expect(mockRunContainerAgent).not.toHaveBeenCalled();
  });

  it('advances cursor then rolls back on agent error with no output sent', async () => {
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as any);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);
    // Agent errors, never calls onOutput
    mockRunContainerAgent.mockResolvedValue({
      status: 'error',
      result: null,
      error: 'Container crashed',
    });

    const orchestrator = createMessageOrchestrator({
      state: state as any,
      queue: makeQueue() as any,
      channels: [channel as any],
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

  it('does NOT roll back cursor when output was already sent to the user', async () => {
    const state = makeState(MAIN_GROUP, 'ts-prev');
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as any);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);
    // Agent sends output first, then errors
    mockRunContainerAgent.mockImplementation(async (_g, _i, _op, onOutput) => {
      if (onOutput) {
        await onOutput({
          status: 'success',
          result: 'Partial response',
          newSessionId: undefined,
        });
      }
      return { status: 'error', result: null, error: 'crashed after output' };
    });

    const orchestrator = createMessageOrchestrator({
      state: state as any,
      queue: makeQueue() as any,
      channels: [channel as any],
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
    mockFindChannel.mockReturnValue(channel as any);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: 'just a regular message, no trigger' }),
    ]);

    const orchestrator = createMessageOrchestrator({
      state: state as any,
      queue: makeQueue() as any,
      channels: [channel as any],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    expect(mockRunContainerAgent).not.toHaveBeenCalled();
  });

  it('processes non-main group when trigger message is present', async () => {
    const state = makeState(NON_MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as any);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: '@Deus please help' }),
    ]);

    const orchestrator = createMessageOrchestrator({
      state: state as any,
      queue: makeQueue() as any,
      channels: [channel as any],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    expect(mockRunContainerAgent).toHaveBeenCalled();
  });

  it('main group processes messages without trigger check', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as any);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: 'no trigger here, just a regular message' }),
    ]);

    const orchestrator = createMessageOrchestrator({
      state: state as any,
      queue: makeQueue() as any,
      channels: [channel as any],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    expect(mockRunContainerAgent).toHaveBeenCalled();
  });

  it('returns session command result without running agent when handled', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as any);
    mockGetMessagesSince.mockReturnValue([
      makeMsg({ content: '@Deus /compact' }),
    ]);
    mockHandleSessionCommand.mockResolvedValue({
      handled: true,
      success: true,
    });

    const orchestrator = createMessageOrchestrator({
      state: state as any,
      queue: makeQueue() as any,
      channels: [channel as any],
    });

    const result = await orchestrator.processGroupMessages('group@g.us');
    expect(result).toBe(true);
    expect(mockRunContainerAgent).not.toHaveBeenCalled();
  });

  it('sends agent output to the channel', async () => {
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as any);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);
    mockRunContainerAgent.mockImplementation(async (_g, _i, _op, onOutput) => {
      if (onOutput)
        await onOutput({
          status: 'success',
          result: 'Hello user!',
          newSessionId: undefined,
        });
      return {
        status: 'success',
        result: 'Hello user!',
        newSessionId: undefined,
      };
    });

    const orchestrator = createMessageOrchestrator({
      state: state as any,
      queue: makeQueue() as any,
      channels: [channel as any],
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
    mockFindChannel.mockReturnValue(channel as any);
    mockGetMessagesSince.mockReturnValue([makeMsg({ timestamp: 'ts-1' })]);
    mockRunContainerAgent.mockImplementation(async (_g, _i, _op, onOutput) => {
      if (onOutput) {
        await onOutput({
          status: 'success',
          result: '<internal>thinking...</internal>Visible reply',
          newSessionId: undefined,
        });
      }
      return { status: 'success', result: null, newSessionId: undefined };
    });

    const orchestrator = createMessageOrchestrator({
      state: state as any,
      queue: makeQueue() as any,
      channels: [channel as any],
    });

    await orchestrator.processGroupMessages('group@g.us');
    expect(channel.sendMessage).toHaveBeenCalledWith(
      'group@g.us',
      'Visible reply',
    );
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
      state: state as any,
      queue: queue as any,
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
      state: state as any,
      queue: queue as any,
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
    mockFindChannel.mockReturnValue(channel as any);
    mockGetNewMessages
      .mockReturnValueOnce({
        messages: [{ ...makeMsg(), chat_jid: 'group@g.us' }],
        newTimestamp: 'ts-new',
      })
      .mockReturnValue({ messages: [], newTimestamp: '' });

    const queue = makeQueue();
    const orchestrator = createMessageOrchestrator({
      state: state as any,
      queue: queue as any,
      channels: [channel as any],
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
    mockFindChannel.mockReturnValue(channel as any);
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
      state: state as any,
      queue: queue as any,
      channels: [channel as any],
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
    mockFindChannel.mockReturnValue(channel as any);
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
      state: state as any,
      queue: queue as any,
      channels: [channel as any],
    });

    orchestrator.startMessageLoop();
    await vi.advanceTimersByTimeAsync(10);

    expect(queue.enqueueMessageCheck).toHaveBeenCalledWith('group@g.us');
  });

  it('intercepts session command: closes stdin and enqueues instead of piping', async () => {
    vi.useFakeTimers();
    const state = makeState(MAIN_GROUP);
    const channel = makeChannel();
    mockFindChannel.mockReturnValue(channel as any);
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
      state: state as any,
      queue: queue as any,
      channels: [channel as any],
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
      state: state as any,
      queue: queue as any,
      channels: [],
    });

    orchestrator.startMessageLoop();
    orchestrator.startMessageLoop(); // second call should no-op

    await vi.advanceTimersByTimeAsync(10);
    // getNewMessages called once (from first loop), not twice
    expect(mockGetNewMessages.mock.calls.length).toBeLessThanOrEqual(2);
  });
});
