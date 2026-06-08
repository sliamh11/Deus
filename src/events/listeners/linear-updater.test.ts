import { beforeEach, describe, expect, it, vi } from 'vitest';

// Mock the two side-effecting modules the listener writes through. Only the
// symbols the listener imports are needed; LinearContext is a type-only import
// so linear-dispatcher.ts is never loaded at runtime.
vi.mock('../../db.js', () => ({
  logPipelineEvent: vi.fn(),
}));
vi.mock('../../linear-notifications.js', () => ({
  notifyPipelineStep: vi.fn().mockResolvedValue(undefined),
}));

import { EventBus } from '../bus.js';
import { registerLinearUpdater } from './linear-updater.js';
import { logPipelineEvent } from '../../db.js';
import { notifyPipelineStep } from '../../linear-notifications.js';
import type { LinearContext } from '../../linear-dispatcher.js';
import type { EventEnvelope } from '../types.js';

function mkCtx(updateIssue: ReturnType<typeof vi.fn>): LinearContext {
  return {
    client: { updateIssue } as unknown as LinearContext['client'],
    stateByName: new Map([
      ['In Review', { id: 'review-id', name: 'In Review' }],
    ]),
    viewerId: 'viewer-id',
  } as unknown as LinearContext;
}

function mkDone(overrides: Partial<EventEnvelope> = {}): EventEnvelope {
  return {
    type: 'agent.done',
    source: 'test',
    actor: 'bot',
    correlationId: { kind: 'issue', id: 'ISS-1', identifier: 'LIA-1' },
    ts: new Date().toISOString(),
    payload: { output: 'done' },
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('registerLinearUpdater', () => {
  it('moves the issue to In Review and fires both follow-ups', async () => {
    const bus = new EventBus();
    const updateIssue = vi.fn().mockResolvedValue(undefined);
    registerLinearUpdater(bus, mkCtx(updateIssue));
    await bus.emit(mkDone());
    expect(updateIssue).toHaveBeenCalledTimes(1);
    expect(updateIssue).toHaveBeenCalledWith('ISS-1', {
      stateId: 'review-id',
      assigneeId: 'viewer-id',
    });
    expect(notifyPipelineStep).toHaveBeenCalledWith(
      expect.anything(),
      'ISS-1',
      'LIA-1',
      'agent_completed',
    );
    expect(logPipelineEvent).toHaveBeenCalledWith(
      'ISS-1',
      'LIA-1',
      'circuit_breaker_reset',
      'agent completed successfully',
    );
  });

  it('write fails: emit resolves (isolated) and NEITHER follow-up fires', async () => {
    const bus = new EventBus();
    const updateIssue = vi.fn().mockRejectedValue(new Error('linear 500'));
    registerLinearUpdater(bus, mkCtx(updateIssue));
    await expect(bus.emit(mkDone())).resolves.toBeUndefined();
    expect(updateIssue).toHaveBeenCalledTimes(1);
    // Load-bearing: a failed transition must NOT fire circuit_breaker_reset
    // (which would zero the consecutive-fail counter and mask repeat failures)
    // or agent_completed. The awaited updateIssue throw exits the handler first.
    expect(notifyPipelineStep).not.toHaveBeenCalled();
    expect(logPipelineEvent).not.toHaveBeenCalled();
  });

  it('ignores non-issue correlations', async () => {
    const bus = new EventBus();
    const updateIssue = vi.fn().mockResolvedValue(undefined);
    registerLinearUpdater(bus, mkCtx(updateIssue));
    await bus.emit(mkDone({ correlationId: { kind: 'run', id: 'run-1' } }));
    expect(updateIssue).not.toHaveBeenCalled();
  });
});
