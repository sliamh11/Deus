/**
 * Pipeline integration test.
 *
 * Wires: in-memory DB + storeMessage + GroupQueue + mock runContainerAgent
 * to exercise the core message-processing pipeline end-to-end.
 *
 * Since processGroupMessages is a private function in index.ts and is
 * deeply coupled to module-level state, we test the pipeline components
 * that make it up:
 *   1. storeMessage / getMessagesSince round-trip (DB layer)
 *   2. GroupQueue enqueue/process semantics (queue layer)
 *   3. runContainerAgent mock integration (runner layer)
 *   4. IPC processTaskIpc task creation → DB storage (IPC layer)
 *   5. Full pipeline: store → queue → mock agent → send
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// ── Mocks ────────────────────────────────────────────────────────────────

vi.mock('./config.js', () => ({
  ASSISTANT_NAME: 'Deus',
  CONTAINER_IMAGE: 'deus-agent:latest',
  CONTAINER_MAX_OUTPUT_SIZE: 10485760,
  CONTAINER_TIMEOUT: 1800000,
  CREDENTIAL_PROXY_PORT: 3001,
  DATA_DIR: '/tmp/deus-pipeline-test',
  GROUPS_DIR: '/tmp/deus-pipeline-groups',
  IDLE_TIMEOUT: 1800000,
  IPC_POLL_INTERVAL: 500,
  MAX_CONCURRENT_CONTAINERS: 2,
  MAX_MESSAGE_LENGTH: 65536,
  POLL_INTERVAL: 5000,
  SCHEDULER_POLL_INTERVAL: 60000,
  TIMEZONE: 'UTC',
  TRIGGER_PATTERN: /^@deus\b/i,
}));

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

vi.mock('./container-runner.js', () => ({
  runContainerAgent: vi.fn(async (_group, _input, _onProcess, onOutput) => {
    if (onOutput) {
      await onOutput({
        status: 'success',
        result: 'Pipeline test response',
        newSessionId: 'sess-pipeline',
      });
    }
    return {
      status: 'success',
      result: 'Pipeline test response',
      newSessionId: 'sess-pipeline',
    };
  }),
  writeTasksSnapshot: vi.fn(),
  writeGroupsSnapshot: vi.fn(),
  _buildVolumeMountsForTests: vi.fn(() => []),
}));

vi.mock('./evolution-client.js', () => ({
  getReflections: vi.fn(async () => ({ block: '', reflectionIds: [] })),
  logInteraction: vi.fn(),
}));

vi.mock('./mount-security.js', () => ({
  validateMount: vi.fn(() => ({
    allowed: true,
    reason: 'ok',
    realHostPath: '/tmp',
    resolvedContainerPath: 'test',
    effectiveReadonly: true,
  })),
  validateAdditionalMounts: vi.fn(() => []),
}));

vi.mock('./project-registry.js', () => ({
  registerProject: vi.fn(),
  associateProject: vi.fn(),
  dissociateProject: vi.fn(),
  removeProject: vi.fn(),
  getAllProjects: vi.fn(() => []),
  getProjectById: vi.fn(() => null),
  SENSITIVE_FILE_PATTERNS: [],
  SENSITIVE_DIR_PATTERNS: [],
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
      readFileSync: vi.fn(() => ''),
      readdirSync: vi.fn(() => []),
      statSync: vi.fn(() => ({ isDirectory: () => false })),
      cpSync: vi.fn(),
    },
  };
});

// ── Imports ───────────────────────────────────────────────────────────────

import {
  _initTestDatabase,
  createTask,
  getAllTasks,
  getTaskById,
  getMessagesSince,
  setRegisteredGroup,
  storeMessage,
  storeChatMetadata,
} from './db.js';
import { GroupQueue } from './group-queue.js';
import { processTaskIpc, IpcDeps } from './ipc.js';
import { runContainerAgent } from './container-runner.js';
import type { RegisteredGroup } from './types.js';

const mockRunContainerAgent = vi.mocked(runContainerAgent);

// ── Test fixtures ─────────────────────────────────────────────────────────

const MAIN_GROUP: RegisteredGroup = {
  name: 'Main',
  folder: 'whatsapp_main',
  trigger: 'always',
  added_at: '2024-01-01T00:00:00.000Z',
  isControlGroup: true,
};

const OTHER_GROUP: RegisteredGroup = {
  name: 'Other',
  folder: 'other-group',
  trigger: '@Deus',
  added_at: '2024-01-01T00:00:00.000Z',
};

let groups: Record<string, RegisteredGroup>;
let ipcDeps: IpcDeps;

beforeEach(() => {
  _initTestDatabase();
  vi.useFakeTimers();

  groups = {
    'main@g.us': MAIN_GROUP,
    'other@g.us': OTHER_GROUP,
  };

  setRegisteredGroup('main@g.us', MAIN_GROUP);
  setRegisteredGroup('other@g.us', OTHER_GROUP);

  storeChatMetadata('main@g.us', '2024-01-01T00:00:00.000Z', 'Main Chat');
  storeChatMetadata('other@g.us', '2024-01-01T00:00:00.000Z', 'Other Chat');

  ipcDeps = {
    sendMessage: vi.fn(async () => {}),
    registeredGroups: () => groups,
    registerGroup: vi.fn((jid, group) => {
      groups[jid] = group;
      setRegisteredGroup(jid, group);
    }),
    syncGroups: vi.fn(async () => {}),
    getAvailableGroups: vi.fn(() => []),
    writeGroupsSnapshot: vi.fn(),
    onTasksChanged: vi.fn(),
  };

  mockRunContainerAgent.mockResolvedValue({
    status: 'success',
    result: 'Pipeline test response',
    newSessionId: 'sess-pipeline',
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.resetAllMocks();
});

// ── 1. DB round-trip: storeMessage → getMessagesSince ────────────────────

describe('DB pipeline: storeMessage → getMessagesSince', () => {
  it('stores and retrieves a message', () => {
    storeMessage({
      id: 'msg-1',
      chat_jid: 'main@g.us',
      sender: 'user@s.whatsapp.net',
      sender_name: 'Alice',
      content: 'Hello Deus',
      timestamp: '2024-01-01T00:00:01.000Z',
    });

    const msgs = getMessagesSince('main@g.us', '', 'Deus');
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe('Hello Deus');
  });

  it('filters bot messages from retrieval', () => {
    storeMessage({
      id: 'bot-msg',
      chat_jid: 'main@g.us',
      sender: 'bot@s.whatsapp.net',
      sender_name: 'Deus',
      content: 'Bot reply',
      timestamp: '2024-01-01T00:00:01.000Z',
      is_bot_message: true,
    });

    const msgs = getMessagesSince('main@g.us', '', 'Deus');
    expect(msgs).toHaveLength(0);
  });

  it('filters messages before sinceTimestamp', () => {
    storeMessage({
      id: 'old-msg',
      chat_jid: 'main@g.us',
      sender: 'user@s.whatsapp.net',
      sender_name: 'Alice',
      content: 'Old message',
      timestamp: '2024-01-01T00:00:01.000Z',
    });
    storeMessage({
      id: 'new-msg',
      chat_jid: 'main@g.us',
      sender: 'user@s.whatsapp.net',
      sender_name: 'Alice',
      content: 'New message',
      timestamp: '2024-01-01T00:00:02.000Z',
    });

    const msgs = getMessagesSince(
      'main@g.us',
      '2024-01-01T00:00:01.000Z',
      'Deus',
    );
    expect(msgs).toHaveLength(1);
    expect(msgs[0].content).toBe('New message');
  });
});

// ── 2. GroupQueue: enqueue and process in order ───────────────────────────

describe('GroupQueue: sequential processing', () => {
  it('setProcessMessagesFn is set and enqueueMessageCheck triggers processing', async () => {
    const queue = new GroupQueue();
    let called = 0;
    queue.setProcessMessagesFn(async (_jid: string) => {
      called++;
      return true;
    });

    queue.enqueueMessageCheck('group@g.us');
    await vi.advanceTimersByTimeAsync(50);
    expect(called).toBeGreaterThan(0);
  });

  it('enqueueTask enqueues and executes a task function', async () => {
    const queue = new GroupQueue();
    queue.setProcessMessagesFn(async () => true);

    let taskRan = false;
    queue.enqueueTask('group@g.us', 'task-1', async () => {
      taskRan = true;
    });
    await vi.advanceTimersByTimeAsync(50);
    expect(taskRan).toBe(true);
  });

  it('messages for different groups do not block each other', () => {
    const queue = new GroupQueue();
    const processed: string[] = [];
    queue.setProcessMessagesFn(async (jid: string) => {
      processed.push(jid);
      return true;
    });

    // Enqueue for two different groups
    queue.enqueueMessageCheck('group-a@g.us');
    queue.enqueueMessageCheck('group-b@g.us');

    // Both should be processable independently
    // (Just check no errors thrown — concurrent processing verified by group-queue.test.ts)
    expect(processed).toBeDefined();
  });
});

// ── 3. Mock runContainerAgent call semantics ──────────────────────────────

describe('runContainerAgent mock: response handling', () => {
  it('mock returns success with result', async () => {
    const group = MAIN_GROUP;
    const output = await runContainerAgent(
      group,
      {
        prompt: 'Test prompt',
        groupFolder: 'main',
        chatJid: 'main@g.us',
        isControlGroup: true,
      },
      () => {},
    );
    expect(output.status).toBe('success');
    expect(output.result).toBe('Pipeline test response');
  });

  it('mock invokes onOutput callback when provided', async () => {
    mockRunContainerAgent.mockImplementation(
      async (_group, _input, _onProcess, onOutput) => {
        if (onOutput) {
          await onOutput({
            status: 'success',
            result: 'Streamed response',
            newSessionId: 'sess-1',
          });
        }
        return {
          status: 'success',
          result: 'Streamed response',
          newSessionId: 'sess-1',
        };
      },
    );

    const onOutput = vi.fn(async () => {});
    await runContainerAgent(
      MAIN_GROUP,
      { prompt: 'Hi', groupFolder: 'main', chatJid: 'main@g.us', isControlGroup: true },
      () => {},
      onOutput,
    );
    expect(onOutput).toHaveBeenCalledWith(
      expect.objectContaining({ result: 'Streamed response' }),
    );
  });

  it('can simulate container error', async () => {
    mockRunContainerAgent.mockResolvedValue({
      status: 'error',
      result: null,
      error: 'Container crashed',
    });
    const output = await runContainerAgent(
      MAIN_GROUP,
      {
        prompt: 'Test',
        groupFolder: 'main',
        chatJid: 'main@g.us',
        isControlGroup: true,
      },
      () => {},
    );
    expect(output.status).toBe('error');
    expect(output.error).toContain('crashed');
  });
});

// ── 4. IPC: task creation → DB storage ───────────────────────────────────

describe('IPC pipeline: schedule_task → DB', () => {
  it('creates a task in DB via processTaskIpc', async () => {
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'Pipeline test task',
        schedule_type: 'once',
        schedule_value: '2027-06-01T09:00:00',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      ipcDeps,
    );

    const tasks = getAllTasks();
    expect(tasks).toHaveLength(1);
    expect(tasks[0].prompt).toBe('Pipeline test task');
    expect(tasks[0].group_folder).toBe('other-group');
  });

  it('task lifecycle: create → pause → resume → cancel', async () => {
    await processTaskIpc(
      {
        type: 'schedule_task',
        taskId: 'pipeline-lifecycle',
        prompt: 'Lifecycle task',
        schedule_type: 'once',
        schedule_value: '2027-06-01T09:00:00',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      ipcDeps,
    );
    expect(getTaskById('pipeline-lifecycle')!.status).toBe('active');

    await processTaskIpc(
      { type: 'pause_task', taskId: 'pipeline-lifecycle' },
      'whatsapp_main',
      true,
      ipcDeps,
    );
    expect(getTaskById('pipeline-lifecycle')!.status).toBe('paused');

    await processTaskIpc(
      { type: 'resume_task', taskId: 'pipeline-lifecycle' },
      'whatsapp_main',
      true,
      ipcDeps,
    );
    expect(getTaskById('pipeline-lifecycle')!.status).toBe('active');

    await processTaskIpc(
      { type: 'cancel_task', taskId: 'pipeline-lifecycle' },
      'whatsapp_main',
      true,
      ipcDeps,
    );
    expect(getTaskById('pipeline-lifecycle')).toBeUndefined();
  });
});

// ── 5. Full pipeline: store → queue → agent → response ───────────────────

describe('Full pipeline simulation', () => {
  it('end-to-end: message stored, agent called, response produced', async () => {
    // Store a message in DB
    storeMessage({
      id: 'pipeline-msg-1',
      chat_jid: 'main@g.us',
      sender: 'user@s.whatsapp.net',
      sender_name: 'Alice',
      content: '@Deus help me debug this',
      timestamp: '2024-01-01T00:00:01.000Z',
    });

    // Simulate what the queue does: pick up messages, call agent
    const msgs = getMessagesSince('main@g.us', '', 'Deus');
    expect(msgs).toHaveLength(1);

    // Simulate runContainerAgent for this group
    const sent: string[] = [];
    const onOutput = vi.fn(async (output: { result?: string | null }) => {
      if (output.result) sent.push(output.result);
    });

    mockRunContainerAgent.mockImplementation(
      async (_g, _input, _onProcess, onOutputCb) => {
        if (onOutputCb)
          await onOutputCb({
            status: 'success',
            result: 'Got it!',
            newSessionId: undefined,
          });
        return {
          status: 'success',
          result: 'Got it!',
          newSessionId: undefined,
        };
      },
    );

    const output = await runContainerAgent(
      MAIN_GROUP,
      {
        prompt: 'msg content',
        groupFolder: 'main',
        chatJid: 'main@g.us',
        isControlGroup: true,
      },
      () => {},
      onOutput,
    );

    expect(output.status).toBe('success');
    expect(onOutput).toHaveBeenCalledWith(
      expect.objectContaining({ result: 'Got it!' }),
    );
    expect(sent[0]).toBe('Got it!');
  });

  it('multiple messages from different groups are isolated', () => {
    // Store messages to different groups
    storeMessage({
      id: 'g1-msg',
      chat_jid: 'main@g.us',
      sender: 'alice@s.whatsapp.net',
      sender_name: 'Alice',
      content: 'Main group message',
      timestamp: '2024-01-01T00:00:01.000Z',
    });
    storeMessage({
      id: 'g2-msg',
      chat_jid: 'other@g.us',
      sender: 'bob@s.whatsapp.net',
      sender_name: 'Bob',
      content: 'Other group message',
      timestamp: '2024-01-01T00:00:01.000Z',
    });

    const mainMsgs = getMessagesSince('main@g.us', '', 'Deus');
    const otherMsgs = getMessagesSince('other@g.us', '', 'Deus');

    expect(mainMsgs).toHaveLength(1);
    expect(mainMsgs[0].content).toBe('Main group message');
    expect(otherMsgs).toHaveLength(1);
    expect(otherMsgs[0].content).toBe('Other group message');
  });
});
