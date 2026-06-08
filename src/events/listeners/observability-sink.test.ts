import { beforeEach, describe, expect, it } from 'vitest';

// Real-DB integration tests (Phase-3 cutover, LIA-166): the sink is now the
// live durable writer, so we assert actual ROW COUNTS in a temp DB rather than
// mock the writers. Row-count assertions also prove loop-safety: a re-emit loop
// would yield >1 row (or hang), so "exactly one row per emit" is the guard.
import { EventBus, getBus } from '../bus.js';
import { registerObservabilitySink } from './observability-sink.js';
import {
  _initTestDatabase,
  getPipelineEvents,
  insertPipelineEventRow,
  logPipelineEvent,
} from '../../db.js';
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

const flush = () => new Promise((r) => setTimeout(r, 10));

beforeEach(() => {
  _initTestDatabase();
});

describe('ObservabilitySink (Phase 3 — live)', () => {
  it('writes exactly one row per emitted pipeline.transition, preserving env.ts', async () => {
    const bus = new EventBus();
    registerObservabilitySink(bus);
    await bus.emit(mkTransition());

    const rows = getPipelineEvents({ issueId: 'ISS-1' });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      identifier: 'LIA-1',
      event_type: 'agent_completed',
      detail: 'done',
      created_at: '2026-05-31T00:00:00.000Z',
    });
  });

  it('loop-safe: one emit yields one row (no re-emit storm)', async () => {
    const bus = new EventBus();
    registerObservabilitySink(bus);
    await bus.emit(mkTransition());
    expect(getPipelineEvents({ issueId: 'ISS-1' })).toHaveLength(1);
  });

  it('ignores non-issue correlations (table is issue-keyed)', async () => {
    const bus = new EventBus();
    registerObservabilitySink(bus);
    await bus.emit(
      mkTransition({ correlationId: { kind: 'run', id: 'run-1' } }),
    );
    expect(getPipelineEvents()).toHaveLength(0);
  });

  it('falls back to empty identifier when the issue ref omits one', async () => {
    const bus = new EventBus();
    registerObservabilitySink(bus);
    await bus.emit(
      mkTransition({ correlationId: { kind: 'issue', id: 'ISS-2' } }),
    );
    const rows = getPipelineEvents({ issueId: 'ISS-2' });
    expect(rows).toHaveLength(1);
    expect(rows[0].identifier).toBe('');
  });

  it('microtask-ordering guard: an awaiting catch-all on() does not starve the sink write', async () => {
    // The live-wire confirmation: even with a slow (awaiting) catch-all ahead of
    // the sink in the sequential-await chain, the sink's row must still land. This
    // is the landmine the advisor flagged — guard it deterministically.
    const bus = new EventBus();
    let onRan = false;
    bus.on(async () => {
      await Promise.resolve();
      onRan = true;
    });
    registerObservabilitySink(bus);
    await bus.emit(mkTransition());

    expect(onRan).toBe(true);
    expect(getPipelineEvents({ issueId: 'ISS-1' })).toHaveLength(1);
  });
});

describe('Phase 3 no-double-write (production wiring via getBus)', () => {
  it('logPipelineEvent (emit-only) + live sink → exactly one row', async () => {
    const unsub = registerObservabilitySink(getBus());
    try {
      // logPipelineEvent no longer inserts inline — it only emits; the live sink
      // performs the single durable write. Exactly one row, never two.
      logPipelineEvent('ISS-9', 'LIA-9', 'gate_ship', 'ok');
      await flush();
      expect(getPipelineEvents({ issueId: 'ISS-9' })).toHaveLength(1);
    } finally {
      unsub();
    }
  });

  it('insertPipelineEventRow (notifyPipelineStep path, no emit) + live sink → exactly one row', async () => {
    const unsub = registerObservabilitySink(getBus());
    try {
      // notifyPipelineStep's path inserts synchronously and does NOT emit, so the
      // sink is not triggered — no double-write. (Covers the merge_conflict path
      // after its redundant bare logPipelineEvent was deleted.)
      insertPipelineEventRow('ISS-10', 'LIA-10', 'merge_conflict', 'url');
      await flush();
      expect(getPipelineEvents({ issueId: 'ISS-10' })).toHaveLength(1);
    } finally {
      unsub();
    }
  });
});
