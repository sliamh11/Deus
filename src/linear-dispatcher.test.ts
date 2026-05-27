import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs';
import path from 'path';
import os from 'os';

const execFileMock = vi.fn();

vi.mock('child_process', async (importOriginal) => {
  const orig = await importOriginal<typeof import('child_process')>();
  const { promisify } = await import('util');
  type ExecFileCb = (err: Error | null, stdout: string, stderr: string) => void;
  const mockFn = vi.fn((...args: unknown[]) => {
    const cb = args[args.length - 1];
    if (typeof cb === 'function') {
      const result = execFileMock(args[0], args[1]);
      if (result?.error) (cb as ExecFileCb)(result.error, '', '');
      else (cb as ExecFileCb)(null, result?.stdout ?? '', result?.stderr ?? '');
    }
  });
  // promisify(execFile) uses a custom symbol to return { stdout, stderr }
  (mockFn as unknown as Record<symbol, unknown>)[promisify.custom] = (
    ...args: unknown[]
  ) => {
    const result = execFileMock(args[0], args[1]);
    if (result?.error) return Promise.reject(result.error);
    return Promise.resolve({
      stdout: result?.stdout ?? '',
      stderr: result?.stderr ?? '',
    });
  };
  return { ...orig, execFile: mockFn };
});

vi.mock('./config.js', async () => {
  const actual =
    await vi.importActual<typeof import('./config.js')>('./config.js');
  const { join } = await import('path');
  const { tmpdir } = await import('os');
  return {
    ...actual,
    PROJECT_ROOT: join(tmpdir(), `deus-test-${process.pid}`),
  };
});

vi.mock('./db.js', () => ({
  CIRCUIT_BREAKER_THRESHOLD: 3,
  getConsecutiveFailCount: vi.fn().mockReturnValue(0),
  getIssuePr: vi.fn().mockReturnValue(null),
  getLastFailTime: vi.fn().mockReturnValue(null),
  getPipelineEvents: vi.fn().mockReturnValue([]),
  logPipelineEvent: vi.fn(),
  upsertIssuePr: vi.fn(),
  getOpenPrsForActiveIssues: vi.fn().mockReturnValue([]),
}));

vi.mock('./linear-notifications.js', () => ({
  notifyPipelineStep: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('./platform.js', async (importOriginal) => {
  const orig = await importOriginal<typeof import('./platform.js')>();
  return { ...orig, IS_MACOS: true, IS_LINUX: false };
});

const TEST_PROJECT_ROOT = path.join(os.tmpdir(), `deus-test-${process.pid}`);

import {
  loadRoleSpecs,
  buildIssuePrompt,
  startLinearDispatcher,
  stopLinearDispatcher,
  truncateComments,
  extractScopeBlock,
  applyPatchArtifact,
} from './linear-dispatcher.js';
import type {
  LinearContext,
  LinearDispatcherDependencies,
} from './linear-dispatcher.js';
import { RuntimeRegistry } from './agent-runtimes/registry.js';

function makeMockDeps(
  overrides: Partial<LinearDispatcherDependencies> = {},
): LinearDispatcherDependencies {
  return {
    registeredGroups: () => ({}),
    registerGroup: vi.fn(),
    registry: new RuntimeRegistry(),
    queue: {
      enqueueTask: vi.fn(),
      notifyIdle: vi.fn(),
      closeStdin: vi.fn(),
      availableSlots: vi.fn().mockReturnValue(5),
    } as unknown as LinearDispatcherDependencies['queue'],
    ...overrides,
  };
}

function makeMockCtx(overrides: Partial<LinearContext> = {}): LinearContext {
  const deps = makeMockDeps();
  return {
    client: {} as LinearContext['client'],
    stateByName: new Map([
      ['Ready for Agent', { id: 'ready-id', name: 'Ready for Agent' }],
      ['Agent Working', { id: 'working-id', name: 'Agent Working' }],
      ['In Review', { id: 'review-id', name: 'In Review' }],
      ['Backlog', { id: 'backlog-id', name: 'Backlog' }],
    ]),
    stateById: new Map([
      ['ready-id', { id: 'ready-id', name: 'Ready for Agent' }],
      ['working-id', { id: 'working-id', name: 'Agent Working' }],
      ['review-id', { id: 'review-id', name: 'In Review' }],
      ['backlog-id', { id: 'backlog-id', name: 'Backlog' }],
    ]),
    botUserId: 'bot-user-id',
    viewerId: 'viewer-user-id',
    deps,
    dispatchGroup: {
      name: 'Linear Dispatch',
      folder: 'linear-dispatch',
      trigger: '',
      added_at: new Date().toISOString(),
      requiresTrigger: false,
      isControlGroup: false,
    },
    inFlightDispatch: new Set(),
    inFlightGate: new Set(),
    gateLabels: { effort: {}, complexity: {} },
    teamId: 'team-id',
    vaultPath: null,
    ...overrides,
  };
}

describe('loadRoleSpecs', () => {
  const tmpDir = path.join(process.cwd(), '.test-agents-tmp');

  beforeEach(() => {
    fs.mkdirSync(tmpDir, { recursive: true });
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('parses frontmatter and extracts linear_label', () => {
    fs.writeFileSync(
      path.join(tmpDir, 'test-role.md'),
      `---
name: test-role
description: A test role
model: sonnet
linear_label: "agent:test-role"
---

You are a test role agent.`,
    );

    const specs = loadRoleSpecs(tmpDir);
    expect(specs.size).toBe(1);
    expect(specs.has('agent:test-role')).toBe(true);

    const spec = specs.get('agent:test-role')!;
    expect(spec.name).toBe('test-role');
    expect(spec.model).toBe('sonnet');
    expect(spec.content).toBe('You are a test role agent.');
  });

  it('skips files without linear_label', () => {
    fs.writeFileSync(
      path.join(tmpDir, 'no-label.md'),
      `---
name: no-label
description: No linear label
---

Some content.`,
    );
    fs.writeFileSync(
      path.join(tmpDir, 'has-label.md'),
      `---
name: has-label
linear_label: "agent:has-label"
---

Content.`,
    );

    const specs = loadRoleSpecs(tmpDir);
    expect(specs.size).toBe(1);
    expect(specs.has('agent:has-label')).toBe(true);
  });

  it('returns empty map for nonexistent directory', () => {
    const specs = loadRoleSpecs('/nonexistent/path');
    expect(specs.size).toBe(0);
  });
});

describe('buildIssuePrompt', () => {
  const role = {
    label: 'agent:test',
    name: 'test',
    content: 'You are a test agent.',
  };

  it('includes role, title, description', () => {
    const prompt = buildIssuePrompt(
      role,
      'Fix the bug',
      'LIA-42',
      'There is a bug in the login flow.',
      [],
    );
    expect(prompt).toContain('<role>');
    expect(prompt).toContain('You are a test agent.');
    expect(prompt).toContain('Title: Fix the bug');
    expect(prompt).toContain('ID: LIA-42');
    expect(prompt).toContain('There is a bug in the login flow.');
    expect(prompt).not.toContain('<comments>');
  });

  it('includes comments when present', () => {
    const prompt = buildIssuePrompt(role, 'Task', 'LIA-1', 'Desc', [
      { author: 'Alice', body: 'Check the auth module' },
    ]);
    expect(prompt).toContain('<comments>');
    expect(prompt).toContain('[Alice]: Check the auth module');
  });

  it('handles missing description', () => {
    const prompt = buildIssuePrompt(role, 'Task', 'LIA-1', undefined, []);
    expect(prompt).toContain('(no description)');
  });
});

describe('startLinearDispatcher', () => {
  afterEach(() => {
    stopLinearDispatcher();
    vi.unstubAllEnvs();
  });

  it('goes dormant when no role specs have linear_label', () => {
    const ctx = makeMockCtx();
    startLinearDispatcher(ctx);
    // No timer started because no role specs exist
  });
});

describe('pollLinear dispatch ordering', () => {
  const agentsDir = path.join(TEST_PROJECT_ROOT, '.claude', 'agents');

  beforeEach(() => {
    vi.useFakeTimers();
    fs.mkdirSync(agentsDir, { recursive: true });
    fs.writeFileSync(
      path.join(agentsDir, 'test-role.md'),
      `---\nname: test-role\nlinear_label: "agent:test"\n---\nYou are a test agent.`,
    );
  });

  afterEach(() => {
    stopLinearDispatcher();
    vi.unstubAllEnvs();
    vi.useRealTimers();
    fs.rmSync(TEST_PROJECT_ROOT, { recursive: true, force: true });
  });

  function makeIssue(id: string, sortOrder: number, labelName = 'agent:test') {
    return {
      id,
      title: `Issue ${id}`,
      identifier: `LIA-${id}`,
      description: 'test',
      sortOrder,
      labels: vi.fn().mockResolvedValue({
        nodes: [{ name: labelName }],
      }),
      comments: vi.fn().mockResolvedValue({ nodes: [] }),
    };
  }

  it('dispatches issues in sortOrder ASC', async () => {
    const enqueueTask = vi.fn();
    const deps = makeMockDeps({
      queue: {
        enqueueTask,
        notifyIdle: vi.fn(),
        closeStdin: vi.fn(),
        availableSlots: vi.fn().mockReturnValue(5),
      } as unknown as LinearDispatcherDependencies['queue'],
    });

    const issues = [
      makeIssue('ccc', 3.0),
      makeIssue('aaa', 1.0),
      makeIssue('bbb', 2.0),
    ];

    const ctx = makeMockCtx({
      deps,
      client: {
        issues: vi.fn().mockResolvedValue({ nodes: issues }),
        updateIssue: vi.fn().mockResolvedValue({}),
      } as unknown as LinearContext['client'],
    });

    startLinearDispatcher(ctx);
    await vi.advanceTimersByTimeAsync(100);

    expect(enqueueTask).toHaveBeenCalledTimes(3);
    const callOrder = enqueueTask.mock.calls.map(
      (call: unknown[]) => call[1] as string,
    );
    expect(callOrder).toEqual(['aaa', 'bbb', 'ccc']);
  });

  it('throttles dispatch to available slots', async () => {
    const enqueueTask = vi.fn();
    const deps = makeMockDeps({
      queue: {
        enqueueTask,
        notifyIdle: vi.fn(),
        closeStdin: vi.fn(),
        availableSlots: vi.fn().mockReturnValue(2),
      } as unknown as LinearDispatcherDependencies['queue'],
    });

    const issues = [
      makeIssue('aaa', 1.0),
      makeIssue('bbb', 2.0),
      makeIssue('ccc', 3.0),
      makeIssue('ddd', 4.0),
    ];

    const ctx = makeMockCtx({
      deps,
      client: {
        issues: vi.fn().mockResolvedValue({ nodes: issues }),
        updateIssue: vi.fn().mockResolvedValue({}),
      } as unknown as LinearContext['client'],
    });

    startLinearDispatcher(ctx);
    await vi.advanceTimersByTimeAsync(100);

    expect(enqueueTask).toHaveBeenCalledTimes(2);
    const callOrder = enqueueTask.mock.calls.map(
      (call: unknown[]) => call[1] as string,
    );
    expect(callOrder).toEqual(['aaa', 'bbb']);
  });
});

describe('truncateComments', () => {
  it('returns empty array for empty input', () => {
    expect(truncateComments([])).toEqual([]);
  });

  it('preserves all comments when under budget', () => {
    const comments = [
      { author: 'Alice', body: 'First' },
      { author: 'Bob', body: 'Second' },
    ];
    expect(truncateComments(comments)).toEqual(comments);
  });

  it('always preserves gate verdict comments', () => {
    const comments = [
      { author: 'Bot', body: '**Warden: readiness-gate** - SHIP\nLooks good' },
      { author: 'Alice', body: 'old comment 1' },
      { author: 'Bob', body: 'old comment 2' },
      { author: 'Carol', body: 'old comment 3' },
      { author: 'Dave', body: 'recent 1' },
      { author: 'Eve', body: 'recent 2' },
      { author: 'Frank', body: 'recent 3' },
    ];
    const result = truncateComments(comments, 50);
    const bodies = result.map((c) => c.body);
    expect(bodies).toContain('**Warden: readiness-gate** - SHIP\nLooks good');
  });

  it('always preserves the most recent 3 regular comments', () => {
    const comments = [
      { author: 'A', body: 'old 1' },
      { author: 'B', body: 'old 2' },
      { author: 'C', body: 'recent 1' },
      { author: 'D', body: 'recent 2' },
      { author: 'E', body: 'recent 3' },
    ];
    const result = truncateComments(comments, 10);
    const bodies = result.map((c) => c.body);
    expect(bodies).toContain('recent 1');
    expect(bodies).toContain('recent 2');
    expect(bodies).toContain('recent 3');
  });

  it('drops oldest non-gate comments when over budget', () => {
    const comments = [
      { author: 'A', body: 'x'.repeat(20000) },
      { author: 'B', body: 'y'.repeat(20000) },
      { author: 'C', body: 'recent 1' },
      { author: 'D', body: 'recent 2' },
      { author: 'E', body: 'recent 3' },
    ];
    const result = truncateComments(comments, 100);
    expect(result[0]).toEqual({
      author: 'System',
      body: '[2 earlier comments omitted]',
    });
    expect(result.length).toBe(4); // omission marker + 3 recent
  });

  it('includes omission marker when older comments are dropped', () => {
    const comments = [
      { author: 'A', body: 'old' },
      { author: 'B', body: 'old' },
      { author: 'C', body: 'r1' },
      { author: 'D', body: 'r2' },
      { author: 'E', body: 'r3' },
    ];
    const result = truncateComments(comments, 1);
    expect(result[0].body).toMatch(/earlier comments omitted/);
  });
});

describe('extractScopeBlock', () => {
  it('extracts content between scope markers', () => {
    const desc = `Some intro text

<!-- gate:agent-readiness-gate:start -->
## Scope
- Fix the login bug
- Add tests
<!-- gate:agent-readiness-gate:end -->

<!-- gate:output-quality-gate:start -->
Quality feedback here
<!-- gate:output-quality-gate:end -->`;

    const result = extractScopeBlock(desc);
    expect(result).toContain('Fix the login bug');
    expect(result).toContain('Add tests');
    expect(result).not.toContain('Quality feedback');
    expect(result).not.toContain('Some intro text');
  });

  it('returns full description when no scope block exists', () => {
    const desc = 'Just a plain description with no markers.';
    expect(extractScopeBlock(desc)).toBe(desc);
  });

  it('returns full description with malformed markers (start only)', () => {
    const desc = 'Text <!-- gate:agent-readiness-gate:start --> partial block';
    expect(extractScopeBlock(desc)).toBe(desc);
  });

  it('returns full description with malformed markers (end only)', () => {
    const desc = 'Text <!-- gate:agent-readiness-gate:end --> partial block';
    expect(extractScopeBlock(desc)).toBe(desc);
  });

  it('returns full description when end comes before start', () => {
    const desc =
      '<!-- gate:agent-readiness-gate:end -->before<!-- gate:agent-readiness-gate:start -->';
    expect(extractScopeBlock(desc)).toBe(desc);
  });
});

describe('applyPatchArtifact', () => {
  const patchGroupDir = path.join(
    TEST_PROJECT_ROOT,
    'groups',
    'linear-dispatch',
  );
  const createComment = vi.fn().mockResolvedValue({});

  function patchCtx(): LinearContext {
    return makeMockCtx({
      client: {
        createComment,
        updateIssue: vi.fn().mockResolvedValue({}),
      } as unknown as LinearContext['client'],
      repoSlug: 'test/repo',
    });
  }

  beforeEach(() => {
    execFileMock.mockReset();
    createComment.mockClear();
    fs.mkdirSync(patchGroupDir, { recursive: true });
  });

  afterEach(() => {
    fs.rmSync(patchGroupDir, { recursive: true, force: true });
  });

  it('returns no-op when no patch files exist', async () => {
    const result = await applyPatchArtifact(
      patchGroupDir,
      'LIA-99',
      'issue-id',
      patchCtx(),
    );
    expect(result).toEqual({ prUrl: null, applied: false });
    expect(execFileMock).not.toHaveBeenCalled();
  });

  it('returns no-op when group dir does not exist', async () => {
    const result = await applyPatchArtifact(
      '/nonexistent/dir',
      'LIA-99',
      'issue-id',
      patchCtx(),
    );
    expect(result).toEqual({ prUrl: null, applied: false });
  });

  it('skips .applied patch files', async () => {
    fs.writeFileSync(
      path.join(patchGroupDir, 'LIA-99.patch.applied'),
      'old patch',
    );
    const result = await applyPatchArtifact(
      patchGroupDir,
      'LIA-99',
      'issue-id',
      patchCtx(),
    );
    expect(result).toEqual({ prUrl: null, applied: false });
  });

  it('refuses when not on main branch', async () => {
    fs.writeFileSync(
      path.join(patchGroupDir, 'LIA-99.patch'),
      'diff --git ...',
    );
    execFileMock.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git' && args[0] === 'rev-parse')
        return { stdout: 'feat/other\n' };
      return { stdout: '' };
    });

    const ctx = patchCtx();
    const result = await applyPatchArtifact(
      patchGroupDir,
      'LIA-99',
      'issue-id',
      ctx,
    );
    expect(result).toEqual({ prUrl: null, applied: false });
    expect(createComment).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.stringContaining('not `main`'),
      }),
    );
  });

  it('refuses when working tree is dirty', async () => {
    fs.writeFileSync(
      path.join(patchGroupDir, 'LIA-99.patch'),
      'diff --git ...',
    );
    execFileMock.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git' && args[0] === 'rev-parse') return { stdout: 'main\n' };
      if (cmd === 'git' && args[0] === 'status')
        return { stdout: 'M src/foo.ts\n' };
      return { stdout: '' };
    });

    const ctx = patchCtx();
    const result = await applyPatchArtifact(
      patchGroupDir,
      'LIA-99',
      'issue-id',
      ctx,
    );
    expect(result).toEqual({ prUrl: null, applied: false });
    expect(createComment).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.stringContaining('uncommitted changes'),
      }),
    );
  });

  it('succeeds via git am when patch is mbox format', async () => {
    const patchPath = path.join(patchGroupDir, 'LIA-99.patch');
    fs.writeFileSync(patchPath, 'From abc123\nSubject: fix\n\ndiff --git ...');

    execFileMock.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git' && args[0] === 'rev-parse') return { stdout: 'main\n' };
      if (cmd === 'git' && args[0] === 'status') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'am') return { stdout: '' };
      if (cmd === 'npm') return { stdout: '' };
      if (cmd === 'gh')
        return { stdout: 'https://github.com/test/repo/pull/10\n' };
      return { stdout: '' };
    });

    const result = await applyPatchArtifact(
      patchGroupDir,
      'LIA-99',
      'issue-id',
      patchCtx(),
    );
    expect(result).toEqual({
      prUrl: 'https://github.com/test/repo/pull/10',
      applied: true,
    });
    // git apply should NOT have been called (am succeeded)
    const applyCalls = execFileMock.mock.calls.filter(
      (c: unknown[]) => c[0] === 'git' && (c[1] as string[])[0] === 'apply',
    );
    expect(applyCalls).toHaveLength(0);
  });

  it('applies patch, builds, pushes, creates PR on success', async () => {
    const patchPath = path.join(patchGroupDir, 'LIA-99.patch');
    fs.writeFileSync(patchPath, 'diff --git a/foo b/foo');
    fs.writeFileSync(
      path.join(patchGroupDir, 'LIA-99-status.md'),
      '```bash\ngit commit -m "fix: resolve scheduled task exit"\n```',
    );

    execFileMock.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git' && args[0] === 'rev-parse') return { stdout: 'main\n' };
      if (cmd === 'git' && args[0] === 'status') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'pull') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'checkout') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'am')
        return { error: new Error('not mbox') };
      if (cmd === 'git' && args[0] === 'apply') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'add') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'commit') return { stdout: '' };
      if (cmd === 'npm' && args[0] === 'run') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'push') return { stdout: '' };
      if (cmd === 'gh' && args[0] === 'pr')
        return { stdout: 'https://github.com/test/repo/pull/42\n' };
      return { stdout: '' };
    });

    const result = await applyPatchArtifact(
      patchGroupDir,
      'LIA-99',
      'issue-id',
      patchCtx(),
    );
    expect(result).toEqual({
      prUrl: 'https://github.com/test/repo/pull/42',
      applied: true,
    });
    expect(fs.existsSync(patchPath)).toBe(false);
    expect(fs.existsSync(patchPath + '.applied')).toBe(true);
  });

  it('parses commit message from status file', async () => {
    fs.writeFileSync(path.join(patchGroupDir, 'LIA-99.patch'), 'diff');
    fs.writeFileSync(
      path.join(patchGroupDir, 'LIA-99-status.md'),
      'some text\ngit commit -m "my custom message"\nmore text',
    );

    const commitArgs: string[][] = [];
    execFileMock.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git' && args[0] === 'rev-parse') return { stdout: 'main\n' };
      if (cmd === 'git' && args[0] === 'status') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'am')
        return { error: new Error('not mbox') };
      if (cmd === 'git' && args[0] === 'commit') {
        commitArgs.push([...args]);
        return { stdout: '' };
      }
      if (cmd === 'gh')
        return { stdout: 'https://github.com/test/repo/pull/1\n' };
      return { stdout: '' };
    });

    await applyPatchArtifact(patchGroupDir, 'LIA-99', 'issue-id', patchCtx());
    expect(commitArgs[0]).toContain('my custom message');
  });

  it('posts comment and cleans up on build failure', async () => {
    fs.writeFileSync(path.join(patchGroupDir, 'LIA-99.patch'), 'diff');

    execFileMock.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git' && args[0] === 'rev-parse') return { stdout: 'main\n' };
      if (cmd === 'git' && args[0] === 'status') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'am')
        return { error: new Error('not mbox') };
      if (cmd === 'npm' && args[0] === 'run')
        return { error: new Error('tsc: error TS2345') };
      return { stdout: '' };
    });

    const ctx = patchCtx();
    const result = await applyPatchArtifact(
      patchGroupDir,
      'LIA-99',
      'issue-id',
      ctx,
    );
    expect(result).toEqual({ prUrl: null, applied: false });
    expect(createComment).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.stringContaining('build failed'),
      }),
    );
  });

  it('posts comment and cleans up on git apply failure', async () => {
    fs.writeFileSync(path.join(patchGroupDir, 'LIA-99.patch'), 'diff');

    execFileMock.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'git' && args[0] === 'rev-parse') return { stdout: 'main\n' };
      if (cmd === 'git' && args[0] === 'status') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'am')
        return { error: new Error('not mbox') };
      if (cmd === 'git' && args[0] === 'apply')
        return { error: new Error('patch does not apply') };
      return { stdout: '' };
    });

    const ctx = patchCtx();
    const result = await applyPatchArtifact(
      patchGroupDir,
      'LIA-99',
      'issue-id',
      ctx,
    );
    expect(result).toEqual({ prUrl: null, applied: false });
    expect(createComment).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.stringContaining('git apply'),
      }),
    );
  });

  it('leaves branch on gh pr create failure', async () => {
    fs.writeFileSync(path.join(patchGroupDir, 'LIA-99.patch'), 'diff');

    const gitCalls: Array<{ cmd: string; args: string[] }> = [];
    execFileMock.mockImplementation((cmd: string, args: string[]) => {
      gitCalls.push({ cmd, args: [...args] });
      if (cmd === 'git' && args[0] === 'rev-parse') return { stdout: 'main\n' };
      if (cmd === 'git' && args[0] === 'status') return { stdout: '' };
      if (cmd === 'git' && args[0] === 'am')
        return { error: new Error('not mbox') };
      if (cmd === 'gh') return { error: new Error('auth required') };
      return { stdout: '' };
    });

    const ctx = patchCtx();
    const result = await applyPatchArtifact(
      patchGroupDir,
      'LIA-99',
      'issue-id',
      ctx,
    );
    expect(result).toEqual({ prUrl: null, applied: false });
    expect(createComment).toHaveBeenCalledWith(
      expect.objectContaining({
        body: expect.stringContaining('create PR manually'),
      }),
    );
    // Should NOT delete branch (push succeeded)
    const branchDeletes = gitCalls.filter(
      (c) => c.cmd === 'git' && c.args[0] === 'branch' && c.args[1] === '-D',
    );
    expect(branchDeletes).toHaveLength(0);
  });
});
