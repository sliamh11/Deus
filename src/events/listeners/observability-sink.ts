import { insertPipelineEventRow } from '../../db.js';
import type { EventBus } from '../bus.js';

/**
 * ObservabilitySink — the event-hub's durability mirror for pipeline events.
 * On `pipeline.transition` it writes the row to `linear_pipeline_events`. As of
 * the Phase-3 cutover it is LIVE and is the durable writer for every emitted
 * pipeline event (the fire-and-forget `logPipelineEvent` callers); the inline
 * write in `logPipelineEvent` was deleted.
 *
 * Loop-safety (load-bearing, the canonical statement of this invariant): the
 * sink writes via `insertPipelineEventRow`, NEVER `logPipelineEvent` — the
 * latter emits `pipeline.transition`, so a sink routed through it would re-fire
 * this handler forever. The non-emitting helper makes that structurally impossible.
 *
 * NOT the sole writer (partial cutover, see docs/decisions/event-hub.md): the
 * `notifyPipelineStep` path keeps a synchronous `insertPipelineEventRow` because
 * it needs the rowid for `status_summary` AND the row present before its
 * `updateUnifiedComment` DB-read; those events are deliberately NOT emitted, so
 * the sink never double-writes them.
 *
 * Returns an unsubscribe function.
 */
export function registerObservabilitySink(bus: EventBus): () => void {
  return bus.subscribe('pipeline.transition', (env) => {
    if (env.correlationId.kind !== 'issue') return;
    const { id, identifier } = env.correlationId;
    const { eventType, detail } = env.payload;

    // Durable write via the non-emitting helper (loop-safe); `env.ts` keeps the
    // mirrored row's time equal to the originating event's time.
    insertPipelineEventRow(id, identifier ?? '', eventType, detail, env.ts);
  });
}
