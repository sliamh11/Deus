import { logger } from '../../logger.js';
import { insertPipelineEventRow } from '../../db.js';
import type { EventBus } from '../bus.js';

// explicit `: boolean` (not literal `true`) so the live branch below stays
// reachable/typecheckable; flip to `false` at the Phase-3 cutover.
const DRY_RUN_DEFAULT: boolean = true;

/**
 * ObservabilitySink — the event-hub's durability mirror for pipeline events.
 * Subscribes to `pipeline.transition` and, when live, writes the row to
 * `linear_pipeline_events`.
 *
 * Loop-safety (load-bearing, the canonical statement of this invariant): the
 * sink writes via `insertPipelineEventRow`, NEVER `logPipelineEvent` — the
 * latter emits `pipeline.transition`, so a sink routed through it would re-fire
 * this handler forever. The non-emitting helper makes that structurally impossible.
 *
 * Phase 2 is dry-run/log-only: the mirrored row's content equals the
 * authoritative `logPipelineEvent` row by construction (same args), so the
 * dry-run only defers the duplicate-row write to the Phase-3 cutover. That
 * cutover must also re-home one thing the envelope does not carry — the
 * `status_summary` UPDATE that `notifyPipelineStep` chains off `logPipelineEvent`'s
 * rowid (one caller); it does not affect this phase.
 *
 * Returns an unsubscribe function.
 */
export function registerObservabilitySink(
  bus: EventBus,
  opts: { dryRun?: boolean } = {},
): () => void {
  const dryRun = opts.dryRun ?? DRY_RUN_DEFAULT;

  return bus.subscribe('pipeline.transition', (env) => {
    if (env.correlationId.kind !== 'issue') return;
    const { id, identifier } = env.correlationId;
    const { eventType, detail } = env.payload;

    if (dryRun) {
      logger.debug(
        { id, eventType },
        'observability-sink[dry-run]: would mirror pipeline.transition -> linear_pipeline_events',
      );
      return;
    }

    // Durable write via the non-emitting helper (loop-safe); `env.ts` keeps the
    // mirrored row's time equal to the originating event's time.
    insertPipelineEventRow(id, identifier ?? '', eventType, detail, env.ts);
  });
}
