import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

vi.mock('./linear-dispatcher.js', () => ({
  discoverWorkflowStates: vi.fn(),
}));

vi.mock('./db.js', () => ({
  logPipelineEvent: vi.fn(),
}));

vi.mock('./platform.js', () => ({
  openBrowser: vi.fn(),
}));

import { discoverWorkflowStates } from './linear-dispatcher.js';
import { logPipelineEvent } from './db.js';
import { openBrowser } from './platform.js';
import {
  initActionContext,
  handleOpenInBrowser,
  toggleWardenSkip,
  triggerGateRerun,
  moveIssueState,
  type ActionContext,
} from './linear-actions.js';

const mockDiscoverStates = vi.mocked(discoverWorkflowStates);
const mockLogEvent = vi.mocked(logPipelineEvent);
const mockOpenBrowser = vi.mocked(openBrowser);

function makeCtx(overrides?: Partial<ActionContext>): ActionContext {
  const stateByName = new Map([
    ['Backlog', { id: 's-backlog', name: 'Backlog' }],
    ['Todo', { id: 's-todo', name: 'Todo' }],
    ['Ready for Agent', { id: 's-ready', name: 'Ready for Agent' }],
    ['Agent Working', { id: 's-working', name: 'Agent Working' }],
    ['In Review', { id: 's-review', name: 'In Review' }],
    ['Done', { id: 's-done', name: 'Done' }],
  ]);

  return {
    client: {
      updateIssue: vi.fn(async () => ({ success: true })),
      issueLabels: vi.fn(async () => ({ nodes: [] })),
    } as unknown as ActionContext['client'],
    teamId: 'team-1',
    stateByName,
    wardenSkipLabelId: 'label-skip',
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('initActionContext', () => {
  it('returns null on error', async () => {
    mockDiscoverStates.mockRejectedValueOnce(new Error('no states'));
    const client = {} as ActionContext['client'];
    const result = await initActionContext(client, 'team-1');
    expect(result).toBeNull();
  });

  it('returns context with warden:skip label ID', async () => {
    mockDiscoverStates.mockResolvedValueOnce(
      new Map([['Done', { id: 's-done', name: 'Done' }]]),
    );
    const client = {
      issueLabels: vi.fn(async () => ({
        nodes: [
          { id: 'lbl-1', name: 'Priority' },
          { id: 'lbl-skip', name: 'warden:skip' },
        ],
      })),
    } as unknown as ActionContext['client'];
    const result = await initActionContext(client, 'team-1');
    expect(result).not.toBeNull();
    expect(result!.wardenSkipLabelId).toBe('lbl-skip');
  });

  it('returns null wardenSkipLabelId when label not found', async () => {
    mockDiscoverStates.mockResolvedValueOnce(new Map());
    const client = {
      issueLabels: vi.fn(async () => ({ nodes: [] })),
    } as unknown as ActionContext['client'];
    const result = await initActionContext(client, 'team-1');
    expect(result!.wardenSkipLabelId).toBeNull();
  });
});

describe('handleOpenInBrowser', () => {
  it('returns opening message when browser opens', () => {
    mockOpenBrowser.mockReturnValueOnce(true);
    const result = handleOpenInBrowser(
      'https://linear.app/issue/LIA-42',
      'LIA-42',
    );
    expect(result.ok).toBe(true);
    expect(result.message).toContain('LIA-42');
  });

  it('returns URL when browser cannot open (SSH)', () => {
    mockOpenBrowser.mockReturnValueOnce(false);
    const result = handleOpenInBrowser(
      'https://linear.app/issue/LIA-42',
      'LIA-42',
    );
    expect(result.ok).toBe(true);
    expect(result.message).toContain('https://linear.app/issue/LIA-42');
  });
});

describe('toggleWardenSkip', () => {
  it('returns error when label not in workspace', async () => {
    const ctx = makeCtx({ wardenSkipLabelId: null });
    const result = await toggleWardenSkip(ctx, 'issue-1', 'LIA-1', []);
    expect(result.ok).toBe(false);
    expect(result.message).toContain('not in workspace');
  });

  it('adds label when not present', async () => {
    const ctx = makeCtx();
    const result = await toggleWardenSkip(ctx, 'issue-1', 'LIA-1', []);
    expect(result.ok).toBe(true);
    expect(result.message).toContain('added');
    expect(ctx.client.updateIssue).toHaveBeenCalledWith('issue-1', {
      addedLabelIds: ['label-skip'],
    });
    expect(mockLogEvent).toHaveBeenCalledWith(
      'issue-1',
      'LIA-1',
      'label_toggled',
      'warden:skip added',
    );
  });

  it('removes label when already present', async () => {
    const ctx = makeCtx();
    const result = await toggleWardenSkip(ctx, 'issue-1', 'LIA-1', [
      'label-skip',
    ]);
    expect(result.ok).toBe(true);
    expect(result.message).toContain('removed');
    expect(ctx.client.updateIssue).toHaveBeenCalledWith('issue-1', {
      removedLabelIds: ['label-skip'],
    });
  });

  it('returns error on API failure', async () => {
    vi.useFakeTimers();
    const ctx = makeCtx();
    (ctx.client.updateIssue as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('rate limited'),
    );
    const promise = toggleWardenSkip(ctx, 'issue-1', 'LIA-1', []);
    await vi.runAllTimersAsync();
    const result = await promise;
    expect(result.ok).toBe(false);
    expect(result.message).toContain('rate limited');
    vi.useRealTimers();
  });
});

describe('triggerGateRerun', () => {
  it('bounces Ready for Agent through Todo', async () => {
    const ctx = makeCtx();
    const result = await triggerGateRerun(
      ctx,
      'issue-1',
      'LIA-1',
      'Ready for Agent',
    );
    expect(result.ok).toBe(true);
    expect(ctx.client.updateIssue).toHaveBeenCalledTimes(2);
    expect(ctx.client.updateIssue).toHaveBeenNthCalledWith(1, 'issue-1', {
      stateId: 's-todo',
    });
    expect(ctx.client.updateIssue).toHaveBeenNthCalledWith(2, 'issue-1', {
      stateId: 's-ready',
    });
  });

  it('bounces In Review through Agent Working', async () => {
    const ctx = makeCtx();
    const result = await triggerGateRerun(ctx, 'issue-1', 'LIA-1', 'In Review');
    expect(result.ok).toBe(true);
    expect(ctx.client.updateIssue).toHaveBeenNthCalledWith(1, 'issue-1', {
      stateId: 's-working',
    });
    expect(ctx.client.updateIssue).toHaveBeenNthCalledWith(2, 'issue-1', {
      stateId: 's-review',
    });
  });

  it('returns error for ungated state', async () => {
    const ctx = makeCtx();
    const result = await triggerGateRerun(
      ctx,
      'issue-1',
      'LIA-1',
      'Agent Working',
    );
    expect(result.ok).toBe(false);
    expect(result.message).toContain('No gate configured');
  });

  it('returns error when bounce state not found', async () => {
    const ctx = makeCtx();
    ctx.stateByName.delete('Todo');
    const result = await triggerGateRerun(
      ctx,
      'issue-1',
      'LIA-1',
      'Ready for Agent',
    );
    expect(result.ok).toBe(false);
    expect(result.message).toContain('"Todo" not found');
  });

  it('returns error on API failure', async () => {
    const ctx = makeCtx();
    (ctx.client.updateIssue as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('network error'),
    );
    const result = await triggerGateRerun(
      ctx,
      'issue-1',
      'LIA-1',
      'Ready for Agent',
    );
    expect(result.ok).toBe(false);
    expect(result.message).toContain('network error');
  });

  it('logs both state changes', async () => {
    const ctx = makeCtx();
    await triggerGateRerun(ctx, 'issue-1', 'LIA-1', 'Ready for Agent');
    expect(mockLogEvent).toHaveBeenCalledTimes(2);
  });
});

describe('moveIssueState', () => {
  it('moves to a valid state', async () => {
    const ctx = makeCtx();
    const result = await moveIssueState(ctx, 'issue-1', 'LIA-1', 'Done');
    expect(result.ok).toBe(true);
    expect(result.message).toContain('Done');
    expect(ctx.client.updateIssue).toHaveBeenCalledWith('issue-1', {
      stateId: 's-done',
    });
    expect(mockLogEvent).toHaveBeenCalled();
  });

  it('returns error for unknown state', async () => {
    const ctx = makeCtx();
    const result = await moveIssueState(ctx, 'issue-1', 'LIA-1', 'Nonexistent');
    expect(result.ok).toBe(false);
    expect(result.message).toContain('"Nonexistent" not found');
    expect(ctx.client.updateIssue).not.toHaveBeenCalled();
  });

  it('returns error on API failure', async () => {
    const ctx = makeCtx();
    (ctx.client.updateIssue as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error('forbidden'),
    );
    const result = await moveIssueState(ctx, 'issue-1', 'LIA-1', 'Done');
    expect(result.ok).toBe(false);
    expect(result.message).toContain('forbidden');
  });
});
