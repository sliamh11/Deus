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

describe('attemptAutoMerge — auto flag passed to gh pr merge', () => {
  beforeEach(() => {
    process.env.LINEAR_AUTO_MERGE = '1';
  });
  afterEach(() => {
    delete process.env.LINEAR_AUTO_MERGE;
  });

  it('passes --auto flag when queuing GitHub auto-merge', async () => {
    // First call: gh pr checks (preCheck inside mergePr with {auto:true}) → pending
    // Second call: gh pr merge --auto → success (autoQueued)
    execFileMock
      .mockReturnValueOnce({
        stdout: JSON.stringify([{ bucket: 'pending', name: 'ci' }]),
      })
      .mockReturnValueOnce({ stdout: '' }); // merge --auto succeeds

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr(
      'am-issue-1',
      'https://github.com/test-owner/test-repo/pull/99',
    );
    updatePrAutoMergeState('am-issue-1', 'pending');

    await attemptAutoMerge(
      ctx,
      'am-issue-1',
      'https://github.com/test-owner/test-repo/pull/99',
      'LIA-99',
    );

    // Verify --auto was in the merge call args
    const mergeCall = execFileMock.mock.calls.find(
      (c: unknown[]) =>
        Array.isArray(c[1]) && (c[1] as string[]).includes('merge'),
    );
    expect(mergeCall).toBeDefined();
    expect(mergeCall![1]).toContain('--auto');
  });

  it('returns immediately when GitHub auto-merge is successfully queued', async () => {
    // preCheck → pending; merge --auto → success
    execFileMock
      .mockReturnValueOnce({
        stdout: JSON.stringify([{ bucket: 'pending', name: 'ci' }]),
      })
      .mockReturnValueOnce({ stdout: '' });

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr(
      'am-issue-2',
      'https://github.com/test-owner/test-repo/pull/100',
    );
    updatePrAutoMergeState('am-issue-2', 'pending');

    await attemptAutoMerge(
      ctx,
      'am-issue-2',
      'https://github.com/test-owner/test-repo/pull/100',
      'LIA-100',
    );

    // No issue state update yet — GitHub handles the merge asynchronously
    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
    expect(ctx.client.createComment).not.toHaveBeenCalled();
  });

  it('falls back to direct merge when --auto fails, and merges if CI passes', async () => {
    vi.useFakeTimers();

    // Call sequence:
    //  1. gh pr checks (preCheck for --auto) → pending
    //  2. gh pr merge --auto → error (branch protection not set)
    //  3. gh pr checks (fallback poll) → pass
    //  4. gh pr checks (preCheck for direct merge) → pass
    //  5. gh pr merge (direct) → success
    const autoError = new Error(
      'auto-merge not allowed: branch protection not configured',
    );
    execFileMock
      .mockReturnValueOnce({
        stdout: JSON.stringify([{ bucket: 'pending', name: 'ci' }]),
      })
      .mockReturnValueOnce({ error: autoError })
      .mockReturnValueOnce({
        stdout: JSON.stringify([{ bucket: 'pass', name: 'ci' }]),
      })
      .mockReturnValueOnce({
        stdout: JSON.stringify([{ bucket: 'pass', name: 'ci' }]),
      })
      .mockReturnValueOnce({ stdout: '' });

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr(
      'am-issue-3',
      'https://github.com/test-owner/test-repo/pull/101',
    );
    updatePrAutoMergeState('am-issue-3', 'pending');

    const promise = attemptAutoMerge(
      ctx,
      'am-issue-3',
      'https://github.com/test-owner/test-repo/pull/101',
      'LIA-101',
    );
    // Advance past the CI_POLL_INTERVAL_MS wait
    await vi.runAllTimersAsync();
    await promise;

    // Direct merge succeeded → issue moved to Done
    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'am-issue-3',
      expect.objectContaining({ stateId: 'done-id' }),
    );
    expect(ctx.client.createComment).toHaveBeenCalledWith(
      expect.objectContaining({ issueId: 'am-issue-3' }),
    );

    vi.useRealTimers();
  });

  it('allows pending CI state for --auto preCheck (does not block on pending)', async () => {
    // preCheck → pending; merge --auto → success (autoQueued)
    execFileMock
      .mockReturnValueOnce({
        stdout: JSON.stringify([{ bucket: 'pending', name: 'ci' }]),
      })
      .mockReturnValueOnce({ stdout: '' });

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr(
      'am-issue-4',
      'https://github.com/test-owner/test-repo/pull/102',
    );
    updatePrAutoMergeState('am-issue-4', 'pending');

    // Should NOT throw or fail — pending CI is allowed for --auto
    await expect(
      attemptAutoMerge(
        ctx,
        'am-issue-4',
        'https://github.com/test-owner/test-repo/pull/102',
        'LIA-102',
      ),
    ).resolves.toBeUndefined();

    // No error comment, no re-queue
    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
  });

  it('blocks --auto when CI is already failing', async () => {
    vi.useFakeTimers();

    // preCheck for --auto → fail (blocks auto-merge attempt)
    // fallback poll → fail (CI still failing)
    execFileMock
      .mockReturnValueOnce({
        stdout: JSON.stringify([{ bucket: 'fail', name: 'ci' }]),
      })
      // mergePr({auto:true}) returns early, no merge call
      // fallback: preCheck returns fail immediately in mergePr({auto:false})
      // but we hit the "auto failed" path first — need to wait for the timer
      // then the fallback queryPrChecks call:
      .mockReturnValueOnce({
        stdout: JSON.stringify([{ bucket: 'fail', name: 'ci' }]),
      });

    const { attemptAutoMerge } = await import('./linear-auto-merge.js');
    const ctx = makeAutoMergeCtx();
    upsertIssuePr(
      'am-issue-5',
      'https://github.com/test-owner/test-repo/pull/103',
    );
    updatePrAutoMergeState('am-issue-5', 'pending');

    const promise = attemptAutoMerge(
      ctx,
      'am-issue-5',
      'https://github.com/test-owner/test-repo/pull/103',
      'LIA-103',
    );
    await vi.runAllTimersAsync();
    await promise;

    // CI fail triggers re-queue to Ready for Agent
    expect(ctx.client.updateIssue).toHaveBeenCalledWith(
      'am-issue-5',
      expect.objectContaining({ stateId: 'ready-id' }),
    );

    vi.useRealTimers();
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
