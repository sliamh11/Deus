import { beforeEach, describe, expect, it, vi } from 'vitest';
import { promisify } from 'util';
import { _initTestDatabase } from './db.js';
import {
  upsertIssuePr,
  getIssuePr,
  updatePrAutoMergeState,
  getPendingAutoMerges,
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
