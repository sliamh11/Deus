import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { promisify } from 'util';
import { _initTestDatabase } from './db.js';
import {
  upsertIssuePr,
  getIssuePr,
  updatePrAutoMergeState,
  getPendingAutoMerges,
  getOpenPrsForActiveIssues,
  upsertIssueCache,
} from './db.js';

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

describe('getOpenPrsForActiveIssues', () => {
  it('returns PRs for issues in Agent Working or In Review', () => {
    // Seed issue cache with two active issues and one Done issue
    upsertIssueCache({
      issue_id: 'issue-working',
      identifier: 'LIA-1',
      title: 'Working issue',
      state_name: 'Agent Working',
      team_id: 'team-1',
      priority: 2,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    upsertIssueCache({
      issue_id: 'issue-review',
      identifier: 'LIA-2',
      title: 'Review issue',
      state_name: 'In Review',
      team_id: 'team-1',
      priority: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    upsertIssueCache({
      issue_id: 'issue-done',
      identifier: 'LIA-3',
      title: 'Done issue',
      state_name: 'Done',
      team_id: 'team-1',
      priority: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });

    upsertIssuePr(
      'issue-working',
      'https://github.com/o/r/pull/10',
      'feat/a',
      'LIA-1',
    );
    upsertIssuePr(
      'issue-review',
      'https://github.com/o/r/pull/11',
      'feat/b',
      'LIA-2',
    );
    upsertIssuePr(
      'issue-done',
      'https://github.com/o/r/pull/12',
      'feat/c',
      'LIA-3',
    );
    // Mark done PR as merged
    updatePrAutoMergeState('issue-done', 'merged');

    const active = getOpenPrsForActiveIssues();
    expect(active).toHaveLength(2);
    const ids = active.map((p) => p.issue_id).sort();
    expect(ids).toEqual(['issue-review', 'issue-working']);
  });

  it('excludes merged PRs even if issue is in active state', () => {
    upsertIssueCache({
      issue_id: 'issue-merged',
      identifier: 'LIA-4',
      title: 'Merged issue',
      state_name: 'Agent Working',
      team_id: 'team-1',
      priority: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    upsertIssuePr(
      'issue-merged',
      'https://github.com/o/r/pull/20',
      'feat/m',
      'LIA-4',
    );
    updatePrAutoMergeState('issue-merged', 'merged');

    const active = getOpenPrsForActiveIssues();
    expect(active).toHaveLength(0);
  });
});

describe('buildConflictRebasePrompt', () => {
  it('includes PR URL, changed files, and base branch', async () => {
    const { buildConflictRebasePrompt } =
      await import('./linear-auto-merge.js');
    const prompt = buildConflictRebasePrompt(
      'https://github.com/o/r/pull/42',
      5,
      'main',
    );
    expect(prompt).toContain('https://github.com/o/r/pull/42');
    expect(prompt).toContain('5');
    expect(prompt).toContain('main');
    expect(prompt).toContain('rebase');
  });
});

describe('checkConflictingPrs', () => {
  function makeCtx(
    overrides: {
      updateIssue?: ReturnType<typeof vi.fn>;
      createComment?: ReturnType<typeof vi.fn>;
      issue?: ReturnType<typeof vi.fn>;
    } = {},
  ) {
    const updateIssue = overrides.updateIssue ?? vi.fn().mockResolvedValue({});
    const createComment =
      overrides.createComment ?? vi.fn().mockResolvedValue({});
    const issue =
      overrides.issue ??
      vi.fn().mockResolvedValue({
        labels: vi.fn().mockResolvedValue({ nodes: [] }),
        state: Promise.resolve({ name: 'In Review' }),
      });

    return {
      client: { updateIssue, createComment, issue },
      stateByName: new Map([
        ['Ready for Agent', { id: 'ready-id', name: 'Ready for Agent' }],
        [
          'Manual Review Required',
          { id: 'manual-id', name: 'Manual Review Required' },
        ],
        ['In Review', { id: 'review-id', name: 'In Review' }],
        ['Agent Working', { id: 'working-id', name: 'Agent Working' }],
      ]),
      gateLabels: { conflict: 'conflict-label-id', effort: {}, complexity: {} },
    } as unknown as import('./linear-dispatcher.js').LinearContext;
  }

  it('skips PRs with UNKNOWN mergeability', async () => {
    upsertIssueCache({
      issue_id: 'issue-u',
      identifier: 'LIA-10',
      title: 'Unknown issue',
      state_name: 'In Review',
      team_id: 'team-1',
      priority: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    upsertIssuePr(
      'issue-u',
      'https://github.com/o/r/pull/100',
      'feat/u',
      'LIA-10',
    );

    execFileMock.mockReturnValue({
      stdout: JSON.stringify({
        mergeable: 'UNKNOWN',
        mergeStateStatus: 'UNKNOWN',
        changedFiles: 3,
        headRefName: 'feat/u',
        baseRefName: 'main',
      }),
    });

    const ctx = makeCtx();
    const { checkConflictingPrs } = await import('./linear-auto-merge.js');
    await checkConflictingPrs(ctx);

    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
    expect(ctx.client.createComment).not.toHaveBeenCalled();
  });

  it('routes small conflicting PR to Ready for Agent', async () => {
    upsertIssueCache({
      issue_id: 'issue-small',
      identifier: 'LIA-11',
      title: 'Small conflict',
      state_name: 'In Review',
      team_id: 'team-1',
      priority: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    upsertIssuePr(
      'issue-small',
      'https://github.com/o/r/pull/101',
      'feat/s',
      'LIA-11',
    );

    execFileMock.mockReturnValue({
      stdout: JSON.stringify({
        mergeable: 'CONFLICTING',
        mergeStateStatus: 'DIRTY',
        changedFiles: 3,
        headRefName: 'feat/s',
        baseRefName: 'main',
      }),
    });

    const ctx = makeCtx();
    const { checkConflictingPrs } = await import('./linear-auto-merge.js');
    await checkConflictingPrs(ctx);

    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'issue-small',
      expect.objectContaining({ stateId: 'ready-id' }),
    );
    expect(ctx.client.createComment).toHaveBeenCalledWith(
      expect.objectContaining({ issueId: 'issue-small' }),
    );
  });

  it('routes large conflicting PR to Manual Review Required', async () => {
    upsertIssueCache({
      issue_id: 'issue-large',
      identifier: 'LIA-12',
      title: 'Large conflict',
      state_name: 'In Review',
      team_id: 'team-1',
      priority: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    upsertIssuePr(
      'issue-large',
      'https://github.com/o/r/pull/102',
      'feat/l',
      'LIA-12',
    );

    execFileMock.mockReturnValue({
      stdout: JSON.stringify({
        mergeable: 'CONFLICTING',
        mergeStateStatus: 'DIRTY',
        changedFiles: 15,
        headRefName: 'feat/l',
        baseRefName: 'main',
      }),
    });

    const ctx = makeCtx();
    const { checkConflictingPrs } = await import('./linear-auto-merge.js');
    await checkConflictingPrs(ctx);

    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'issue-large',
      expect.objectContaining({ stateId: 'manual-id' }),
    );
  });

  it('skips issues already labeled conflict', async () => {
    upsertIssueCache({
      issue_id: 'issue-already',
      identifier: 'LIA-13',
      title: 'Already labeled',
      state_name: 'In Review',
      team_id: 'team-1',
      priority: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    upsertIssuePr(
      'issue-already',
      'https://github.com/o/r/pull/103',
      'feat/x',
      'LIA-13',
    );

    execFileMock.mockReturnValue({
      stdout: JSON.stringify({
        mergeable: 'CONFLICTING',
        mergeStateStatus: 'DIRTY',
        changedFiles: 3,
        headRefName: 'feat/x',
        baseRefName: 'main',
      }),
    });

    // Issue already has conflict label
    const issueWithLabel = {
      labels: vi.fn().mockResolvedValue({
        nodes: [{ id: 'conflict-label-id', name: 'conflict' }],
      }),
      state: Promise.resolve({ name: 'In Review' }),
    };
    const ctx = makeCtx({ issue: vi.fn().mockResolvedValue(issueWithLabel) });
    const { checkConflictingPrs } = await import('./linear-auto-merge.js');
    await checkConflictingPrs(ctx);

    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
  });

  it('skips MERGEABLE PRs', async () => {
    upsertIssueCache({
      issue_id: 'issue-mergeable',
      identifier: 'LIA-14',
      title: 'Mergeable issue',
      state_name: 'In Review',
      team_id: 'team-1',
      priority: 0,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    });
    upsertIssuePr(
      'issue-mergeable',
      'https://github.com/o/r/pull/104',
      'feat/m',
      'LIA-14',
    );

    execFileMock.mockReturnValue({
      stdout: JSON.stringify({
        mergeable: 'MERGEABLE',
        mergeStateStatus: 'CLEAN',
        changedFiles: 2,
        headRefName: 'feat/m',
        baseRefName: 'main',
      }),
    });

    const ctx = makeCtx();
    const { checkConflictingPrs } = await import('./linear-auto-merge.js');
    await checkConflictingPrs(ctx);

    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// attemptAutoMerge — GitHub auto-merge flow
// ---------------------------------------------------------------------------

const TEST_REPO = 'test-owner/test-repo';

function makeAutoMergeCtx() {
  return {
    client: {
      updateIssue: vi.fn().mockResolvedValue({}),
      createComment: vi.fn().mockResolvedValue({}),
      issue: vi.fn().mockResolvedValue({
        labels: vi.fn().mockResolvedValue({ nodes: [] }),
        comments: vi.fn().mockResolvedValue({ nodes: [] }),
      }),
    },
    stateByName: new Map([
      ['Done', { id: 'done-id', name: 'Done' }],
      ['Ready for Agent', { id: 'ready-id', name: 'Ready for Agent' }],
      ['In Review', { id: 'review-id', name: 'In Review' }],
    ]),
    gateLabels: {
      wardenSkip: 'warden-skip-id',
      revise: 'revise-id',
      evaluating: 'eval-id',
    },
    repoSlug: TEST_REPO,
  } as unknown as import('./linear-dispatcher.js').LinearContext;
}

describe('attemptAutoMerge — --admin poll-to-green (LIA-215)', () => {
  const PR = 'https://github.com/test-owner/test-repo/pull/200';
  const checks = (bucket: 'pass' | 'pending' | 'fail') => ({
    stdout: JSON.stringify([{ bucket, name: 'ci' }]),
  });

  beforeEach(() => {
    process.env.LINEAR_AUTO_MERGE = '1';
  });
  afterEach(() => {
    delete process.env.LINEAR_AUTO_MERGE;
    delete process.env.AUTO_MERGE_POLL_MAX_ATTEMPTS;
    vi.useRealTimers();
  });

  function mergeCallArgs(): string[] | undefined {
    const call = execFileMock.mock.calls.find(
      (c: unknown[]) =>
        Array.isArray(c[1]) && (c[1] as string[]).includes('merge'),
    );
    return call ? (call[1] as string[]) : undefined;
  }

  it('CI already green → --admin merge (never --auto) → Done', async () => {
    // initial checks → pass; mergePr precheck → pass; gh pr merge → ok
    execFileMock
      .mockReturnValueOnce(checks('pass'))
      .mockReturnValueOnce(checks('pass'))
      .mockReturnValueOnce({ stdout: '' });

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('am-pass', PR);
    updatePrAutoMergeState('am-pass', 'pending');

    await attemptAutoMerge(ctx, 'am-pass', PR, 'LIA-200');

    const args = mergeCallArgs();
    expect(args).toBeDefined();
    expect(args).toContain('--admin');
    expect(args).not.toContain('--auto');
    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'am-pass',
      expect.objectContaining({ stateId: 'done-id' }),
    );
  });

  it('CI failing → requeue to Ready for Agent, no merge call', async () => {
    execFileMock.mockReturnValueOnce(checks('fail'));

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('am-fail', PR);
    updatePrAutoMergeState('am-fail', 'pending');

    await attemptAutoMerge(ctx, 'am-fail', PR, 'LIA-201');

    expect(mergeCallArgs()).toBeUndefined();
    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'am-fail',
      expect.objectContaining({ stateId: 'ready-id' }),
    );
  });

  it('CI pending → schedules background poll, no synchronous merge/requeue', async () => {
    vi.useFakeTimers();
    execFileMock.mockReturnValueOnce(checks('pending'));

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('am-pend', PR);
    updatePrAutoMergeState('am-pend', 'pending');

    await attemptAutoMerge(ctx, 'am-pend', PR, 'LIA-202');

    // Nothing decided synchronously — the detached poll's timer is not advanced.
    expect(mergeCallArgs()).toBeUndefined();
    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
    // Still pending (not failed, not requeued).
    expect(getPendingAutoMerges().some((p) => p.issue_id === 'am-pend')).toBe(
      true,
    );
  });

  it('CI pending then green → MERGED via the background poll (the LIA-215 regression)', async () => {
    vi.useFakeTimers();
    // initial → pending; poll#1 → pending; poll#2 → pass; mergePr precheck → pass; merge → ok
    execFileMock
      .mockReturnValueOnce(checks('pending'))
      .mockReturnValueOnce(checks('pending'))
      .mockReturnValueOnce(checks('pass'))
      .mockReturnValueOnce(checks('pass'))
      .mockReturnValueOnce({ stdout: '' });

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('am-slow', PR);
    updatePrAutoMergeState('am-slow', 'pending');

    await attemptAutoMerge(ctx, 'am-slow', PR, 'LIA-203');
    await vi.runAllTimersAsync(); // drive the detached poll to completion

    const args = mergeCallArgs();
    expect(args).toContain('--admin');
    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'am-slow',
      expect.objectContaining({ stateId: 'done-id' }),
    );
  });

  it('CI pending then failing → requeue via the poll', async () => {
    vi.useFakeTimers();
    execFileMock
      .mockReturnValueOnce(checks('pending'))
      .mockReturnValueOnce(checks('fail'));

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('am-pf', PR);
    updatePrAutoMergeState('am-pf', 'pending');

    await attemptAutoMerge(ctx, 'am-pf', PR, 'LIA-204');
    await vi.runAllTimersAsync();

    expect(mergeCallArgs()).toBeUndefined();
    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'am-pf',
      expect.objectContaining({ stateId: 'ready-id' }),
    );
  });

  it('CI pending past the cap → PARKED: stays pending, no requeue, no breaker', async () => {
    vi.useFakeTimers();
    process.env.AUTO_MERGE_POLL_MAX_ATTEMPTS = '2';
    // initial → pending; poll#1 → pending; poll#2 → pending; cap reached → park.
    execFileMock
      .mockReturnValueOnce(checks('pending'))
      .mockReturnValueOnce(checks('pending'))
      .mockReturnValueOnce(checks('pending'));

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('am-cap', PR);
    updatePrAutoMergeState('am-cap', 'pending');

    await attemptAutoMerge(ctx, 'am-cap', PR, 'LIA-205');
    await vi.runAllTimersAsync();

    // Parked-but-visible: no merge, no requeue/state change, but a comment is
    // posted and the issue stays 'pending' for the sweep.
    expect(mergeCallArgs()).toBeUndefined();
    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
    expect(ctx.client.createComment).toHaveBeenCalledWith(
      expect.objectContaining({ issueId: 'am-cap' }),
    );
    expect(getPendingAutoMerges().some((p) => p.issue_id === 'am-cap')).toBe(
      true,
    );
  });
});

// ---------------------------------------------------------------------------
// sweepPendingAutoMerges — MERGED detection
// ---------------------------------------------------------------------------

describe('sweepPendingAutoMerges — detects PR already merged by GitHub', () => {
  beforeEach(() => {
    process.env.LINEAR_AUTO_MERGE = '1';
  });
  afterEach(() => {
    delete process.env.LINEAR_AUTO_MERGE;
  });

  it('syncs to Done when GitHub auto-merge already completed the PR', async () => {
    // Seed a pending auto-merge record
    upsertIssuePr(
      'sweep-issue-1',
      'https://github.com/test-owner/test-repo/pull/200',
      'feat/sweep',
      'LIA-200',
    );
    updatePrAutoMergeState('sweep-issue-1', 'pending');

    // gh pr view → MERGED
    execFileMock.mockReturnValueOnce({
      stdout: JSON.stringify({ state: 'MERGED' }),
    });

    const ctx = {
      client: {
        updateIssue: vi.fn().mockResolvedValue({}),
        createComment: vi.fn().mockResolvedValue({}),
        issue: vi.fn().mockResolvedValue({
          labels: vi.fn().mockResolvedValue({ nodes: [] }),
          comments: vi.fn().mockResolvedValue({ nodes: [] }),
        }),
      },
      stateByName: new Map([
        ['Done', { id: 'done-id', name: 'Done' }],
        ['Ready for Agent', { id: 'ready-id', name: 'Ready for Agent' }],
      ]),
      gateLabels: {
        wardenSkip: 'warden-skip-id',
        revise: 'revise-id',
        evaluating: 'eval-id',
      },
      repoSlug: TEST_REPO,
    } as unknown as import('./linear-dispatcher.js').LinearContext;

    const { sweepPendingAutoMerges } = await import('./linear-auto-merge.js');
    await sweepPendingAutoMerges(ctx);

    // Wait for the async fire-and-forget chain to settle
    await new Promise((r) => setTimeout(r, 50));

    // Should have moved issue to Done
    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'sweep-issue-1',
      expect.objectContaining({ stateId: 'done-id' }),
    );
    expect(ctx.client.createComment).toHaveBeenCalledWith(
      expect.objectContaining({ issueId: 'sweep-issue-1' }),
    );

    // DB state should be 'merged'
    const { getIssuePr: getPr } = await import('./db.js');
    expect(getPr('sweep-issue-1')?.auto_merge_state).toBe('merged');
  });
});

describe('mergeIfGreen / markDoneIfMerged — GitHub-webhook merge-only entries (LIA-315 Phase 4)', () => {
  const PR = 'https://github.com/test-owner/test-repo/pull/300';
  const checks = (bucket: 'pass' | 'pending' | 'fail') => ({
    stdout: JSON.stringify([{ bucket, name: 'ci' }]),
  });
  const mergeCall = () =>
    execFileMock.mock.calls.find(
      (c: unknown[]) =>
        Array.isArray(c[1]) && (c[1] as string[]).includes('merge'),
    );

  beforeEach(() => {
    process.env.LINEAR_AUTO_MERGE = '1';
  });
  afterEach(() => {
    delete process.env.LINEAR_AUTO_MERGE;
  });

  it('mergeIfGreen: CI green → --admin merge → Done', async () => {
    execFileMock
      .mockReturnValueOnce(checks('pass')) // mergeIfGreen re-query
      .mockReturnValueOnce(checks('pass')) // mergePr precheck
      .mockReturnValueOnce({ stdout: '' }); // gh pr merge
    const { mergeIfGreen } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('mg-pass', PR);
    await mergeIfGreen(ctx, 'mg-pass', PR, 'LIA-300');
    expect(mergeCall()).toBeDefined();
    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'mg-pass',
      expect.objectContaining({ stateId: 'done-id' }),
    );
  });

  // SECURITY: the public webhook must NEVER spawn an agent. On a stale/forged CI-green
  // event for a now-red PR, mergeIfGreen must be a pure no-op — NO merge, and crucially NO
  // requeue to "Ready for Agent" (which the dispatcher would turn into an agent run).
  it('mergeIfGreen: CI fail → NO merge AND NO requeue to Ready for Agent', async () => {
    execFileMock.mockReturnValueOnce(checks('fail'));
    const { mergeIfGreen } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('mg-fail', PR);
    await mergeIfGreen(ctx, 'mg-fail', PR, 'LIA-301');
    expect(mergeCall()).toBeUndefined();
    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
  });

  it('mergeIfGreen: CI pending → pure no-op (no merge, no requeue)', async () => {
    execFileMock.mockReturnValueOnce(checks('pending'));
    const { mergeIfGreen } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('mg-pend', PR);
    await mergeIfGreen(ctx, 'mg-pend', PR, 'LIA-302');
    expect(mergeCall()).toBeUndefined();
    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
  });

  it('markDoneIfMerged: PR MERGED → moves issue to Done', async () => {
    execFileMock.mockReturnValueOnce({
      stdout: JSON.stringify({ state: 'MERGED' }),
    });
    const { markDoneIfMerged } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('md-merged', PR);
    await markDoneIfMerged(ctx, 'md-merged', PR, 'LIA-303');
    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'md-merged',
      expect.objectContaining({ stateId: 'done-id' }),
    );
  });

  it('markDoneIfMerged: PR still OPEN → no-op (no Done transition)', async () => {
    execFileMock.mockReturnValueOnce({
      stdout: JSON.stringify({ state: 'OPEN' }),
    });
    const { markDoneIfMerged } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr('md-open', PR);
    await markDoneIfMerged(ctx, 'md-open', PR, 'LIA-304');
    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
  });
});
