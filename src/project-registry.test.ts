import { describe, it, expect, beforeEach, vi } from 'vitest';

// Mocks must come before imports
vi.mock('./config.js', () => ({
  HOME_DIR: '/home/testuser',
}));

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

vi.mock('./mount-security.js', () => ({
  validateMount: vi.fn(),
}));

vi.mock('./db.js', () => ({
  createProject: vi.fn(),
  deleteProject: vi.fn(),
  getAllProjects: vi.fn(() => []),
  getProjectById: vi.fn(() => null),
  getProjectByPath: vi.fn(() => null),
  setGroupProject: vi.fn(),
}));

vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      existsSync: vi.fn(),
      statSync: vi.fn(),
      realpathSync: vi.fn(),
      readFileSync: vi.fn(() => ''),
    },
  };
});

import fs from 'fs';
import { validateMount } from './mount-security.js';
import { getProjectByPath } from './db.js';
import { detectProjectType } from './project-registry.js';

const mockExistsSync = vi.mocked(fs.existsSync);
const mockStatSync = vi.mocked(fs.statSync);
const mockReadFileSync = vi.mocked(fs.readFileSync);
const mockValidateMount = vi.mocked(validateMount);
const mockGetProjectByPath = vi.mocked(getProjectByPath);

beforeEach(() => {
  vi.resetAllMocks();
  // Default: paths exist, are directories, realpathSync returns same path
  mockExistsSync.mockReturnValue(false);
  mockStatSync.mockReturnValue({ isDirectory: () => true } as ReturnType<
    typeof fs.statSync
  >);
  vi.mocked(fs.realpathSync).mockImplementation((p: fs.PathLike) => String(p));
  mockValidateMount.mockReturnValue({
    allowed: true,
    reason: 'ok',
    realHostPath: '/tmp/project',
    resolvedContainerPath: 'project',
    effectiveReadonly: false,
  });
  mockGetProjectByPath.mockReturnValue(null as any);
});

// ── detectProjectType ─────────────────────────────────────────────────────

describe('detectProjectType', () => {
  function setupFiles(files: string[], packageJson?: Record<string, unknown>) {
    mockExistsSync.mockImplementation((p: fs.PathLike) => {
      return files.some((f) => String(p).endsWith(f));
    });
    mockReadFileSync.mockImplementation((p: fs.PathOrFileDescriptor) => {
      if (String(p).endsWith('package.json') && packageJson) {
        return JSON.stringify(packageJson);
      }
      if (String(p).endsWith('requirements.txt')) return '';
      return '';
    });
  }

  it('detects Rust project from Cargo.toml', () => {
    setupFiles(['Cargo.toml']);
    const result = detectProjectType('/tmp/project');
    expect(result?.language).toBe('rust');
    expect(result?.packageManager).toBe('cargo');
  });

  it('detects Go project from go.mod', () => {
    setupFiles(['go.mod']);
    const result = detectProjectType('/tmp/project');
    expect(result?.language).toBe('go');
    expect(result?.testRunner).toBe('go test');
  });

  it('detects Python project from requirements.txt', () => {
    setupFiles(['requirements.txt']);
    const result = detectProjectType('/tmp/project');
    expect(result?.language).toBe('python');
  });

  it('detects Python project from pyproject.toml', () => {
    setupFiles(['pyproject.toml']);
    const result = detectProjectType('/tmp/project');
    expect(result?.language).toBe('python');
    expect(result?.packageManager).toBe('pip');
  });

  it('detects Ruby project from Gemfile', () => {
    setupFiles(['Gemfile']);
    const result = detectProjectType('/tmp/project');
    expect(result?.language).toBe('ruby');
    expect(result?.packageManager).toBe('bundler');
  });

  it('detects Java project from pom.xml', () => {
    setupFiles(['pom.xml']);
    const result = detectProjectType('/tmp/project');
    expect(result?.language).toBe('java');
    expect(result?.packageManager).toBe('maven');
  });

  it('detects TypeScript project from package.json + tsconfig.json', () => {
    setupFiles(['package.json', 'tsconfig.json'], {
      dependencies: { vitest: '1.0' },
    });
    const result = detectProjectType('/tmp/project');
    expect(result?.language).toBe('typescript');
    expect(result?.testRunner).toBe('vitest');
  });

  it('detects JavaScript project from package.json alone', () => {
    setupFiles(['package.json'], { dependencies: {} });
    const result = detectProjectType('/tmp/project');
    expect(result?.language).toBe('javascript');
  });

  it('detects Next.js framework from package.json dependencies', () => {
    setupFiles(['package.json', 'tsconfig.json'], {
      dependencies: { next: '14.0', react: '18.0' },
    });
    const result = detectProjectType('/tmp/project');
    expect(result?.framework).toBe('next.js');
  });

  it('returns null for unknown project (no marker files)', () => {
    setupFiles([]);
    const result = detectProjectType('/tmp/project');
    expect(result).toBeNull();
  });
});
