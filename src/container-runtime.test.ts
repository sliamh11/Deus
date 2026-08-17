import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock logger
vi.mock('./logger.js', () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

// Mock child_process — store the mock fns so tests can configure them
const mockExecSync = vi.fn();
const mockExecFileSync = vi.fn();
vi.mock('child_process', () => ({
  execSync: (...args: unknown[]) => mockExecSync(...args),
  execFileSync: (...args: unknown[]) => mockExecFileSync(...args),
}));

import {
  CONTAINER_RUNTIME_BIN,
  readonlyMountArgs,
  stopContainerSync,
  ensureContainerRuntimeRunning,
  cleanupOrphans,
  _setSleepFnForTests,
} from './container-runtime.js';
import { FatalError } from './errors/index.js';
import { logger } from './logger.js';

beforeEach(() => {
  vi.clearAllMocks();
  _setSleepFnForTests(() => {});
});

// --- Pure functions ---

describe('readonlyMountArgs', () => {
  it('returns -v flag with :ro suffix', () => {
    const args = readonlyMountArgs('/host/path', '/container/path');
    expect(args).toEqual(['-v', '/host/path:/container/path:ro']);
  });
});

describe('stopContainerSync', () => {
  it('calls execFileSync with correct args', () => {
    stopContainerSync('deus-test-123');
    expect(mockExecFileSync).toHaveBeenCalledWith(
      CONTAINER_RUNTIME_BIN,
      ['stop', '-t', '1', 'deus-test-123'],
      { stdio: 'pipe', timeout: 15000 },
    );
  });
});

// --- ensureContainerRuntimeRunning ---

describe('ensureContainerRuntimeRunning', () => {
  it('does nothing when runtime is already running', () => {
    mockExecFileSync.mockReturnValueOnce('');

    ensureContainerRuntimeRunning();

    expect(mockExecFileSync).toHaveBeenCalledWith(
      CONTAINER_RUNTIME_BIN,
      ['info'],
      { stdio: 'pipe', timeout: 10_000 },
    );
    expect(logger.debug).toHaveBeenCalledWith(
      'Container runtime already running',
    );
  });

  it('throws FatalError after all retries exhausted', () => {
    const dockerErr = new Error('Cannot connect to the Docker daemon');
    mockExecFileSync.mockImplementation(() => {
      throw dockerErr;
    });

    expect(() => ensureContainerRuntimeRunning()).toThrow(FatalError);
    // 1 initial + 6 retries = 7 total calls
    expect(mockExecFileSync).toHaveBeenCalledTimes(7);
    expect(logger.error).toHaveBeenCalled();
  });

  it('succeeds after retries when runtime becomes available', () => {
    const dockerErr = new Error('Cannot connect to the Docker daemon');
    mockExecFileSync
      .mockImplementationOnce(() => {
        throw dockerErr;
      })
      .mockImplementationOnce(() => {
        throw dockerErr;
      })
      .mockImplementationOnce(() => {
        throw dockerErr;
      })
      .mockReturnValueOnce('');

    ensureContainerRuntimeRunning();

    expect(mockExecFileSync).toHaveBeenCalledTimes(4);
    expect(logger.info).toHaveBeenCalledWith(
      { attempt: 4 },
      'Container runtime became available after retries',
    );
  });

  it('logs warning on each retry attempt', () => {
    const dockerErr = new Error('Cannot connect to the Docker daemon');
    mockExecFileSync
      .mockImplementationOnce(() => {
        throw dockerErr;
      })
      .mockImplementationOnce(() => {
        throw dockerErr;
      })
      .mockReturnValueOnce('');

    ensureContainerRuntimeRunning();

    expect(logger.warn).toHaveBeenCalledTimes(2);
    expect(logger.warn).toHaveBeenNthCalledWith(
      1,
      { attempt: 1, maxRetries: 6, delayMs: 5_000 },
      'Container runtime not ready, retrying...',
    );
    expect(logger.warn).toHaveBeenNthCalledWith(
      2,
      { attempt: 2, maxRetries: 6, delayMs: 10_000 },
      'Container runtime not ready, retrying...',
    );
  });
});

// --- cleanupOrphans ---

// Instance ids are always 8 lowercase hex (see config.deusInstanceId).
const MINE = 'a1b2c3d4';
const THEIRS = '99887766';

describe('cleanupOrphans', () => {
  it('stops orphaned deus containers stamped with this instance', () => {
    // docker ps returns container names, one per line
    mockExecFileSync.mockReturnValueOnce(
      `deus-group1-111-i${MINE}\ndeus-group2-222-i${MINE}\n`,
    );

    cleanupOrphans(MINE);

    // ps call + 2 stop calls, all via execFileSync
    expect(mockExecFileSync).toHaveBeenCalledTimes(3);
    expect(mockExecFileSync).toHaveBeenNthCalledWith(
      1,
      CONTAINER_RUNTIME_BIN,
      ['ps', '--filter', 'name=deus-', '--format', '{{.Names}}'],
      { stdio: ['pipe', 'pipe', 'pipe'], encoding: 'utf-8' },
    );
    expect(mockExecFileSync).toHaveBeenNthCalledWith(
      2,
      CONTAINER_RUNTIME_BIN,
      ['stop', '-t', '1', `deus-group1-111-i${MINE}`],
      { stdio: 'pipe', timeout: 15000 },
    );
    expect(mockExecFileSync).toHaveBeenNthCalledWith(
      3,
      CONTAINER_RUNTIME_BIN,
      ['stop', '-t', '1', `deus-group2-222-i${MINE}`],
      { stdio: 'pipe', timeout: 15000 },
    );
    expect(logger.info).toHaveBeenCalledWith(
      {
        count: 2,
        names: [`deus-group1-111-i${MINE}`, `deus-group2-222-i${MINE}`],
      },
      'Stopped orphaned containers',
    );
  });

  it('leaves containers belonging to another live instance alone (LIA-491)', () => {
    mockExecFileSync.mockReturnValueOnce(
      `deus-mine-1-i${MINE}\ndeus-theirs-2-i${THEIRS}\n`,
    );

    cleanupOrphans(MINE);

    // ps + exactly one stop: only ours.
    expect(mockExecFileSync).toHaveBeenCalledTimes(2);
    expect(mockExecFileSync).toHaveBeenNthCalledWith(
      2,
      CONTAINER_RUNTIME_BIN,
      ['stop', '-t', '1', `deus-mine-1-i${MINE}`],
      { stdio: 'pipe', timeout: 15000 },
    );
    expect(logger.info).toHaveBeenCalledWith(
      { count: 1, names: [`deus-mine-1-i${MINE}`] },
      'Stopped orphaned containers',
    );
  });

  it('warns about unstamped pre-LIA-491 containers without stopping them', () => {
    mockExecFileSync.mockReturnValueOnce('deus-legacy-111\n');

    cleanupOrphans(MINE);

    // ps only — nothing stopped.
    expect(mockExecFileSync).toHaveBeenCalledTimes(1);
    expect(logger.info).not.toHaveBeenCalled();
    expect(logger.warn).toHaveBeenCalledWith(
      { count: 1, names: ['deus-legacy-111'] },
      expect.stringContaining('unstamped'),
    );
  });

  it('does not treat a group folder that looks like an instance id as a stamp', () => {
    // A folder legally named `iabcdef12-...` produces this legacy name. It must
    // be classified as unstamped, not mistaken for another instance (LIA-491).
    mockExecFileSync.mockReturnValueOnce('deus-iabcdef12-mygroup-111\n');

    cleanupOrphans(MINE);

    expect(mockExecFileSync).toHaveBeenCalledTimes(1);
    expect(logger.warn).toHaveBeenCalledWith(
      { count: 1, names: ['deus-iabcdef12-mygroup-111'] },
      expect.stringContaining('unstamped'),
    );
  });

  it('does nothing when no orphans exist', () => {
    mockExecFileSync.mockReturnValueOnce('');

    cleanupOrphans(MINE);

    expect(mockExecFileSync).toHaveBeenCalledTimes(1);
    expect(logger.info).not.toHaveBeenCalled();
  });

  it('warns and continues when ps fails', () => {
    mockExecFileSync.mockImplementationOnce(() => {
      throw new Error('docker not available');
    });

    cleanupOrphans(MINE); // should not throw

    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({ err: expect.any(Error) }),
      'Failed to clean up orphaned containers',
    );
  });

  it('continues stopping remaining containers when one stop fails', () => {
    // ps call returns two orphans, both stamped as ours so both are eligible —
    // this test's subject is partial-failure recovery, not ownership.
    mockExecFileSync.mockReturnValueOnce(
      `deus-a-1-i${MINE}\ndeus-b-2-i${MINE}\n`,
    );
    // First stop fails
    mockExecFileSync.mockImplementationOnce(() => {
      throw new Error('already stopped');
    });
    // Second stop succeeds
    mockExecFileSync.mockReturnValueOnce('');

    cleanupOrphans(MINE); // should not throw

    expect(mockExecFileSync).toHaveBeenCalledTimes(3);
    expect(logger.info).toHaveBeenCalledWith(
      { count: 2, names: [`deus-a-1-i${MINE}`, `deus-b-2-i${MINE}`] },
      'Stopped orphaned containers',
    );
  });
});
