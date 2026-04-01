/**
 * Integration tests for processTaskIpc — exercises actual DB writes,
 * authorization logic, and task lifecycle.
 *
 * These use _initTestDatabase() (in-memory) and real processTaskIpc calls,
 * no IPC file watcher needed.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest';

import {
  _initTestDatabase,
  createTask,
  getAllTasks,
  getTaskById,
  setRegisteredGroup,
} from './db.js';
import { processTaskIpc, IpcDeps } from './ipc.js';
import type { RegisteredGroup } from './types.js';

// Mock the project-registry to avoid real FS + allowlist operations
vi.mock('./project-registry.js', () => ({
  registerProject: vi.fn(() => ({ id: 'proj-test', name: 'Test', path: '/tmp/proj', type: null, readonly: false, created_at: '' })),
  associateProject: vi.fn(),
  dissociateProject: vi.fn(),
  removeProject: vi.fn(),
  getAllProjects: vi.fn(() => []),
  getProjectById: vi.fn(() => null),
}));

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

vi.mock('./config.js', () => ({
  DATA_DIR: '/tmp/deus-ipc-test',
  IPC_POLL_INTERVAL: 500,
  TIMEZONE: 'UTC',
}));

vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      mkdirSync: vi.fn(),
      writeFileSync: vi.fn(),
      existsSync: vi.fn(() => false),
      readdirSync: vi.fn(() => []),
      statSync: vi.fn(() => ({ isDirectory: () => false })),
      readFileSync: vi.fn(() => ''),
      unlinkSync: vi.fn(),
      renameSync: vi.fn(),
    },
  };
});

// Registered groups used throughout
const MAIN_GROUP: RegisteredGroup = {
  name: 'Main',
  folder: 'whatsapp_main',
  trigger: 'always',
  added_at: '2024-01-01T00:00:00.000Z',
  isMain: true,
};

const OTHER_GROUP: RegisteredGroup = {
  name: 'Other',
  folder: 'other-group',
  trigger: '@Deus',
  added_at: '2024-01-01T00:00:00.000Z',
};

let groups: Record<string, RegisteredGroup>;
let deps: IpcDeps;

beforeEach(() => {
  _initTestDatabase();

  groups = {
    'main@g.us': MAIN_GROUP,
    'other@g.us': OTHER_GROUP,
  };

  setRegisteredGroup('main@g.us', MAIN_GROUP);
  setRegisteredGroup('other@g.us', OTHER_GROUP);

  deps = {
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
});

// ── 1. schedule_task creates DB entry ────────────────────────────────────

describe('processTaskIpc: schedule_task', () => {
  it('creates a task in the DB with correct fields', async () => {
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'Send a report',
        schedule_type: 'once',
        schedule_value: '2027-01-01T09:00:00',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    const tasks = getAllTasks();
    expect(tasks).toHaveLength(1);
    expect(tasks[0].prompt).toBe('Send a report');
    expect(tasks[0].group_folder).toBe('other-group');
    expect(tasks[0].status).toBe('active');
  });

  it('calls onTasksChanged after creating a task', async () => {
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'Notify',
        schedule_type: 'once',
        schedule_value: '2027-06-01T00:00:00',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(deps.onTasksChanged).toHaveBeenCalled();
  });

  it('preserves explicit taskId', async () => {
    await processTaskIpc(
      {
        type: 'schedule_task',
        taskId: 'my-custom-id',
        prompt: 'Custom ID task',
        schedule_type: 'once',
        schedule_value: '2027-06-01T00:00:00',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    const task = getTaskById('my-custom-id');
    expect(task).toBeDefined();
  });
});

// ── 2. pause_task / resume_task / cancel_task lifecycle ───────────────────

describe('processTaskIpc: task lifecycle', () => {
  beforeEach(() => {
    createTask({
      id: 'lifecycle-task',
      group_folder: 'other-group',
      chat_jid: 'other@g.us',
      prompt: 'Test task',
      schedule_type: 'once',
      schedule_value: '2027-06-01T00:00:00',
      context_mode: 'isolated',
      next_run: '2027-06-01T00:00:00.000Z',
      status: 'active',
      created_at: '2024-01-01T00:00:00.000Z',
    });
  });

  it('pause_task transitions task to paused', async () => {
    await processTaskIpc(
      { type: 'pause_task', taskId: 'lifecycle-task' },
      'whatsapp_main',
      true,
      deps,
    );
    expect(getTaskById('lifecycle-task')!.status).toBe('paused');
    expect(deps.onTasksChanged).toHaveBeenCalled();
  });

  it('resume_task transitions task back to active', async () => {
    await processTaskIpc(
      { type: 'pause_task', taskId: 'lifecycle-task' },
      'whatsapp_main',
      true,
      deps,
    );
    await processTaskIpc(
      { type: 'resume_task', taskId: 'lifecycle-task' },
      'whatsapp_main',
      true,
      deps,
    );
    expect(getTaskById('lifecycle-task')!.status).toBe('active');
  });

  it('cancel_task removes task from DB', async () => {
    await processTaskIpc(
      { type: 'cancel_task', taskId: 'lifecycle-task' },
      'whatsapp_main',
      true,
      deps,
    );
    expect(getTaskById('lifecycle-task')).toBeUndefined();
    expect(deps.onTasksChanged).toHaveBeenCalled();
  });
});

// ── 3. update_task modifies prompt ────────────────────────────────────────

describe('processTaskIpc: update_task', () => {
  beforeEach(() => {
    createTask({
      id: 'update-task',
      group_folder: 'other-group',
      chat_jid: 'other@g.us',
      prompt: 'Original prompt',
      schedule_type: 'once',
      schedule_value: '2027-06-01T00:00:00',
      context_mode: 'isolated',
      next_run: '2027-06-01T00:00:00.000Z',
      status: 'active',
      created_at: '2024-01-01T00:00:00.000Z',
    });
  });

  it('updates the prompt field', async () => {
    await processTaskIpc(
      { type: 'update_task', taskId: 'update-task', prompt: 'Updated prompt' },
      'whatsapp_main',
      true,
      deps,
    );
    expect(getTaskById('update-task')!.prompt).toBe('Updated prompt');
    expect(deps.onTasksChanged).toHaveBeenCalled();
  });

  it('non-main group cannot update another groups task', async () => {
    await processTaskIpc(
      { type: 'update_task', taskId: 'update-task', prompt: 'Hacked' },
      'third-group',
      false,
      deps,
    );
    expect(getTaskById('update-task')!.prompt).toBe('Original prompt');
  });
});

// ── 4. Authorization boundaries ───────────────────────────────────────────

describe('processTaskIpc: authorization', () => {
  it('non-main group cannot register a new group', async () => {
    await processTaskIpc(
      {
        type: 'register_group',
        jid: 'new@g.us',
        name: 'New Group',
        folder: 'new-group',
        trigger: '@Deus',
      },
      'other-group',
      false,
      deps,
    );
    expect(groups['new@g.us']).toBeUndefined();
  });

  it('main group can register a new group', async () => {
    await processTaskIpc(
      {
        type: 'register_group',
        jid: 'new2@g.us',
        name: 'New Group 2',
        folder: 'new-group-2',
        trigger: '@Deus',
      },
      'whatsapp_main',
      true,
      deps,
    );
    expect(groups['new2@g.us']).toBeDefined();
    expect(groups['new2@g.us'].name).toBe('New Group 2');
  });

  it('unknown IPC type does not throw', async () => {
    await expect(
      processTaskIpc(
        { type: 'completely_unknown_type' },
        'whatsapp_main',
        true,
        deps,
      ),
    ).resolves.toBeUndefined();
  });

  it('non-main cannot call refresh_groups', async () => {
    await processTaskIpc(
      { type: 'refresh_groups' },
      'other-group',
      false,
      deps,
    );
    // syncGroups should not have been called
    expect(deps.syncGroups).not.toHaveBeenCalled();
  });
});

// ── 5-7. (watcher-level) authorization pattern tests (direct logic) ───────

describe('IPC authorization logic (replicated from startIpcWatcher)', () => {
  function isMessageAuthorized(
    sourceGroup: string,
    isMain: boolean,
    targetChatJid: string,
  ): boolean {
    const targetGroup = groups[targetChatJid];
    return isMain || (!!targetGroup && targetGroup.folder === sourceGroup);
  }

  it('main group can send to any chat JID', () => {
    expect(isMessageAuthorized('whatsapp_main', true, 'other@g.us')).toBe(true);
    expect(isMessageAuthorized('whatsapp_main', true, 'unknown@g.us')).toBe(true);
  });

  it('non-main can send to its own JID only', () => {
    expect(isMessageAuthorized('other-group', false, 'other@g.us')).toBe(true);
  });

  it('non-main cannot send to another groups JID', () => {
    expect(isMessageAuthorized('other-group', false, 'main@g.us')).toBe(false);
  });

  it('non-main cannot send to unregistered JID', () => {
    expect(isMessageAuthorized('other-group', false, 'unknown@g.us')).toBe(false);
  });
});
