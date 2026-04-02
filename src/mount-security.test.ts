import { describe, it, expect, beforeEach, vi } from 'vitest';
import os from 'os';
import path from 'path';

// Mock config before importing the module
vi.mock('./config.js', () => ({
  HOME_DIR: '/home/testuser',
  MOUNT_ALLOWLIST_PATH: '/home/testuser/.config/deus/mount-allowlist.json',
}));

// Mock logger
vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

// Mock fs entirely
vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      existsSync: vi.fn(),
      readFileSync: vi.fn(),
      realpathSync: vi.fn(),
    },
  };
});

import fs from 'fs';
import {
  _resetAllowlistCacheForTests,
  loadMountAllowlist,
  validateMount,
  validateAdditionalMounts,
} from './mount-security.js';

const mockExistsSync = vi.mocked(fs.existsSync);
const mockReadFileSync = vi.mocked(fs.readFileSync);
const mockRealpathSync = vi.mocked(fs.realpathSync);

beforeEach(() => {
  _resetAllowlistCacheForTests();
  vi.resetAllMocks();
});

// ── loadMountAllowlist ──────────────────────────────────────────────────────

describe('loadMountAllowlist', () => {
  it('returns null when allowlist file does not exist', () => {
    mockExistsSync.mockReturnValue(false);
    expect(loadMountAllowlist()).toBeNull();
  });

  it('returns null when allowlist file has invalid JSON', () => {
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync.mockReturnValue('not valid json');
    expect(loadMountAllowlist()).toBeNull();
  });

  it('returns null when allowedRoots is not an array', () => {
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        allowedRoots: 'bad',
        blockedPatterns: [],
        nonMainReadOnly: true,
      }),
    );
    expect(loadMountAllowlist()).toBeNull();
  });

  it('returns null when blockedPatterns is not an array', () => {
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        allowedRoots: [],
        blockedPatterns: 'bad',
        nonMainReadOnly: true,
      }),
    );
    expect(loadMountAllowlist()).toBeNull();
  });

  it('returns null when nonMainReadOnly is not a boolean', () => {
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        allowedRoots: [],
        blockedPatterns: [],
        nonMainReadOnly: 'yes',
      }),
    );
    expect(loadMountAllowlist()).toBeNull();
  });

  it('loads and caches a valid allowlist', () => {
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        allowedRoots: [
          { path: '/home/testuser/projects', allowReadWrite: true },
        ],
        blockedPatterns: [],
        nonMainReadOnly: false,
      }),
    );
    const result = loadMountAllowlist();
    expect(result).not.toBeNull();
    expect(result!.allowedRoots).toHaveLength(1);

    // Second call should return cached result (readFileSync called only once)
    loadMountAllowlist();
    expect(mockReadFileSync).toHaveBeenCalledTimes(1);
  });

  it('merges default blocked patterns with user-provided ones', () => {
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        allowedRoots: [],
        blockedPatterns: ['my-custom-secret'],
        nonMainReadOnly: false,
      }),
    );
    const result = loadMountAllowlist();
    expect(result).not.toBeNull();
    // Default patterns included
    expect(result!.blockedPatterns).toContain('.ssh');
    expect(result!.blockedPatterns).toContain('credentials');
    // User pattern also included
    expect(result!.blockedPatterns).toContain('my-custom-secret');
  });
});

// ── validateMount ──────────────────────────────────────────────────────────

describe('validateMount', () => {
  function setupAllowlist(
    extra: Partial<{
      allowedRoots: {
        path: string;
        allowReadWrite: boolean;
        description?: string;
      }[];
      blockedPatterns: string[];
      nonMainReadOnly: boolean;
    }> = {},
  ) {
    mockExistsSync.mockImplementation((p: fs.PathLike) => {
      // allowlist file exists
      if (String(p).includes('mount-allowlist')) return true;
      // host path exists
      return true;
    });
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        allowedRoots: [
          {
            path: '/home/testuser/projects',
            allowReadWrite: true,
            description: 'Dev projects',
          },
        ],
        blockedPatterns: [],
        nonMainReadOnly: false,
        ...extra,
      }),
    );
    // realpathSync returns the path as-is (no symlinks in test)
    mockRealpathSync.mockImplementation((p: fs.PathLike) => String(p));
  }

  it('blocks all mounts when no allowlist exists', () => {
    mockExistsSync.mockReturnValue(false);
    const result = validateMount(
      { hostPath: '/home/testuser/projects/myapp' },
      true,
    );
    expect(result.allowed).toBe(false);
    expect(result.reason).toContain('No mount allowlist');
  });

  it('blocks paths matching default blocked patterns (.ssh)', () => {
    setupAllowlist();
    const result = validateMount(
      { hostPath: '/home/testuser/projects/.ssh/config' },
      true,
    );
    expect(result.allowed).toBe(false);
    expect(result.reason).toContain('.ssh');
  });

  it('blocks paths not under any allowed root', () => {
    setupAllowlist();
    const result = validateMount({ hostPath: '/etc/passwd' }, true);
    expect(result.allowed).toBe(false);
    expect(result.reason).toContain('not under any allowed root');
  });

  it('allows a path under an allowed root', () => {
    setupAllowlist();
    const result = validateMount(
      { hostPath: '/home/testuser/projects/myapp' },
      true,
    );
    expect(result.allowed).toBe(true);
    // path.resolve adds a drive letter on Windows; resolve both sides so they match
    expect(path.resolve(result.realHostPath!)).toBe(
      path.resolve('/home/testuser/projects/myapp'),
    );
  });

  it('defaults to readonly when mount.readonly is not explicitly false', () => {
    setupAllowlist();
    const result = validateMount(
      { hostPath: '/home/testuser/projects/myapp' },
      true,
    );
    expect(result.effectiveReadonly).toBe(true);
  });

  it('allows read-write for main group when root permits it', () => {
    setupAllowlist();
    const result = validateMount(
      { hostPath: '/home/testuser/projects/myapp', readonly: false },
      true,
    );
    expect(result.allowed).toBe(true);
    expect(result.effectiveReadonly).toBe(false);
  });

  it('forces readonly for non-main group when nonMainReadOnly is true', () => {
    setupAllowlist({ nonMainReadOnly: true });
    const result = validateMount(
      { hostPath: '/home/testuser/projects/myapp', readonly: false },
      false, // isMain=false
    );
    expect(result.allowed).toBe(true);
    expect(result.effectiveReadonly).toBe(true);
  });

  it('forces readonly when root does not allow read-write', () => {
    setupAllowlist({
      allowedRoots: [
        { path: '/home/testuser/projects', allowReadWrite: false },
      ],
      nonMainReadOnly: false,
    });
    const result = validateMount(
      { hostPath: '/home/testuser/projects/myapp', readonly: false },
      true,
    );
    expect(result.allowed).toBe(true);
    expect(result.effectiveReadonly).toBe(true);
  });

  it('blocks paths where host does not exist (realpathSync throws)', () => {
    mockExistsSync.mockImplementation((p: fs.PathLike) => {
      if (String(p).includes('mount-allowlist')) return true;
      return true;
    });
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        allowedRoots: [
          { path: '/home/testuser/projects', allowReadWrite: true },
        ],
        blockedPatterns: [],
        nonMainReadOnly: false,
      }),
    );
    // realpathSync throws for paths that don't exist on disk
    mockRealpathSync.mockImplementation((p: fs.PathLike) => {
      if (String(p).includes('nonexistent')) throw new Error('ENOENT');
      return String(p);
    });
    const result = validateMount(
      { hostPath: '/home/testuser/projects/nonexistent' },
      true,
    );
    expect(result.allowed).toBe(false);
    expect(result.reason).toContain('does not exist');
  });

  it('rejects invalid container path with path traversal', () => {
    setupAllowlist();
    const result = validateMount(
      { hostPath: '/home/testuser/projects/myapp', containerPath: '../escape' },
      true,
    );
    expect(result.allowed).toBe(false);
    expect(result.reason).toContain('Invalid container path');
  });

  it('derives containerPath from hostPath basename when not specified', () => {
    setupAllowlist();
    const result = validateMount(
      { hostPath: '/home/testuser/projects/myapp' },
      true,
    );
    expect(result.resolvedContainerPath).toBe('myapp');
  });
});

// ── validateAdditionalMounts ──────────────────────────────────────────────

describe('validateAdditionalMounts', () => {
  it('returns empty array when all mounts are rejected', () => {
    mockExistsSync.mockReturnValue(false); // no allowlist
    const result = validateAdditionalMounts(
      [{ hostPath: '/home/testuser/projects/foo' }],
      'test-group',
      true,
    );
    expect(result).toHaveLength(0);
  });

  it('returns validated mounts with /workspace/extra/ prefix on containerPath', () => {
    mockExistsSync.mockReturnValue(true);
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        allowedRoots: [
          { path: '/home/testuser/projects', allowReadWrite: false },
        ],
        blockedPatterns: [],
        nonMainReadOnly: false,
      }),
    );
    mockRealpathSync.mockImplementation((p: fs.PathLike) => String(p));

    const result = validateAdditionalMounts(
      [{ hostPath: '/home/testuser/projects/myapp' }],
      'test-group',
      true,
    );
    expect(result).toHaveLength(1);
    expect(result[0].containerPath).toBe('/workspace/extra/myapp');
  });
});
