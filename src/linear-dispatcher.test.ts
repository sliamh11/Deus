import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs';
import path from 'path';
import os from 'os';

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

const TEST_PROJECT_ROOT = path.join(os.tmpdir(), `deus-test-${process.pid}`);

import {
  loadRoleSpecs,
  buildIssuePrompt,
  startLinearDispatcher,
  stopLinearDispatcher,
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
