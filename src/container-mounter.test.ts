/**
 * Unit tests for container-mounter.ts — security-critical volume mount assembly.
 *
 * Covers: mount allowlist enforcement, .env credential shadowing, TOCTOU
 * symlink defense, sensitive file/dir shadowing, vault mounting, and
 * per-group IPC namespace isolation.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import path from 'path';

// ── Mocks (must be declared before importing the module under test) ─────

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

vi.mock('./config.js', () => ({
  DATA_DIR: '/tmp/deus-data',
  GROUPS_DIR: '/tmp/deus-groups',
  HOME_DIR: '/home/testuser',
  CONFIG_DIR: '/home/testuser/.config/deus',
}));

vi.mock('./group-folder.js', () => ({
  resolveGroupFolderPath: vi.fn(
    (folder: string) => `/tmp/deus-groups/${folder}`,
  ),
  resolveGroupIpcPath: vi.fn(
    (folder: string) => `/tmp/deus-data/ipc/${folder}`,
  ),
}));

vi.mock('./db.js', () => ({
  getProjectById: vi.fn(),
}));

vi.mock('./credential-proxy.js', () => ({
  detectAuthMode: vi.fn(() => 'api-key'),
}));

vi.mock('./project-registry.js', () => ({
  SENSITIVE_FILE_PATTERNS: ['.env', '.env.local', '.env.production'],
  SENSITIVE_DIR_PATTERNS: ['credentials', 'secrets'],
}));

vi.mock('./mount-security.js', () => ({
  validateAdditionalMounts: vi.fn(() => []),
}));

// Mock fs — we need fine-grained control over existsSync, realpathSync, etc.
vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      existsSync: vi.fn(() => false),
      readFileSync: vi.fn(() => ''),
      realpathSync: vi.fn((p: string) => p),
      mkdirSync: vi.fn(),
      writeFileSync: vi.fn(),
      readdirSync: vi.fn(() => []),
      statSync: vi.fn(() => ({ isDirectory: () => false })),
      cpSync: vi.fn(),
    },
  };
});

import fs from 'fs';
import { buildVolumeMounts, VolumeMount } from './container-mounter.js';
import { getProjectById } from './db.js';
import { detectAuthMode } from './credential-proxy.js';
import { validateAdditionalMounts } from './mount-security.js';
import { logger } from './logger.js';
import type { RegisteredGroup } from './types.js';

const mockExistsSync = vi.mocked(fs.existsSync);
const mockRealpathSync = vi.mocked(fs.realpathSync);
const mockMkdirSync = vi.mocked(fs.mkdirSync);
const mockWriteFileSync = vi.mocked(fs.writeFileSync);
const mockReaddirSync = vi.mocked(fs.readdirSync);
const mockStatSync = vi.mocked(fs.statSync);
const mockCpSync = vi.mocked(fs.cpSync);
const mockReadFileSync = vi.mocked(fs.readFileSync);
const mockGetProjectById = vi.mocked(getProjectById);
const mockDetectAuthMode = vi.mocked(detectAuthMode);
const mockValidateAdditionalMounts = vi.mocked(validateAdditionalMounts);

// ── Test helpers ────────────────────────────────────────────────────────

const makeGroup = (
  overrides: Partial<RegisteredGroup> = {},
): RegisteredGroup => ({
  name: 'Test Group',
  folder: 'test-group',
  trigger: '@Deus',
  added_at: '2024-01-01T00:00:00.000Z',
  ...overrides,
});

function findMount(
  mounts: VolumeMount[],
  containerPath: string,
): VolumeMount | undefined {
  return mounts.find((m) => m.containerPath === containerPath);
}

beforeEach(() => {
  vi.clearAllMocks();
  // Default: most paths don't exist, realpathSync identity, no auth mode
  mockExistsSync.mockReturnValue(false);
  mockRealpathSync.mockImplementation((p) => String(p));
  mockDetectAuthMode.mockReturnValue('api-key');
  mockValidateAdditionalMounts.mockReturnValue([]);
  mockReaddirSync.mockReturnValue([]);
});

// ── Control group basic mounts ──────────────────────────────────────────

describe('buildVolumeMounts: control group', () => {
  it('mounts project root as read-only at /workspace/project', () => {
    const group = makeGroup({ isControlGroup: true });
    const mounts = buildVolumeMounts(group, true);
    const projectMount = findMount(mounts, '/workspace/project');
    expect(projectMount).toBeDefined();
    expect(projectMount!.readonly).toBe(true);
  });

  it('mounts group folder as writable at /workspace/group', () => {
    const group = makeGroup({ isControlGroup: true });
    const mounts = buildVolumeMounts(group, true);
    const groupMount = findMount(mounts, '/workspace/group');
    expect(groupMount).toBeDefined();
    expect(groupMount!.readonly).toBe(false);
    expect(groupMount!.hostPath).toBe('/tmp/deus-groups/test-group');
  });

  it('shadows .env with /dev/null when .env exists', () => {
    // Make .env exist
    mockExistsSync.mockImplementation((p) => {
      if (String(p).endsWith('.env')) return true;
      return false;
    });
    const group = makeGroup({ isControlGroup: true });
    const mounts = buildVolumeMounts(group, true);
    const envShadow = findMount(mounts, '/workspace/project/.env');
    expect(envShadow).toBeDefined();
    expect(envShadow!.readonly).toBe(true);
    // Should use os.devNull (platform-agnostic null device)
    expect(
      envShadow!.hostPath === '/dev/null' ||
        envShadow!.hostPath === '\\\\.\\nul',
    ).toBe(true);
  });

  it('does not shadow .env when it does not exist', () => {
    mockExistsSync.mockReturnValue(false);
    const group = makeGroup({ isControlGroup: true });
    const mounts = buildVolumeMounts(group, true);
    const envShadow = findMount(mounts, '/workspace/project/.env');
    expect(envShadow).toBeUndefined();
  });
});

// ── Non-control group basic mounts ──────────────────────────────────────

describe('buildVolumeMounts: non-control group', () => {
  it('mounts only group folder, not project root', () => {
    const group = makeGroup();
    const mounts = buildVolumeMounts(group, false);
    const groupMount = findMount(mounts, '/workspace/group');
    expect(groupMount).toBeDefined();
    expect(groupMount!.readonly).toBe(false);
    // Should not have a project mount from the control-group path
    // (only from projectId path which we haven't set)
    const projectMount = findMount(mounts, '/workspace/project');
    expect(projectMount).toBeUndefined();
  });

  it('mounts global memory directory as read-only when it exists', () => {
    mockExistsSync.mockImplementation((p) => {
      if (String(p) === '/tmp/deus-groups/global') return true;
      return false;
    });
    const group = makeGroup();
    const mounts = buildVolumeMounts(group, false);
    const globalMount = findMount(mounts, '/workspace/global');
    expect(globalMount).toBeDefined();
    expect(globalMount!.readonly).toBe(true);
    expect(globalMount!.hostPath).toBe('/tmp/deus-groups/global');
  });

  it('does not mount global directory when it does not exist', () => {
    mockExistsSync.mockReturnValue(false);
    const group = makeGroup();
    const mounts = buildVolumeMounts(group, false);
    const globalMount = findMount(mounts, '/workspace/global');
    expect(globalMount).toBeUndefined();
  });
});

// ── External project mount (TOCTOU defense) ─────────────────────────────

describe('buildVolumeMounts: external project mount', () => {
  const PROJECT = {
    id: 'proj-1',
    name: 'MyApp',
    path: '/home/testuser/projects/myapp',
    type: null,
    readonly: false,
    created_at: '2024-01-01',
  };

  it('mounts project when realpath matches registered path', () => {
    mockGetProjectById.mockReturnValue(PROJECT);
    mockExistsSync.mockReturnValue(true);
    mockRealpathSync.mockImplementation((p) => String(p));
    mockStatSync.mockReturnValue({ isDirectory: () => false } as fs.Stats);

    const group = makeGroup({
      projectId: 'proj-1',
      isControlGroup: true,
    });
    const mounts = buildVolumeMounts(group, true);
    const projectMount = mounts.find(
      (m) =>
        m.containerPath === '/workspace/project' && m.hostPath === PROJECT.path,
    );
    expect(projectMount).toBeDefined();
    expect(projectMount!.readonly).toBe(false); // control group + project.readonly=false
  });

  it('blocks mount when realpath differs from registered path (symlink swap attack)', () => {
    mockGetProjectById.mockReturnValue(PROJECT);
    mockExistsSync.mockReturnValue(true);
    // Simulate symlink swap: realpath resolves to different location
    mockRealpathSync.mockReturnValue('/etc/shadow');

    const group = makeGroup({ projectId: 'proj-1' });
    const mounts = buildVolumeMounts(group, true);
    // Should NOT have a project mount at the attacker's path
    const projectMount = mounts.find(
      (m) =>
        m.containerPath === '/workspace/project' &&
        m.hostPath === '/etc/shadow',
    );
    expect(projectMount).toBeUndefined();
    expect(logger.error).toHaveBeenCalledWith(
      expect.objectContaining({
        registeredPath: PROJECT.path,
        currentRealPath: '/etc/shadow',
      }),
      expect.stringContaining('mount BLOCKED'),
    );
  });

  it('skips mount when realpathSync throws (path no longer resolvable)', () => {
    mockGetProjectById.mockReturnValue(PROJECT);
    mockExistsSync.mockReturnValue(true);
    mockRealpathSync.mockImplementation(() => {
      throw new Error('ENOENT');
    });

    const group = makeGroup({ projectId: 'proj-1' });
    const mounts = buildVolumeMounts(group, true);
    // No project mount should be present
    const projectMount = mounts.find(
      (m) =>
        m.containerPath === '/workspace/project' && m.hostPath === PROJECT.path,
    );
    expect(projectMount).toBeUndefined();
    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({ projectId: 'proj-1' }),
      expect.stringContaining('no longer resolvable'),
    );
  });

  it('forces readonly for non-control groups', () => {
    mockGetProjectById.mockReturnValue(PROJECT);
    mockExistsSync.mockReturnValue(true);
    mockRealpathSync.mockImplementation((p) => String(p));
    mockStatSync.mockReturnValue({ isDirectory: () => false } as fs.Stats);

    const group = makeGroup({ projectId: 'proj-1' });
    const mounts = buildVolumeMounts(group, false);
    const projectMount = mounts.find(
      (m) =>
        m.containerPath === '/workspace/project' && m.hostPath === PROJECT.path,
    );
    expect(projectMount).toBeDefined();
    expect(projectMount!.readonly).toBe(true); // non-control always readonly
  });

  it('forces readonly when project config says readonly', () => {
    const readonlyProject = { ...PROJECT, readonly: true };
    mockGetProjectById.mockReturnValue(readonlyProject);
    mockExistsSync.mockReturnValue(true);
    mockRealpathSync.mockImplementation((p) => String(p));
    mockStatSync.mockReturnValue({ isDirectory: () => false } as fs.Stats);

    const group = makeGroup({
      projectId: 'proj-1',
      isControlGroup: true,
    });
    const mounts = buildVolumeMounts(group, true);
    const projectMount = mounts.find(
      (m) =>
        m.containerPath === '/workspace/project' && m.hostPath === PROJECT.path,
    );
    expect(projectMount).toBeDefined();
    expect(projectMount!.readonly).toBe(true);
  });

  it('skips mount when project path does not exist on disk', () => {
    mockGetProjectById.mockReturnValue(PROJECT);
    mockExistsSync.mockReturnValue(false);

    const group = makeGroup({ projectId: 'proj-1' });
    const mounts = buildVolumeMounts(group, true);
    const projectMount = mounts.find((m) => m.hostPath === PROJECT.path);
    expect(projectMount).toBeUndefined();
    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({ path: PROJECT.path }),
      expect.stringContaining('does not exist'),
    );
  });

  it('skips mount when project is not found in DB', () => {
    mockGetProjectById.mockReturnValue(undefined);

    const group = makeGroup({ projectId: 'proj-1' });
    // Should not throw
    const mounts = buildVolumeMounts(group, true);
    const projectMount = mounts.find(
      (m) =>
        m.containerPath === '/workspace/project' && m.hostPath === PROJECT.path,
    );
    expect(projectMount).toBeUndefined();
  });
});

// ── Sensitive file/directory shadowing ──────────────────────────────────

describe('buildVolumeMounts: sensitive file shadowing', () => {
  const PROJECT = {
    id: 'proj-1',
    name: 'MyApp',
    path: '/home/testuser/projects/myapp',
    type: null,
    readonly: false,
    created_at: '2024-01-01',
  };

  it('shadows .env files inside the project with /dev/null', () => {
    mockGetProjectById.mockReturnValue(PROJECT);
    mockRealpathSync.mockImplementation((p) => String(p));
    mockExistsSync.mockReturnValue(true); // all paths exist
    mockStatSync.mockReturnValue({ isDirectory: () => false } as fs.Stats);

    const group = makeGroup({
      projectId: 'proj-1',
      isControlGroup: true,
    });
    const mounts = buildVolumeMounts(group, true);

    // Check .env shadow
    const envShadow = findMount(mounts, '/workspace/project/.env');
    expect(envShadow).toBeDefined();
    expect(envShadow!.hostPath).toBe('/dev/null');
    expect(envShadow!.readonly).toBe(true);

    // Check .env.local shadow
    const envLocalShadow = findMount(mounts, '/workspace/project/.env.local');
    expect(envLocalShadow).toBeDefined();
    expect(envLocalShadow!.hostPath).toBe('/dev/null');
  });

  it('shadows sensitive directories with empty tmpdir', () => {
    mockGetProjectById.mockReturnValue(PROJECT);
    mockRealpathSync.mockImplementation((p) => String(p));
    mockExistsSync.mockReturnValue(true);
    mockStatSync.mockReturnValue({ isDirectory: () => true } as fs.Stats);

    const group = makeGroup({
      projectId: 'proj-1',
      isControlGroup: true,
    });
    const mounts = buildVolumeMounts(group, true);

    const credShadow = findMount(mounts, '/workspace/project/credentials');
    expect(credShadow).toBeDefined();
    expect(credShadow!.readonly).toBe(true);
    // Shadow dir should be under DATA_DIR/project-shadows/
    expect(credShadow!.hostPath).toContain('project-shadows');
    expect(credShadow!.hostPath).toContain('proj-1');

    const secretsShadow = findMount(mounts, '/workspace/project/secrets');
    expect(secretsShadow).toBeDefined();
    expect(secretsShadow!.readonly).toBe(true);
  });

  it('creates shadow directory with restrictive permissions (0o700)', () => {
    mockGetProjectById.mockReturnValue(PROJECT);
    mockRealpathSync.mockImplementation((p) => String(p));
    mockExistsSync.mockReturnValue(true);
    mockStatSync.mockReturnValue({ isDirectory: () => true } as fs.Stats);

    const group = makeGroup({
      projectId: 'proj-1',
      isControlGroup: true,
    });
    buildVolumeMounts(group, true);

    // Verify mkdirSync was called with mode 0o700 for shadow dirs
    const shadowMkdirCalls = mockMkdirSync.mock.calls.filter((call) =>
      String(call[0]).includes('project-shadows'),
    );
    expect(shadowMkdirCalls.length).toBeGreaterThan(0);
    for (const call of shadowMkdirCalls) {
      expect(call[1]).toEqual(
        expect.objectContaining({ recursive: true, mode: 0o700 }),
      );
    }
  });
});

// ── Per-group IPC namespace isolation ───────────────────────────────────

describe('buildVolumeMounts: IPC namespace', () => {
  it('creates per-group IPC directories (messages, tasks, input)', () => {
    const group = makeGroup();
    buildVolumeMounts(group, false);

    // Verify IPC subdirectories were created
    const ipcMkdirCalls = mockMkdirSync.mock.calls.filter((call) =>
      String(call[0]).includes('/ipc/'),
    );
    const paths = ipcMkdirCalls.map((call) => String(call[0]));
    expect(paths).toContain('/tmp/deus-data/ipc/test-group/messages');
    expect(paths).toContain('/tmp/deus-data/ipc/test-group/tasks');
    expect(paths).toContain('/tmp/deus-data/ipc/test-group/input');
  });

  it('mounts IPC directory at /workspace/ipc (writable)', () => {
    const group = makeGroup();
    const mounts = buildVolumeMounts(group, false);
    const ipcMount = findMount(mounts, '/workspace/ipc');
    expect(ipcMount).toBeDefined();
    expect(ipcMount!.readonly).toBe(false);
    expect(ipcMount!.hostPath).toBe('/tmp/deus-data/ipc/test-group');
  });
});

// ── Per-group Claude session isolation ──────────────────────────────────

describe('buildVolumeMounts: session isolation', () => {
  it('mounts per-group .claude session dir at /home/node/.claude', () => {
    const group = makeGroup();
    const mounts = buildVolumeMounts(group, false);
    const sessionMount = findMount(mounts, '/home/node/.claude');
    expect(sessionMount).toBeDefined();
    expect(sessionMount!.readonly).toBe(false);
    expect(sessionMount!.hostPath).toContain('sessions/test-group/.claude');
  });

  it('creates default settings.json when it does not exist', () => {
    mockExistsSync.mockReturnValue(false);
    const group = makeGroup();
    buildVolumeMounts(group, false);

    // Should have written a settings.json
    const settingsWrites = mockWriteFileSync.mock.calls.filter((call) =>
      String(call[0]).includes('settings.json'),
    );
    expect(settingsWrites.length).toBeGreaterThan(0);
    const written = JSON.parse(String(settingsWrites[0][1]));
    expect(written.env).toBeDefined();
    expect(written.env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS).toBe('1');
  });
});

// ── OAuth credential injection ──────────────────────────────────────────

describe('buildVolumeMounts: OAuth mode', () => {
  it('writes placeholder credentials when auth mode is oauth', () => {
    mockDetectAuthMode.mockReturnValue('oauth');
    const group = makeGroup();
    buildVolumeMounts(group, false);

    const credWrites = mockWriteFileSync.mock.calls.filter((call) =>
      String(call[0]).includes('.credentials.json'),
    );
    expect(credWrites.length).toBeGreaterThan(0);
    const creds = JSON.parse(String(credWrites[0][1]));
    expect(creds.claudeAiOauth.accessToken).toBe('placeholder');
  });

  it('does not write credentials when auth mode is api-key', () => {
    mockDetectAuthMode.mockReturnValue('api-key');
    const group = makeGroup();
    buildVolumeMounts(group, false);

    const credWrites = mockWriteFileSync.mock.calls.filter((call) =>
      String(call[0]).includes('.credentials.json'),
    );
    expect(credWrites).toHaveLength(0);
  });
});

// ── Vault mount ─────────────────────────────────────────────────────────

describe('buildVolumeMounts: vault', () => {
  it('mounts vault when DEUS_VAULT_PATH env var is set and path exists', () => {
    const originalEnv = process.env.DEUS_VAULT_PATH;
    process.env.DEUS_VAULT_PATH = '/home/testuser/vault';
    mockExistsSync.mockImplementation((p) => {
      if (String(p).includes('vault')) return true;
      return false;
    });

    const group = makeGroup();
    const mounts = buildVolumeMounts(group, false);
    const vaultMount = findMount(mounts, '/workspace/vault');
    expect(vaultMount).toBeDefined();
    expect(vaultMount!.readonly).toBe(false);

    // Cleanup
    if (originalEnv !== undefined) {
      process.env.DEUS_VAULT_PATH = originalEnv;
    } else {
      delete process.env.DEUS_VAULT_PATH;
    }
  });

  it('expands ~ in vault path', () => {
    const originalEnv = process.env.DEUS_VAULT_PATH;
    process.env.DEUS_VAULT_PATH = '~/my-vault';
    mockExistsSync.mockReturnValue(true);

    const group = makeGroup();
    const mounts = buildVolumeMounts(group, false);
    const vaultMount = findMount(mounts, '/workspace/vault');
    expect(vaultMount).toBeDefined();
    // Should expand ~ to HOME_DIR
    expect(vaultMount!.hostPath).toContain('/home/testuser');
    expect(vaultMount!.hostPath).toContain('my-vault');

    if (originalEnv !== undefined) {
      process.env.DEUS_VAULT_PATH = originalEnv;
    } else {
      delete process.env.DEUS_VAULT_PATH;
    }
  });

  it('reads vault path from config.json when env var is not set', () => {
    const originalEnv = process.env.DEUS_VAULT_PATH;
    delete process.env.DEUS_VAULT_PATH;
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync.mockImplementation((p) => {
      if (String(p).includes('config.json')) {
        return JSON.stringify({
          vault_path: '/home/testuser/vault-from-config',
        });
      }
      return '';
    });

    const group = makeGroup();
    const mounts = buildVolumeMounts(group, false);
    const vaultMount = findMount(mounts, '/workspace/vault');
    expect(vaultMount).toBeDefined();

    if (originalEnv !== undefined) {
      process.env.DEUS_VAULT_PATH = originalEnv;
    } else {
      delete process.env.DEUS_VAULT_PATH;
    }
  });
});

// ── Additional mounts delegation ────────────────────────────────────────

describe('buildVolumeMounts: additional mounts', () => {
  it('delegates to validateAdditionalMounts and appends results', () => {
    const extraMount: VolumeMount = {
      hostPath: '/home/testuser/data',
      containerPath: '/workspace/extra/data',
      readonly: true,
    };
    mockValidateAdditionalMounts.mockReturnValue([extraMount]);

    const group = makeGroup({
      containerConfig: {
        additionalMounts: [{ hostPath: '/home/testuser/data' }],
      },
    });
    const mounts = buildVolumeMounts(group, false);

    expect(mockValidateAdditionalMounts).toHaveBeenCalledWith(
      [{ hostPath: '/home/testuser/data' }],
      'Test Group',
      false,
    );
    const dataMount = findMount(mounts, '/workspace/extra/data');
    expect(dataMount).toBeDefined();
  });

  it('does not call validateAdditionalMounts when no additional mounts configured', () => {
    const group = makeGroup();
    buildVolumeMounts(group, false);
    expect(mockValidateAdditionalMounts).not.toHaveBeenCalled();
  });
});

// ── Skills sync ─────────────────────────────────────────────────────────

describe('buildVolumeMounts: skills sync', () => {
  it('copies skill directories from container/skills into group session', () => {
    mockExistsSync.mockImplementation((p) => {
      if (String(p).includes(path.join('container', 'skills'))) return true;
      return false;
    });
    mockReaddirSync.mockImplementation(((p: fs.PathLike) => {
      if (String(p).includes(path.join('container', 'skills'))) {
        return ['debug', 'resume'];
      }
      return [];
    }) as typeof fs.readdirSync);
    mockStatSync.mockReturnValue({ isDirectory: () => true } as fs.Stats);

    const group = makeGroup();
    buildVolumeMounts(group, false);

    // cpSync should have been called for each skill directory
    expect(mockCpSync).toHaveBeenCalledTimes(2);
  });
});

// ── Agent-runner source mount ───────────────────────────────────────────

describe('buildVolumeMounts: agent-runner', () => {
  it('mounts agent-runner source read-only at /app/src', () => {
    mockExistsSync.mockImplementation((p) => {
      if (String(p).includes('agent-runner')) return true;
      return false;
    });

    const group = makeGroup();
    const mounts = buildVolumeMounts(group, false);
    const appMount = findMount(mounts, '/app/src');
    expect(appMount).toBeDefined();
    expect(appMount!.readonly).toBe(true);
  });
});
