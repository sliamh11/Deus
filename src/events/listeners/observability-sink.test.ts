import { beforeEach, describe, expect, it, vi } from 'vitest';

// Mock the db module the sink writes through. Both pipeline-event writers are
// stubbed so the loop-safety test can assert the sink uses the NON-emitting
// `insertPipelineEventRow` and never `logPipelineEvent` (which would re-emit).
vi.mock('../../db.js', () => ({
  insertPipelineEventRow: vi.fn(),
  logPipelineEvent: vi.fn(),
}));

import { EventBus } from '../bus.js';
import { registerObservabilitySink } from './observability-sink.js';
import { insertPipelineEventRow, logPipelineEvent } from '../../db.js';
import type { EventEnvelope } from '../types.js';

function mkTransition(overrides: Partial<EventEnvelope> = {}): EventEnvelope {
  return {
    type: 'pipeline.transition',
    source: 'db.logPipelineEvent',
    actor: 'system',
    correlationId: { kind: 'issue', id: 'ISS-1', identifier: 'LIA-1' },
    ts: '2026-05-31T00:00:00.000Z',
    payload: { eventType: 'agent_completed', detail: 'done' },
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('registerObservabilitySink', () => {
  it('dry-run (default): logs only, writes no row', async () => {
    const bus = new EventBus();
    registerObservabilitySink(bus); // default dryRun = true
    await bus.emit(mkTransition());
    expect(insertPipelineEventRow).not.toHaveBeenCalled();
  });

  it('live: mirrors the row via insertPipelineEventRow with the event timestamp', async () => {
    const bus = new EventBus();
    registerObservabilitySink(bus, { dryRun: false });
    await bus.emit(mkTransition());
    expect(insertPipelineEventRow).toHaveBeenCalledTimes(1);
    expect(insertPipelineEventRow).toHaveBeenCalledWith(
      'ISS-1',
      'LIA-1',
      'agent_completed',
      'done',
      '2026-05-31T00:00:00.000Z',
    );
  });

  it('loop-safety: never writes through the emitting logPipelineEvent', async () => {
    const bus = new EventBus();
    registerObservabilitySink(bus, { dryRun: false });
    await bus.emit(mkTransition());
    // The mirror must go through the non-emitting helper (see sink doc-comment).
    expect(logPipelineEvent).not.toHaveBeenCalled();
    expect(insertPipelineEventRow).toHaveBeenCalledTimes(1);
  });

  it('ignores non-issue correlations (table is issue-keyed)', async () => {
    const bus = new EventBus();
    registerObservabilitySink(bus, { dryRun: false });
    await bus.emit(
      mkTransition({ correlationId: { kind: 'run', id: 'run-1' } }),
    );
    expect(insertPipelineEventRow).not.toHaveBeenCalled();
  });

  it('falls back to empty identifier when the issue ref omits one', async () => {
    const bus = new EventBus();
    registerObservabilitySink(bus, { dryRun: false });
    await bus.emit(
      mkTransition({ correlationId: { kind: 'issue', id: 'ISS-2' } }),
    );
    expect(insertPipelineEventRow).toHaveBeenCalledWith(
      'ISS-2',
      '',
      'agent_completed',
      'done',
      '2026-05-31T00:00:00.000Z',
    );
  });
});
