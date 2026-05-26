import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { promisify } from 'util';
import { _initTestDatabase } from './db.js';
import {
  upsertIssuePr,
  getIssuePr,
  updatePrAutoMergeState,
  getPendingAutoMerges,
  logPipelineEvent,
} from './db.js';
import type { LinearContext } from './linear-dispatcher.js';

const execFileMock = vi.fn();

vi.mock('child_process', async (importOriginal) => {
  const orig = await importOriginal<typeof import('child_process')>();
  const mockFn = (...args: unknown[]) => {
    const cb = args[args.length - 1];
    if (typeof cb === 'function') {
      const result = execFileMock(args[0], args[1]);
      if (result?.error) cb(result.error);
      else cb(null, result?.stdout ?? '', '');
    }
  };
  // Preserve custom promisify behavior so promisify(execFile) resolves { stdout, stderr }
  (mockFn as unknown as Record<symbol, unknown>)[promisify.custom] = (
    ...args: unknown[]
  ) => {
    const result = execFileMock(args[0], args[1]);
    if (result?.error) return Promise.reject(result.error);
    return Promise.resolve({ stdout: result?.stdout ?? '', stderr: '' });
  };
  return { ...orig, execFile: mockFn };
});

beforeEach(() => {
  _initTestDatabase();
  execFileMock.mockReset();
});

describe('linear_issue_prs DB accessors', () => {
  it('upserts and retrieves a PR record', () => {
    upsertIssuePr('issue-1', 'https://github.com/o/r/pull/1', 'feat/x');
    const pr = getIssuePr('issue-1');
    expect(pr).toBeDefined();
    expect(pr!.pr_url).toBe('https://github.com/o/r/pull/1');
    expect(pr!.branch).toBe('feat/x');
    expect(pr!.auto_merge_state).toBe('none');
  });

  it('updates on conflict (upsert)', () => {
    upsertIssuePr('issue-1', 'https://github.com/o/r/pull/1', 'feat/x');
    upsertIssuePr('issue-1', 'https://github.com/o/r/pull/2');
    const pr = getIssuePr('issue-1');
    expect(pr!.pr_url).toBe('https://github.com/o/r/pull/2');
    expect(pr!.branch).toBe('feat/x');
  });

  it('returns undefined for non-existent issue', () => {
    expect(getIssuePr('nope')).toBeUndefined();
  });

  it('updates auto_merge_state', () => {
    upsertIssuePr('issue-1', 'https://github.com/o/r/pull/1');
    updatePrAutoMergeState('issue-1', 'pending');
    expect(getIssuePr('issue-1')!.auto_merge_state).toBe('pending');
    updatePrAutoMergeState('issue-1', 'merged');
    expect(getIssuePr('issue-1')!.auto_merge_state).toBe('merged');
  });

  it('getPendingAutoMerges returns only pending entries', () => {
    upsertIssuePr('a', 'https://github.com/o/r/pull/1');
    upsertIssuePr('b', 'https://github.com/o/r/pull/2');
    upsertIssuePr('c', 'https://github.com/o/r/pull/3');
    updatePrAutoMergeState('a', 'pending');
    updatePrAutoMergeState('c', 'pending');
    updatePrAutoMergeState('b', 'merged');

    const pending = getPendingAutoMerges();
    expect(pending).toHaveLength(2);
    expect(pending.map((p) => p.issue_id).sort()).toEqual(['a', 'c']);
  });
});

describe('queryPrChecks', () => {
  it('is importable', async () => {
    const mod = await import('./linear-auto-merge.js');
    expect(typeof mod.queryPrChecks).toBe('function');
  });

  it('returns pending when some checks fail but others are still running', async () => {
    execFileMock.mockReturnValue({
      stdout: JSON.stringify([
        { bucket: 'fail', name: 'ci' },
        { bucket: 'pending', name: 'CodeQL' },
        { bucket: 'pass', name: 'label' },
      ]),
    });

    const { queryPrChecks } = await import('./linear-auto-merge.js');
    const result = await queryPrChecks('https://github.com/test/repo/pull/1');
    expect(result.status).toBe('pending');
    expect(result.summary).toContain('CodeQL');
  });

  it('returns fail only when all checks are complete', async () => {
    execFileMock.mockReturnValue({
      stdout: JSON.stringify([
        { bucket: 'fail', name: 'ci' },
        { bucket: 'pass', name: 'label' },
      ]),
    });

    const { queryPrChecks } = await import('./linear-auto-merge.js');
    const result = await queryPrChecks('https://github.com/test/repo/pull/1');
    expect(result.status).toBe('fail');
    expect(result.summary).toContain('ci');
  });

  it('returns pass when all checks pass', async () => {
    execFileMock.mockReturnValue({
      stdout: JSON.stringify([
        { bucket: 'pass', name: 'ci' },
        { bucket: 'pass', name: 'CodeQL' },
      ]),
    });

    const { queryPrChecks } = await import('./linear-auto-merge.js');
    const result = await queryPrChecks('https://github.com/test/repo/pull/1');
    expect(result.status).toBe('pass');
  });
});

// ---------------------------------------------------------------------------
// sweepStaleAgentWorking
// ---------------------------------------------------------------------------

/** Returns a stub LinearContext with the states and client methods needed for sweep tests. */
function makeAgentWorkingCtx(
  overrides: Partial<LinearContext> = {},
): LinearContext {
  const updateIssue = vi.fn().mockResolvedValue({});
  const createComment = vi.fn().mockResolvedValue({});

  return {
    client: {
      updateIssue,
      createComment,
      issues: vi.fn().mockResolvedValue({ nodes: [] }),
      issue: vi.fn(),
    } as unknown as LinearContext['client'],
    stateByName: new Map([
      ['Ready for Agent', { id: 'ready-id', name: 'Ready for Agent' }],
      ['Agent Working', { id: 'working-id', name: 'Agent Working' }],
      ['In Review', { id: 'review-id', name: 'In Review' }],
      ['Done', { id: 'done-id', name: 'Done' }],
      ['Backlog', { id: 'backlog-id', name: 'Backlog' }],
      [
        'Manual Review Required',
        { id: 'manual-id', name: 'Manual Review Required' },
      ],
    ]),
    stateById: new Map(),
    botUserId: 'bot-id',
    viewerId: 'viewer-id',
    inFlightDispatch: new Set(),
    inFlightGate: new Set(),
    gateLabels: {
      effort: {},
      complexity: {},
      wardenSkip: 'label-warden-skip',
      revise: 'label-revise',
      evaluating: 'label-evaluating',
    },
    teamId: 'team-id',
    vaultPath: null,
    repoSlug: 'test/repo',
    deps: {
      registeredGroups: () => ({}),
      registerGroup: vi.fn(),
      registry: {} as LinearContext['deps']['registry'],
      queue: {} as LinearContext['deps']['queue'],
    },
    dispatchGroup: {
      name: 'Linear Dispatch',
      folder: 'linear-dispatch',
      trigger: '',
      added_at: new Date().toISOString(),
      requiresTrigger: false,
      isControlGroup: false,
    },
    ...overrides,
  } as unknown as LinearContext;
}

/** Builds a fake issue node that would be returned by ctx.client.issues() */
function makeIssueNode(
  id: string,
  identifier: string,
  labelNames: string[] = [],
  updatedAt?: string,
) {
  return {
    id,
    identifier,
    title: `Issue ${identifier}`,
    description: null,
    // updatedAt more than 2 hours ago by default
    updatedAt:
      updatedAt ?? new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
    labels: vi.fn().mockResolvedValue({
      nodes: labelNames.map((name) => ({ id: `label-${name}`, name })),
    }),
  };
}

describe('sweepStaleAgentWorking', () => {
  const ORIGINAL_AUTO_MERGE = process.env.LINEAR_AUTO_MERGE;

  beforeEach(() => {
    process.env.LINEAR_AUTO_MERGE = '1';
    _initTestDatabase();
    execFileMock.mockReset();
  });

  afterEach(() => {
    process.env.LINEAR_AUTO_MERGE = ORIGINAL_AUTO_MERGE;
  });

  it('moves issue to Done when the PR is already merged', async () => {
    const issueId = 'issue-merged-pr';
    const prUrl = 'https://github.com/test/repo/pull/42';

    logPipelineEvent(issueId, 'LIA-1', 'agent_started');
    upsertIssuePr(issueId, prUrl);

    // gh pr view returns MERGED
    execFileMock.mockReturnValue({
      stdout: JSON.stringify({ state: 'MERGED' }),
    });

    const issueNode = makeIssueNode(issueId, 'LIA-1');
    const ctx = makeAgentWorkingCtx({
      client: {
        updateIssue: vi.fn().mockResolvedValue({}),
        createComment: vi.fn().mockResolvedValue({}),
        issues: vi.fn().mockResolvedValue({ nodes: [issueNode] }),
        issue: vi.fn(),
      } as unknown as LinearContext['client'],
    });

    const { sweepStaleAgentWorking } = await import('./linear-auto-merge.js');
    // Pass thresholdMs=0 so recently-added DB events are still treated as stale
    await sweepStaleAgentWorking(ctx, 0);

    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      issueId,
      expect.objectContaining({ stateId: 'done-id' }),
    );
    expect(ctx.client.createComment).toHaveBeenCalledWith(
      expect.objectContaining({
        issueId,
        body: expect.stringContaining('already merged'),
      }),
    );
  });

  it('moves issue to In Review when PR exists but is not merged', async () => {
    const issueId = 'issue-open-pr';
    const prUrl = 'https://github.com/test/repo/pull/99';
    upsertIssuePr(issueId, prUrl);

    execFileMock.mockReturnValue({
      stdout: JSON.stringify({ state: 'OPEN' }),
    });

    const issueNode = makeIssueNode(issueId, 'LIA-2');
    const ctx = makeAgentWorkingCtx({
      client: {
        updateIssue: vi.fn().mockResolvedValue({}),
        createComment: vi.fn().mockResolvedValue({}),
        issues: vi.fn().mockResolvedValue({ nodes: [issueNode] }),
        issue: vi.fn(),
      } as unknown as LinearContext['client'],
    });

    const { sweepStaleAgentWorking } = await import('./linear-auto-merge.js');
    await sweepStaleAgentWorking(ctx, 0);

    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      issueId,
      expect.objectContaining({ stateId: 'review-id' }),
    );
  });

  it('moves issue to In Review when agent_completed event exists but no PR', async () => {
    const issueId = 'issue-completed-no-pr';
    logPipelineEvent(issueId, 'LIA-3', 'agent_started');
    logPipelineEvent(issueId, 'LIA-3', 'agent_completed');

    const issueNode = makeIssueNode(issueId, 'LIA-3');
    const ctx = makeAgentWorkingCtx({
      client: {
        updateIssue: vi.fn().mockResolvedValue({}),
        createComment: vi.fn().mockResolvedValue({}),
        issues: vi.fn().mockResolvedValue({ nodes: [issueNode] }),
        issue: vi.fn(),
      } as unknown as LinearContext['client'],
    });

    const { sweepStaleAgentWorking } = await import('./linear-auto-merge.js');
    await sweepStaleAgentWorking(ctx, 0);

    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      issueId,
      expect.objectContaining({ stateId: 'review-id' }),
    );
  });

  it('moves issue to Manual Review Required when stale with no progress signal', async () => {
    const issueId = 'issue-no-signal';
    // No pipeline events, no PR — genuinely stuck with no info

    const issueNode = makeIssueNode(issueId, 'LIA-4');
    const ctx = makeAgentWorkingCtx({
      client: {
        updateIssue: vi.fn().mockResolvedValue({}),
        createComment: vi.fn().mockResolvedValue({}),
        issues: vi.fn().mockResolvedValue({ nodes: [issueNode] }),
        issue: vi.fn(),
      } as unknown as LinearContext['client'],
    });

    const { sweepStaleAgentWorking } = await import('./linear-auto-merge.js');
    await sweepStaleAgentWorking(ctx, 0);

    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      issueId,
      expect.objectContaining({ stateId: 'manual-id' }),
    );
  });

  it('does not transition an issue that is currently in-flight', async () => {
    const issueId = 'issue-inflight';

    const issueNode = makeIssueNode(issueId, 'LIA-5');
    const updateIssue = vi.fn().mockResolvedValue({});
    const ctx = makeAgentWorkingCtx({
      client: {
        updateIssue,
        createComment: vi.fn().mockResolvedValue({}),
        issues: vi.fn().mockResolvedValue({ nodes: [issueNode] }),
        issue: vi.fn(),
      } as unknown as LinearContext['client'],
      inFlightDispatch: new Set([issueId]),
    });

    const { sweepStaleAgentWorking } = await import('./linear-auto-merge.js');
    // Even with threshold=0, in-flight issues must be skipped
    await sweepStaleAgentWorking(ctx, 0);

    expect(updateIssue).not.toHaveBeenCalled();
  });

  it('does not transition an issue that entered Agent Working recently (under threshold)', async () => {
    const issueId = 'issue-fresh';
    // Seed an agent_started event with current timestamp (well under 60 min threshold)
    logPipelineEvent(issueId, 'LIA-6', 'agent_started');

    const recentUpdatedAt = new Date(Date.now() - 5 * 60 * 1000).toISOString(); // 5 min ago
    const issueNode = makeIssueNode(issueId, 'LIA-6', [], recentUpdatedAt);
    const updateIssue = vi.fn().mockResolvedValue({});
    const ctx = makeAgentWorkingCtx({
      client: {
        updateIssue,
        createComment: vi.fn().mockResolvedValue({}),
        issues: vi.fn().mockResolvedValue({ nodes: [issueNode] }),
        issue: vi.fn(),
      } as unknown as LinearContext['client'],
    });

    const { sweepStaleAgentWorking } = await import('./linear-auto-merge.js');
    // Use the default 60-minute threshold — the 5-minute-old event should not be swept
    await sweepStaleAgentWorking(ctx);

    expect(updateIssue).not.toHaveBeenCalled();
  });
});
