/**
 * Unit tests for ipc.ts — security-critical IPC paths.
 *
 * Covers: schedule task validation (bad cron/interval/timestamp),
 * non-main authorization blocking for project management operations,
 * register_group folder validation & isControlGroup stripping,
 * context_mode defaulting, skill IPC handler delegation/error handling,
 * and startIpcWatcher idempotency guard.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// ── Mocks (must be declared before importing the module under test) ─────

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

vi.mock('./config.js', () => ({
  DATA_DIR: '/tmp/deus-data',
  IPC_POLL_INTERVAL: 5000,
  TIMEZONE: 'UTC',
}));

vi.mock('./group-folder.js', () => ({
  isValidGroupFolder: vi.fn((folder: string) => {
    // Reject path traversal, empty, and slash-containing names
    if (!folder) return false;
    if (folder.includes('/') || folder.includes('\\') || folder.includes('..'))
      return false;
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/.test(folder)) return false;
    return true;
  }),
}));

vi.mock('./db.js', () => ({
  createTask: vi.fn(),
  deleteTask: vi.fn(),
  getTaskById: vi.fn(),
  updateTask: vi.fn(),
}));

vi.mock('./project-registry.js', () => ({
  registerProject: vi.fn(() => ({ id: 'proj-1', name: 'Test', path: '/tmp' })),
  associateProject: vi.fn(),
  dissociateProject: vi.fn(),
  removeProject: vi.fn(),
  getAllProjects: vi.fn(() => []),
  getProjectById: vi.fn(),
}));

vi.mock('./skills/registry.js', () => ({
  getSkillIpcHandlers: vi.fn(() => new Map()),
}));

vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      existsSync: vi.fn(() => false),
      readFileSync: vi.fn(() => ''),
      readdirSync: vi.fn(() => []),
      statSync: vi.fn(() => ({ isDirectory: () => true })),
      mkdirSync: vi.fn(),
      writeFileSync: vi.fn(),
      unlinkSync: vi.fn(),
      renameSync: vi.fn(),
    },
  };
});

import fs from 'fs';
import { processTaskIpc, startIpcWatcher, IpcDeps } from './ipc.js';
import { createTask, getTaskById, deleteTask, updateTask } from './db.js';
import {
  registerProject,
  associateProject,
  dissociateProject,
  removeProject,
  getAllProjects,
} from './project-registry.js';
import { getSkillIpcHandlers } from './skills/registry.js';
import { logger } from './logger.js';
import { isValidGroupFolder } from './group-folder.js';
import type { RegisteredGroup } from './types.js';

const mockCreateTask = vi.mocked(createTask);
const mockGetTaskById = vi.mocked(getTaskById);
const mockDeleteTask = vi.mocked(deleteTask);
const mockUpdateTask = vi.mocked(updateTask);
const mockRegisterProject = vi.mocked(registerProject);
const mockAssociateProject = vi.mocked(associateProject);
const mockDissociateProject = vi.mocked(dissociateProject);
const mockRemoveProject = vi.mocked(removeProject);
const mockGetAllProjects = vi.mocked(getAllProjects);
const mockGetSkillIpcHandlers = vi.mocked(getSkillIpcHandlers);
const mockIsValidGroupFolder = vi.mocked(isValidGroupFolder);
const mockMkdirSync = vi.mocked(fs.mkdirSync);
const mockWriteFileSync = vi.mocked(fs.writeFileSync);
const mockReaddirSync = vi.mocked(fs.readdirSync);
const mockStatSync = vi.mocked(fs.statSync);
const mockExistsSync = vi.mocked(fs.existsSync);

// ── Test helpers ────────────────────────────────────────────────────────

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

function makeDeps(overrides: Partial<IpcDeps> = {}): IpcDeps {
  return {
    sendMessage: vi.fn(async () => {}),
    registeredGroups: () => ({
      'main@g.us': MAIN_GROUP,
      'other@g.us': OTHER_GROUP,
    }),
    registerGroup: vi.fn(),
    syncGroups: vi.fn(async () => {}),
    getAvailableGroups: vi.fn(() => []),
    writeGroupsSnapshot: vi.fn(),
    onTasksChanged: vi.fn(),
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockGetSkillIpcHandlers.mockReturnValue(new Map());
  mockExistsSync.mockReturnValue(false);
  mockReaddirSync.mockReturnValue([]);
  mockStatSync.mockReturnValue({ isDirectory: () => true } as fs.Stats);
});

// ── Schedule task validation ────────────────────────────────────────────

describe('schedule_task validation', () => {
  it('rejects invalid cron expression', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'bad cron',
        schedule_type: 'cron',
        schedule_value: 'not-a-cron',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockCreateTask).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({ scheduleValue: 'not-a-cron' }),
      'Invalid cron expression',
    );
  });

  it('rejects non-numeric interval', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'bad interval',
        schedule_type: 'interval',
        schedule_value: 'abc',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockCreateTask).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({ scheduleValue: 'abc' }),
      'Invalid interval',
    );
  });

  it('rejects zero interval', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'zero interval',
        schedule_type: 'interval',
        schedule_value: '0',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockCreateTask).not.toHaveBeenCalled();
  });

  it('rejects negative interval', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'negative',
        schedule_type: 'interval',
        schedule_value: '-1000',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockCreateTask).not.toHaveBeenCalled();
  });

  it('rejects invalid once timestamp', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'bad timestamp',
        schedule_type: 'once',
        schedule_value: 'not-a-date',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockCreateTask).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({ scheduleValue: 'not-a-date' }),
      'Invalid timestamp',
    );
  });

  it('rejects schedule_task for unregistered target JID', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'orphan',
        schedule_type: 'once',
        schedule_value: '2025-06-01T00:00:00',
        targetJid: 'unknown@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockCreateTask).not.toHaveBeenCalled();
  });
});

// ── context_mode defaulting ─────────────────────────────────────────────

describe('schedule_task context_mode', () => {
  it('defaults to isolated when context_mode is omitted', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'test',
        schedule_type: 'once',
        schedule_value: '2025-06-01T00:00:00',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockCreateTask).toHaveBeenCalledWith(
      expect.objectContaining({ context_mode: 'isolated' }),
    );
  });

  it('defaults to isolated when context_mode is invalid', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'test',
        schedule_type: 'once',
        schedule_value: '2025-06-01T00:00:00',
        context_mode: 'bogus',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockCreateTask).toHaveBeenCalledWith(
      expect.objectContaining({ context_mode: 'isolated' }),
    );
  });

  it('accepts context_mode=group', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'test',
        schedule_type: 'once',
        schedule_value: '2025-06-01T00:00:00',
        context_mode: 'group',
        targetJid: 'other@g.us',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockCreateTask).toHaveBeenCalledWith(
      expect.objectContaining({ context_mode: 'group' }),
    );
  });
});

// ── Non-main authorization for project management ───────────────────────

describe('project management authorization', () => {
  const projectOps = [
    'register_project',
    'associate_project',
    'dissociate_project',
    'delete_project',
    'list_projects',
  ] as const;

  for (const op of projectOps) {
    it(`blocks non-main group from ${op}`, async () => {
      const deps = makeDeps();
      await processTaskIpc(
        {
          type: op,
          name: 'Test',
          projectPath: '/tmp/test',
          projectId: 'proj-1',
          folder: 'some-folder',
        },
        'other-group',
        false,
        deps,
      );

      expect(logger.warn).toHaveBeenCalledWith(
        expect.objectContaining({ sourceGroup: 'other-group' }),
        expect.stringContaining(`Unauthorized ${op} attempt blocked`),
      );
    });
  }

  it('allows main group to register_project', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'register_project',
        name: 'TestProject',
        projectPath: '/home/user/project',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockRegisterProject).toHaveBeenCalledWith(
      'TestProject',
      '/home/user/project',
      expect.objectContaining({}),
    );
  });

  it('allows main group to associate_project', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'associate_project',
        projectId: 'proj-1',
        folder: 'target-folder',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockAssociateProject).toHaveBeenCalledWith(
      'proj-1',
      'target-folder',
    );
  });

  it('allows main group to dissociate_project', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'dissociate_project',
        folder: 'target-folder',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockDissociateProject).toHaveBeenCalledWith('target-folder');
  });

  it('allows main group to delete_project', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'delete_project',
        projectId: 'proj-1',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockRemoveProject).toHaveBeenCalledWith('proj-1');
  });

  it('allows main group to list_projects', async () => {
    mockGetAllProjects.mockReturnValue([]);
    const deps = makeDeps();
    await processTaskIpc(
      { type: 'list_projects' },
      'whatsapp_main',
      true,
      deps,
    );

    expect(mockGetAllProjects).toHaveBeenCalled();
    expect(mockWriteFileSync).toHaveBeenCalledWith(
      expect.stringContaining('projects.json'),
      expect.any(String),
    );
  });
});

// ── register_group folder validation & isControlGroup stripping ─────────

describe('register_group', () => {
  it('rejects path traversal in folder name', async () => {
    mockIsValidGroupFolder.mockReturnValue(false);
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'register_group',
        jid: 'evil@g.us',
        name: 'Evil',
        folder: '../../etc',
        trigger: '@Deus',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(deps.registerGroup).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({ folder: '../../etc' }),
      expect.stringContaining('unsafe folder name'),
    );
  });

  it('rejects folder with slashes', async () => {
    mockIsValidGroupFolder.mockReturnValue(false);
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'register_group',
        jid: 'slash@g.us',
        name: 'Slash',
        folder: 'path/to/folder',
        trigger: '@Deus',
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(deps.registerGroup).not.toHaveBeenCalled();
  });

  it('cannot set isControlGroup via IPC (defense in depth)', async () => {
    mockIsValidGroupFolder.mockReturnValue(true);
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'register_group',
        jid: 'new@g.us',
        name: 'New Group',
        folder: 'new-group',
        trigger: '@Deus',
        // Attacker tries to escalate to control group
        isControlGroup: true,
      } as any,
      'whatsapp_main',
      true,
      deps,
    );

    expect(deps.registerGroup).toHaveBeenCalledWith(
      'new@g.us',
      expect.not.objectContaining({ isControlGroup: true }),
    );
    // Verify the registered group object does not contain isControlGroup
    const registeredGroup = (deps.registerGroup as ReturnType<typeof vi.fn>)
      .mock.calls[0][1];
    expect(registeredGroup).not.toHaveProperty('isControlGroup');
  });

  it('rejects registration with missing required fields', async () => {
    mockIsValidGroupFolder.mockReturnValue(true);
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'register_group',
        jid: 'partial@g.us',
        name: 'Partial',
        // missing folder and trigger
      },
      'whatsapp_main',
      true,
      deps,
    );

    expect(deps.registerGroup).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({ data: expect.any(Object) }),
      expect.stringContaining('missing required fields'),
    );
  });
});

// ── Skill IPC handler delegation ────────────────────────────────────────

describe('skill IPC handler delegation', () => {
  it('delegates unknown type to skill handlers', async () => {
    const handler = vi.fn(async () => true);
    const handlers = new Map([['test-skill', handler]]);
    mockGetSkillIpcHandlers.mockReturnValue(handlers);

    const deps = makeDeps();
    await processTaskIpc(
      { type: 'custom_skill_action' },
      'whatsapp_main',
      true,
      deps,
    );

    expect(handler).toHaveBeenCalledWith(
      { type: 'custom_skill_action' },
      'whatsapp_main',
      true,
      deps,
    );
  });

  it('tries handlers in order and stops at the first that returns true', async () => {
    const handler1 = vi.fn(async () => false);
    const handler2 = vi.fn(async () => true);
    const handler3 = vi.fn(async () => true);
    const handlers = new Map<string, any>([
      ['skill-a', handler1],
      ['skill-b', handler2],
      ['skill-c', handler3],
    ]);
    mockGetSkillIpcHandlers.mockReturnValue(handlers);

    const deps = makeDeps();
    await processTaskIpc({ type: 'some_action' }, 'whatsapp_main', true, deps);

    expect(handler1).toHaveBeenCalled();
    expect(handler2).toHaveBeenCalled();
    expect(handler3).not.toHaveBeenCalled();
  });

  it('logs warning when no handler matches', async () => {
    mockGetSkillIpcHandlers.mockReturnValue(new Map());
    const deps = makeDeps();
    await processTaskIpc(
      { type: 'totally_unknown' },
      'whatsapp_main',
      true,
      deps,
    );

    expect(logger.warn).toHaveBeenCalledWith(
      { type: 'totally_unknown' },
      'Unknown IPC task type',
    );
  });

  it('catches and logs skill handler errors without crashing', async () => {
    const failingHandler = vi.fn(async () => {
      throw new Error('skill exploded');
    });
    const handlers = new Map<string, any>([['bad-skill', failingHandler]]);
    mockGetSkillIpcHandlers.mockReturnValue(handlers);

    const deps = makeDeps();
    // Should not throw
    await processTaskIpc(
      { type: 'trigger_bad_skill' },
      'whatsapp_main',
      true,
      deps,
    );

    expect(logger.error).toHaveBeenCalledWith(
      expect.objectContaining({
        skill: 'bad-skill',
        type: 'trigger_bad_skill',
      }),
      'Skill IPC handler error',
    );
  });

  it('continues to next handler after a skill handler throws', async () => {
    const failingHandler = vi.fn(async () => {
      throw new Error('boom');
    });
    const successHandler = vi.fn(async () => true);
    const handlers = new Map<string, any>([
      ['failing', failingHandler],
      ['success', successHandler],
    ]);
    mockGetSkillIpcHandlers.mockReturnValue(handlers);

    const deps = makeDeps();
    await processTaskIpc(
      { type: 'test_recovery' },
      'whatsapp_main',
      true,
      deps,
    );

    expect(failingHandler).toHaveBeenCalled();
    expect(successHandler).toHaveBeenCalled();
  });
});

// ── startIpcWatcher idempotency ─────────────────────────────────────────

describe('startIpcWatcher idempotency', () => {
  // We need to reset the module-level ipcWatcherRunning flag between tests
  // by re-importing after clearing the module cache.
  // Since vitest caches modules, we test the guard by calling twice.

  it('does not start a second watcher when called twice', () => {
    // Mock fs operations used during initialization
    mockReaddirSync.mockReturnValue([]);
    mockExistsSync.mockReturnValue(false);
    mockStatSync.mockReturnValue({ isDirectory: () => true } as fs.Stats);

    const deps = makeDeps();

    // First call: should start watcher
    startIpcWatcher(deps);
    expect(logger.info).toHaveBeenCalledWith(
      'IPC watcher started (per-group namespaces)',
    );

    vi.clearAllMocks();

    // Second call: should be a no-op
    startIpcWatcher(deps);
    expect(logger.debug).toHaveBeenCalledWith(
      'IPC watcher already running, skipping duplicate start',
    );
    expect(logger.info).not.toHaveBeenCalledWith(
      'IPC watcher started (per-group namespaces)',
    );
  });
});

// ── Non-main schedule_task cross-group authorization ────────────────────

describe('schedule_task cross-group authorization', () => {
  it('non-main group cannot schedule for another group', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'sneaky',
        schedule_type: 'once',
        schedule_value: '2025-06-01T00:00:00',
        targetJid: 'main@g.us',
      },
      'other-group',
      false,
      deps,
    );

    expect(mockCreateTask).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({
        sourceGroup: 'other-group',
        targetFolder: 'whatsapp_main',
      }),
      'Unauthorized schedule_task attempt blocked',
    );
  });

  it('non-main group can schedule for itself', async () => {
    const deps = makeDeps();
    await processTaskIpc(
      {
        type: 'schedule_task',
        prompt: 'self task',
        schedule_type: 'once',
        schedule_value: '2025-06-01T00:00:00',
        targetJid: 'other@g.us',
      },
      'other-group',
      false,
      deps,
    );

    expect(mockCreateTask).toHaveBeenCalledWith(
      expect.objectContaining({ group_folder: 'other-group' }),
    );
  });
});
