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
} from './container-runtime.js';
import { logger } from './logger.js';

beforeEach(() => {
  vi.clearAllMocks();
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
      { stdio: 'pipe', timeout: 10000 },
    );
    expect(logger.debug).toHaveBeenCalledWith(
      'Container runtime already running',
    );
  });

  it('throws when docker info fails', () => {
    mockExecFileSync.mockImplementationOnce(() => {
      throw new Error('Cannot connect to the Docker daemon');
    });

    expect(() => ensureContainerRuntimeRunning()).toThrow(
      'Container runtime is required but failed to start',
    );
    expect(logger.error).toHaveBeenCalled();
  });
});

// --- cleanupOrphans ---

describe('cleanupOrphans', () => {
  it('stops orphaned deus containers', () => {
    // docker ps returns container names, one per line
    mockExecFileSync.mockReturnValueOnce('deus-group1-111\ndeus-group2-222\n');

    cleanupOrphans();

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
      ['stop', '-t', '1', 'deus-group1-111'],
      { stdio: 'pipe', timeout: 15000 },
    );
    expect(mockExecFileSync).toHaveBeenNthCalledWith(
      3,
      CONTAINER_RUNTIME_BIN,
      ['stop', '-t', '1', 'deus-group2-222'],
      { stdio: 'pipe', timeout: 15000 },
    );
    expect(logger.info).toHaveBeenCalledWith(
      { count: 2, names: ['deus-group1-111', 'deus-group2-222'] },
      'Stopped orphaned containers',
    );
  });

  it('does nothing when no orphans exist', () => {
    mockExecFileSync.mockReturnValueOnce('');

    cleanupOrphans();

    expect(mockExecFileSync).toHaveBeenCalledTimes(1);
    expect(logger.info).not.toHaveBeenCalled();
  });

  it('warns and continues when ps fails', () => {
    mockExecFileSync.mockImplementationOnce(() => {
      throw new Error('docker not available');
    });

    cleanupOrphans(); // should not throw

    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({ err: expect.any(Error) }),
      'Failed to clean up orphaned containers',
    );
  });

  it('continues stopping remaining containers when one stop fails', () => {
    // ps call returns two orphans
    mockExecFileSync.mockReturnValueOnce('deus-a-1\ndeus-b-2\n');
    // First stop fails
    mockExecFileSync.mockImplementationOnce(() => {
      throw new Error('already stopped');
    });
    // Second stop succeeds
    mockExecFileSync.mockReturnValueOnce('');

    cleanupOrphans(); // should not throw

    expect(mockExecFileSync).toHaveBeenCalledTimes(3);
    expect(logger.info).toHaveBeenCalledWith(
      { count: 2, names: ['deus-a-1', 'deus-b-2'] },
      'Stopped orphaned containers',
    );
  });
});
