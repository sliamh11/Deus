import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';
import { EventEmitter } from 'events';
import { PassThrough } from 'stream';

// Sentinel markers must match container-runner.ts
const OUTPUT_START_MARKER = '---DEUS_OUTPUT_START---';
const OUTPUT_END_MARKER = '---DEUS_OUTPUT_END---';

// Mock config
vi.mock('./config.js', () => ({
  CONTAINER_IMAGE: 'deus-agent:latest',
  CONTAINER_MAX_OUTPUT_SIZE: 10485760,
  CONTAINER_TIMEOUT: 1800000, // 30min
  CREDENTIAL_PROXY_PORT: 3001,
  DATA_DIR: '/tmp/deus-test-data',
  GROUPS_DIR: '/tmp/deus-test-groups',
  IDLE_TIMEOUT: 1800000, // 30min
  TIMEZONE: 'America/Los_Angeles',
}));

// Mock logger
vi.mock('./logger.js', () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

// Mock fs
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
      copyFileSync: vi.fn(),
    },
  };
});

// Mock mount-security
vi.mock('./mount-security.js', () => ({
  validateAdditionalMounts: vi.fn(() => []),
}));

// Mock evolution-client (spawns Python subprocess with 3s timeout — incompatible with fake timers)
vi.mock('./evolution-client.js', () => ({
  getReflections: vi.fn(async () => ({ block: '', reflectionIds: [] })),
  logInteraction: vi.fn(),
}));

// Mock domain-presets and user-signal
vi.mock('./domain-presets.js', () => ({
  detectDomains: vi.fn(() => []),
}));

vi.mock('./user-signal.js', () => ({
  detectUserSignal: vi.fn(() => null),
}));

// Mock project-registry (must export SENSITIVE_FILE_PATTERNS and SENSITIVE_DIR_PATTERNS
// which container-runner.ts imports to shadow credentials inside project mounts)
vi.mock('./project-registry.js', () => ({
  getProjectById: vi.fn(() => null),
  SENSITIVE_FILE_PATTERNS: [
    '.env',
    '.env.local',
    '.env.development',
    '.env.production',
    '.env.staging',
    '.env.test',
  ],
  SENSITIVE_DIR_PATTERNS: ['credentials', 'secrets'],
}));

// Create a controllable fake ChildProcess
function createFakeProcess() {
  const proc = new EventEmitter() as EventEmitter & {
    stdin: PassThrough;
    stdout: PassThrough;
    stderr: PassThrough;
    kill: ReturnType<typeof vi.fn>;
    pid: number;
  };
  proc.stdin = new PassThrough();
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.kill = vi.fn();
  proc.pid = 12345;
  return proc;
}

let fakeProc: ReturnType<typeof createFakeProcess>;

// Mock child_process.spawn
vi.mock('child_process', async () => {
  const actual =
    await vi.importActual<typeof import('child_process')>('child_process');
  return {
    ...actual,
    spawn: vi.fn(() => fakeProc),
    exec: vi.fn(
      (_cmd: string, _opts: unknown, cb?: (err: Error | null) => void) => {
        if (cb) cb(null);
        return new EventEmitter();
      },
    ),
  };
});

import { runContainerAgent, ContainerOutput } from './container-runner.js';
import type { RegisteredGroup } from './types.js';

const testGroup: RegisteredGroup = {
  name: 'Test Group',
  folder: 'test-group',
  trigger: '@Deus',
  added_at: new Date().toISOString(),
};

const testInput = {
  prompt: 'Hello',
  groupFolder: 'test-group',
  chatJid: 'test@g.us',
  isMain: false,
};

function emitOutputMarker(
  proc: ReturnType<typeof createFakeProcess>,
  output: ContainerOutput,
) {
  const json = JSON.stringify(output);
  proc.stdout.push(`${OUTPUT_START_MARKER}\n${json}\n${OUTPUT_END_MARKER}\n`);
}

// Mock db (getProjectById is imported from db.js in container-runner.ts)
vi.mock('./db.js', () => ({
  getProjectById: vi.fn(() => undefined),
}));

// Mock credential-proxy
vi.mock('./credential-proxy.js', () => ({
  detectAuthMode: vi.fn(() => 'api-key'),
}));

// Mock container-runtime
vi.mock('./container-runtime.js', () => ({
  CONTAINER_HOST_GATEWAY: 'host.docker.internal',
  CONTAINER_RUNTIME_BIN: 'docker',
  hostGatewayArgs: vi.fn(() => []),
  readonlyMountArgs: vi.fn(
    (hostPath: string, containerPath: string) => [
      '-v',
      `${hostPath}:${containerPath}:ro`,
    ],
  ),
}));

describe('container-runner timeout behavior', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    fakeProc = createFakeProcess();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('timeout after output resolves as success', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    // Emit output with a result
    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'Here is my response',
      newSessionId: 'session-123',
    });

    // Let output processing settle
    await vi.advanceTimersByTimeAsync(10);

    // Fire the hard timeout (IDLE_TIMEOUT + 30s = 1830000ms)
    await vi.advanceTimersByTimeAsync(1830000);

    // Emit close event (as if container was stopped by the timeout)
    fakeProc.emit('close', 137);

    // Let the promise resolve
    await vi.advanceTimersByTimeAsync(10);

    const result = await resultPromise;
    expect(result.status).toBe('success');
    expect(result.newSessionId).toBe('session-123');
    expect(onOutput).toHaveBeenCalledWith(
      expect.objectContaining({ result: 'Here is my response' }),
    );
  });

  it('timeout with no output resolves as error', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    // No output emitted — fire the hard timeout
    await vi.advanceTimersByTimeAsync(1830000);

    // Emit close event
    fakeProc.emit('close', 137);

    await vi.advanceTimersByTimeAsync(10);

    const result = await resultPromise;
    expect(result.status).toBe('error');
    expect(result.error).toContain('timed out');
    expect(onOutput).not.toHaveBeenCalled();
  });

  it('normal exit after output resolves as success', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    // Emit output
    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'Done',
      newSessionId: 'session-456',
    });

    await vi.advanceTimersByTimeAsync(10);

    // Normal exit (no timeout)
    fakeProc.emit('close', 0);

    await vi.advanceTimersByTimeAsync(10);

    const result = await resultPromise;
    expect(result.status).toBe('success');
    expect(result.newSessionId).toBe('session-456');
  });
});

// ---------------------------------------------------------------------------
// buildVolumeMounts tests
//
// buildVolumeMounts is not exported, so we exercise it via runContainerAgent.
// We capture the args passed to spawn (which encode all mounts) and the fs
// side-effect calls (mkdirSync, writeFileSync, cpSync) to assert behaviour.
// ---------------------------------------------------------------------------

import * as childProcess from 'child_process';
import * as fsMod from 'fs';
import { getProjectById } from './db.js';

// Helpers to parse mounts out of spawn args
// spawn receives: ['run', '-i', '--rm', '--name', name, ...mountArgs, image]
// Mounts appear as: ['-v', 'host:container'] (writable)
//                or ['-v', 'host:container:ro'] (readonly)
function parseMountsFromSpawnArgs(
  spawnArgs: string[],
): Array<{ hostPath: string; containerPath: string; readonly: boolean }> {
  const mounts: Array<{
    hostPath: string;
    containerPath: string;
    readonly: boolean;
  }> = [];
  for (let i = 0; i < spawnArgs.length; i++) {
    if (spawnArgs[i] === '-v' && i + 1 < spawnArgs.length) {
      const parts = spawnArgs[i + 1].split(':');
      if (parts.length === 3 && parts[2] === 'ro') {
        mounts.push({
          hostPath: parts[0],
          containerPath: parts[1],
          readonly: true,
        });
      } else if (parts.length === 2) {
        mounts.push({
          hostPath: parts[0],
          containerPath: parts[1],
          readonly: false,
        });
      }
      i++;
    }
  }
  return mounts;
}

// Run runContainerAgent with real timers, emit close(0) immediately after spawn
// to resolve the promise without waiting for a real container.
async function runAndCaptureMounts(
  group: RegisteredGroup,
  isMain: boolean,
): Promise<Array<{ hostPath: string; containerPath: string; readonly: boolean }>> {
  fakeProc = createFakeProcess();
  // Wire up the spawn mock BEFORE running so it returns the new proc
  vi.mocked(childProcess.spawn).mockReturnValue(
    fakeProc as ReturnType<typeof childProcess.spawn>,
  );

  // Emit close immediately after the agent sets up listeners (next tick)
  const localProc = fakeProc;
  setImmediate(() => {
    localProc.emit('close', 0);
  });

  const input = {
    prompt: 'test',
    groupFolder: group.folder,
    chatJid: 'test@g.us',
    isMain,
  };

  await runContainerAgent(group, input, () => {});

  const spawnMock = vi.mocked(childProcess.spawn);
  const lastCall = spawnMock.mock.calls[spawnMock.mock.calls.length - 1];
  const args = lastCall[1] as string[];
  return parseMountsFromSpawnArgs(args);
}

describe('buildVolumeMounts — main group', () => {
  beforeEach(() => {
    vi.useRealTimers();
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked(fsMod.default);
    fsMocked.existsSync.mockReset();
    fsMocked.existsSync.mockReturnValue(false);
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();
    fsMocked.statSync.mockReturnValue({ isDirectory: () => false } as ReturnType<typeof fsMod.statSync>);
    if ('cpSync' in fsMocked) {
      (fsMocked as Record<string, ReturnType<typeof vi.fn>>).cpSync = vi.fn();
    }

    vi.mocked(getProjectById).mockReturnValue(undefined);
    vi.mocked(childProcess.spawn).mockReturnValue(fakeProc as ReturnType<typeof childProcess.spawn>);
  });

  it('mounts project root read-only at /workspace/project', async () => {
    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isMain: true,
    };

    const mounts = await runAndCaptureMounts(group, true);

    const projectMount = mounts.find(
      (m) => m.containerPath === '/workspace/project',
    );
    expect(projectMount).toBeDefined();
    expect(projectMount!.readonly).toBe(true);
    expect(projectMount!.hostPath).toBe(process.cwd());
  });

  it('mounts group folder writable at /workspace/group', async () => {
    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isMain: true,
    };

    const mounts = await runAndCaptureMounts(group, true);

    const groupMount = mounts.find(
      (m) => m.containerPath === '/workspace/group',
    );
    expect(groupMount).toBeDefined();
    expect(groupMount!.readonly).toBe(false);
    expect(groupMount!.hostPath).toContain('main-group');
  });

  it('shadows .env with /dev/null when it exists', async () => {
    const fsMocked = vi.mocked(fsMod.default);
    fsMocked.existsSync.mockImplementation((p: unknown) => {
      if (typeof p === 'string' && p.endsWith('/.env')) return true;
      return false;
    });

    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isMain: true,
    };

    const mounts = await runAndCaptureMounts(group, true);

    const envShadow = mounts.find(
      (m) => m.containerPath === '/workspace/project/.env',
    );
    expect(envShadow).toBeDefined();
    expect(envShadow!.hostPath).toBe('/dev/null');
    expect(envShadow!.readonly).toBe(true);
  });

  it('does not shadow .env when it does not exist', async () => {
    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isMain: true,
    };

    // existsSync returns false by default (set in beforeEach)
    const mounts = await runAndCaptureMounts(group, true);

    const envShadow = mounts.find(
      (m) => m.containerPath === '/workspace/project/.env',
    );
    expect(envShadow).toBeUndefined();
  });

  it('creates settings.json with correct feature flags', async () => {
    const fsMocked = vi.mocked(fsMod.default);
    // settings file does not exist yet — trigger writeFileSync
    fsMocked.existsSync.mockReturnValue(false);

    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isMain: true,
    };

    fakeProc = createFakeProcess();
    setImmediate(() => fakeProc.emit('close', 0));
    vi.mocked(childProcess.spawn).mockReturnValue(fakeProc as ReturnType<typeof childProcess.spawn>);

    await runContainerAgent(
      group,
      { prompt: 'test', groupFolder: group.folder, chatJid: 'x@g.us', isMain: true },
      () => {},
    );

    const writeFileCalls = fsMocked.writeFileSync.mock.calls;
    const settingsCall = writeFileCalls.find(
      (args) =>
        typeof args[0] === 'string' && (args[0] as string).endsWith('settings.json'),
    );
    expect(settingsCall).toBeDefined();

    const content = JSON.parse(settingsCall![1] as string);
    expect(content.env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS).toBe('1');
    expect(content.env.CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD).toBe('1');
    expect(content.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY).toBe('0');
  });

  it('syncs skills directory when container/skills/ exists', async () => {
    const fsMocked = vi.mocked(fsMod.default);
    const skillsSrc = `${process.cwd()}/container/skills`;

    fsMocked.existsSync.mockImplementation((p: unknown) => {
      if (p === skillsSrc) return true;
      return false;
    });
    fsMocked.readdirSync.mockImplementation((p: unknown) => {
      if (p === skillsSrc) return ['pdf-skill' as unknown as import('fs').Dirent];
      return [];
    });
    fsMocked.statSync.mockImplementation((p: unknown) => {
      if (typeof p === 'string' && p.includes('pdf-skill')) {
        return { isDirectory: () => true } as ReturnType<typeof fsMod.statSync>;
      }
      return { isDirectory: () => false } as ReturnType<typeof fsMod.statSync>;
    });

    const cpSyncMock = vi.fn();
    (fsMocked as Record<string, unknown>).cpSync = cpSyncMock;

    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isMain: true,
    };

    fakeProc = createFakeProcess();
    setImmediate(() => fakeProc.emit('close', 0));
    vi.mocked(childProcess.spawn).mockReturnValue(fakeProc as ReturnType<typeof childProcess.spawn>);

    await runContainerAgent(
      group,
      { prompt: 'test', groupFolder: group.folder, chatJid: 'x@g.us', isMain: true },
      () => {},
    );

    // cpSync should have been called to sync the skill
    expect(cpSyncMock).toHaveBeenCalled();
    const [src, dst] = cpSyncMock.mock.calls[0];
    expect(src).toContain('pdf-skill');
    expect(dst).toContain('pdf-skill');
  });
});

describe('buildVolumeMounts — non-main group', () => {
  beforeEach(() => {
    vi.useRealTimers();
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked(fsMod.default);
    fsMocked.existsSync.mockReset();
    fsMocked.existsSync.mockReturnValue(false);
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();
    fsMocked.statSync.mockReturnValue({ isDirectory: () => false } as ReturnType<typeof fsMod.statSync>);

    vi.mocked(getProjectById).mockReturnValue(undefined);
    vi.mocked(childProcess.spawn).mockReturnValue(fakeProc as ReturnType<typeof childProcess.spawn>);
  });

  it('does NOT mount project root', async () => {
    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    const mounts = await runAndCaptureMounts(group, false);

    // No /workspace/project unless projectId is set
    const projectMount = mounts.find(
      (m) => m.containerPath === '/workspace/project',
    );
    expect(projectMount).toBeUndefined();
  });

  it('mounts group folder at /workspace/group', async () => {
    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    const mounts = await runAndCaptureMounts(group, false);

    const groupMount = mounts.find(
      (m) => m.containerPath === '/workspace/group',
    );
    expect(groupMount).toBeDefined();
    expect(groupMount!.readonly).toBe(false);
  });

  it('mounts global directory read-only when it exists', async () => {
    const fsMocked = vi.mocked(fsMod.default);
    const globalDir = '/tmp/deus-test-groups/global';

    fsMocked.existsSync.mockImplementation((p: unknown) => {
      return p === globalDir;
    });

    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    const mounts = await runAndCaptureMounts(group, false);

    const globalMount = mounts.find(
      (m) => m.containerPath === '/workspace/global',
    );
    expect(globalMount).toBeDefined();
    expect(globalMount!.hostPath).toBe(globalDir);
    expect(globalMount!.readonly).toBe(true);
  });

  it('does not mount global directory when it does not exist', async () => {
    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    // existsSync returns false by default
    const mounts = await runAndCaptureMounts(group, false);

    const globalMount = mounts.find(
      (m) => m.containerPath === '/workspace/global',
    );
    expect(globalMount).toBeUndefined();
  });

  it('creates per-group IPC subdirectories', async () => {
    const fsMocked = vi.mocked(fsMod.default);

    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    fakeProc = createFakeProcess();
    setImmediate(() => fakeProc.emit('close', 0));
    vi.mocked(childProcess.spawn).mockReturnValue(fakeProc as ReturnType<typeof childProcess.spawn>);

    await runContainerAgent(
      group,
      { prompt: 'test', groupFolder: group.folder, chatJid: 'x@g.us', isMain: false },
      () => {},
    );

    const mkdirCalls = fsMocked.mkdirSync.mock.calls.map((c) => c[0] as string);
    expect(mkdirCalls.some((p) => p.endsWith('/messages'))).toBe(true);
    expect(mkdirCalls.some((p) => p.endsWith('/tasks'))).toBe(true);
    expect(mkdirCalls.some((p) => p.endsWith('/input'))).toBe(true);
  });

  it('mounts IPC directory at /workspace/ipc (writable)', async () => {
    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    const mounts = await runAndCaptureMounts(group, false);

    const ipcMount = mounts.find((m) => m.containerPath === '/workspace/ipc');
    expect(ipcMount).toBeDefined();
    expect(ipcMount!.readonly).toBe(false);
    expect(ipcMount!.hostPath).toContain('ext-group');
  });
});

describe('buildVolumeMounts — external project mounts', () => {
  const PROJECT_PATH = '/home/user/projects/myapp';

  beforeEach(() => {
    vi.useRealTimers();
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked(fsMod.default);
    fsMocked.existsSync.mockReset();
    fsMocked.existsSync.mockReturnValue(false);
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();
    fsMocked.statSync.mockReturnValue({ isDirectory: () => false } as ReturnType<typeof fsMod.statSync>);
    fsMocked.realpathSync.mockReset?.();

    vi.mocked(childProcess.spawn).mockReturnValue(fakeProc as ReturnType<typeof childProcess.spawn>);
  });

  it('mounts real project path when realpath matches registered path', async () => {
    const fsMocked = vi.mocked(fsMod.default);

    vi.mocked(getProjectById).mockReturnValue({
      id: 'proj-1',
      name: 'My App',
      path: PROJECT_PATH,
      type: null,
      readonly: false,
      created_at: new Date().toISOString(),
    });

    fsMocked.existsSync.mockImplementation((p: unknown) => p === PROJECT_PATH);
    fsMocked.realpathSync = vi.fn().mockReturnValue(PROJECT_PATH) as typeof fsMod.realpathSync;

    const group: RegisteredGroup = {
      name: 'App Group',
      folder: 'app-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
      projectId: 'proj-1',
    };

    // Use isMain: false — non-main groups don't mount process.cwd() at
    // /workspace/project, so the only /workspace/project entry comes from
    // the external project mount. This keeps the assertion unambiguous.
    const mounts = await runAndCaptureMounts(group, false);

    const projectMount = mounts.find(
      (m) => m.containerPath === '/workspace/project',
    );
    expect(projectMount).toBeDefined();
    expect(projectMount!.hostPath).toBe(PROJECT_PATH);
  });

  it('blocks mount when realpath differs from registered path (symlink swap)', async () => {
    const fsMocked = vi.mocked(fsMod.default);
    const SYMLINK_TARGET = '/etc/passwd-dir';

    vi.mocked(getProjectById).mockReturnValue({
      id: 'proj-symlink',
      name: 'Legit App',
      path: PROJECT_PATH,
      type: null,
      readonly: false,
      created_at: new Date().toISOString(),
    });

    fsMocked.existsSync.mockImplementation((p: unknown) => p === PROJECT_PATH);
    fsMocked.realpathSync = vi.fn().mockReturnValue(SYMLINK_TARGET) as typeof fsMod.realpathSync;

    const group: RegisteredGroup = {
      name: 'App Group',
      folder: 'app-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
      projectId: 'proj-symlink',
    };

    // Use isMain: false so we can assert no external project at /workspace/project
    const mounts = await runAndCaptureMounts(group, false);

    // Mount should be blocked — no /workspace/project with the symlink target
    const projectMount = mounts.find(
      (m) => m.containerPath === '/workspace/project',
    );
    expect(projectMount).toBeUndefined();
  });

  it('skips mount and warns when project path does not exist', async () => {
    const fsMocked = vi.mocked(fsMod.default);

    vi.mocked(getProjectById).mockReturnValue({
      id: 'proj-missing',
      name: 'Gone App',
      path: '/gone/path',
      type: null,
      readonly: false,
      created_at: new Date().toISOString(),
    });

    // existsSync returns false for project path
    fsMocked.existsSync.mockReturnValue(false);

    const group: RegisteredGroup = {
      name: 'App Group',
      folder: 'app-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
      projectId: 'proj-missing',
    };

    // Use isMain: false to avoid the main group's process.cwd() mount
    const mounts = await runAndCaptureMounts(group, false);

    const projectMount = mounts.find(
      (m) => m.containerPath === '/workspace/project',
    );
    expect(projectMount).toBeUndefined();
  });

  it('project is readonly when project.readonly is true', async () => {
    const fsMocked = vi.mocked(fsMod.default);

    vi.mocked(getProjectById).mockReturnValue({
      id: 'proj-ro',
      name: 'ReadOnly App',
      path: PROJECT_PATH,
      type: null,
      readonly: true,
      created_at: new Date().toISOString(),
    });

    fsMocked.existsSync.mockImplementation((p: unknown) => p === PROJECT_PATH);
    fsMocked.realpathSync = vi.fn().mockReturnValue(PROJECT_PATH) as typeof fsMod.realpathSync;

    const group: RegisteredGroup = {
      name: 'App Group',
      folder: 'app-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
      projectId: 'proj-ro',
    };

    const mounts = await runAndCaptureMounts(group, true);

    const projectMount = mounts.find(
      (m) => m.containerPath === '/workspace/project',
    );
    expect(projectMount).toBeDefined();
    expect(projectMount!.readonly).toBe(true);
  });
});

describe('buildVolumeMounts — sensitive file shadowing', () => {
  const PROJECT_PATH = '/home/user/projects/myapp';

  beforeEach(() => {
    vi.useRealTimers();
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked(fsMod.default);
    fsMocked.existsSync.mockReset();
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();

    vi.mocked(childProcess.spawn).mockReturnValue(fakeProc as ReturnType<typeof childProcess.spawn>);

    // Default project setup for all shadowing tests
    vi.mocked(getProjectById).mockReturnValue({
      id: 'proj-shadow',
      name: 'Shadow App',
      path: PROJECT_PATH,
      type: null,
      readonly: false,
      created_at: new Date().toISOString(),
    });
    fsMocked.realpathSync = vi.fn().mockReturnValue(PROJECT_PATH) as typeof fsMod.realpathSync;
  });

  it('shadows .env file in the project with /dev/null', async () => {
    const fsMocked = vi.mocked(fsMod.default);

    fsMocked.existsSync.mockImplementation((p: unknown) => {
      const s = p as string;
      if (s === PROJECT_PATH) return true;
      if (s === `${PROJECT_PATH}/.env`) return true;
      return false;
    });
    fsMocked.statSync.mockReturnValue({ isDirectory: () => false } as ReturnType<typeof fsMod.statSync>);

    const group: RegisteredGroup = {
      name: 'App',
      folder: 'app-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
      projectId: 'proj-shadow',
    };

    const mounts = await runAndCaptureMounts(group, true);

    const envShadow = mounts.find(
      (m) => m.containerPath === '/workspace/project/.env',
    );
    expect(envShadow).toBeDefined();
    expect(envShadow!.hostPath).toBe('/dev/null');
    expect(envShadow!.readonly).toBe(true);
  });

  it('shadows .aws/credentials dir with empty tmpdir', async () => {
    const fsMocked = vi.mocked(fsMod.default);
    const awsDir = `${PROJECT_PATH}/credentials`;

    fsMocked.existsSync.mockImplementation((p: unknown) => {
      const s = p as string;
      if (s === PROJECT_PATH) return true;
      if (s === awsDir) return true;
      return false;
    });
    fsMocked.statSync.mockImplementation((p: unknown) => {
      if (p === awsDir) return { isDirectory: () => true } as ReturnType<typeof fsMod.statSync>;
      return { isDirectory: () => false } as ReturnType<typeof fsMod.statSync>;
    });

    const group: RegisteredGroup = {
      name: 'App',
      folder: 'app-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
      projectId: 'proj-shadow',
    };

    const mounts = await runAndCaptureMounts(group, true);

    const credShadow = mounts.find(
      (m) => m.containerPath === '/workspace/project/credentials',
    );
    expect(credShadow).toBeDefined();
    expect(credShadow!.readonly).toBe(true);
    // Host path should be an empty shadow dir (not /dev/null for dirs)
    expect(credShadow!.hostPath).not.toBe('/dev/null');
    expect(credShadow!.hostPath).toContain('project-shadows');
  });

  it('creates shadow dir with mkdirSync for sensitive directories', async () => {
    const fsMocked = vi.mocked(fsMod.default);
    const secretsDir = `${PROJECT_PATH}/secrets`;

    fsMocked.existsSync.mockImplementation((p: unknown) => {
      const s = p as string;
      if (s === PROJECT_PATH) return true;
      if (s === secretsDir) return true;
      return false;
    });
    fsMocked.statSync.mockImplementation((p: unknown) => {
      if (p === secretsDir) return { isDirectory: () => true } as ReturnType<typeof fsMod.statSync>;
      return { isDirectory: () => false } as ReturnType<typeof fsMod.statSync>;
    });

    const group: RegisteredGroup = {
      name: 'App',
      folder: 'app-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
      projectId: 'proj-shadow',
    };

    fakeProc = createFakeProcess();
    setImmediate(() => fakeProc.emit('close', 0));
    vi.mocked(childProcess.spawn).mockReturnValue(fakeProc as ReturnType<typeof childProcess.spawn>);

    await runContainerAgent(
      group,
      { prompt: 'test', groupFolder: group.folder, chatJid: 'x@g.us', isMain: true },
      () => {},
    );

    const mkdirCalls = fsMocked.mkdirSync.mock.calls.map((c) => c[0] as string);
    expect(mkdirCalls.some((p) => p.includes('project-shadows'))).toBe(true);
  });
});

describe('buildVolumeMounts — agent-runner source mount', () => {
  beforeEach(() => {
    vi.useRealTimers();
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked(fsMod.default);
    fsMocked.existsSync.mockReset();
    fsMocked.existsSync.mockReturnValue(false);
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();
    fsMocked.statSync.mockReturnValue({ isDirectory: () => false } as ReturnType<typeof fsMod.statSync>);

    vi.mocked(getProjectById).mockReturnValue(undefined);
    vi.mocked(childProcess.spawn).mockReturnValue(fakeProc as ReturnType<typeof childProcess.spawn>);
  });

  it('mounts agent-runner source read-only at /app/src', async () => {
    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
    };

    const mounts = await runAndCaptureMounts(group, true);

    const appSrcMount = mounts.find((m) => m.containerPath === '/app/src');
    expect(appSrcMount).toBeDefined();
    expect(appSrcMount!.readonly).toBe(true);
    expect(appSrcMount!.hostPath).toContain('agent-runner-src');
  });

  it('copies agent-runner source when source dir exists', async () => {
    const fsMocked = vi.mocked(fsMod.default);
    const agentSrc = `${process.cwd()}/container/agent-runner/src`;

    fsMocked.existsSync.mockImplementation((p: unknown) => p === agentSrc);

    const cpSyncMock = vi.fn();
    (fsMocked as Record<string, unknown>).cpSync = cpSyncMock;

    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
    };

    fakeProc = createFakeProcess();
    setImmediate(() => fakeProc.emit('close', 0));
    vi.mocked(childProcess.spawn).mockReturnValue(fakeProc as ReturnType<typeof childProcess.spawn>);

    await runContainerAgent(
      group,
      { prompt: 'test', groupFolder: group.folder, chatJid: 'x@g.us', isMain: true },
      () => {},
    );

    expect(cpSyncMock).toHaveBeenCalledWith(
      agentSrc,
      expect.stringContaining('agent-runner-src'),
      { recursive: true },
    );
  });
});
