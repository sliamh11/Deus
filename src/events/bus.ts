import { logger } from '../logger.js';
import type { DeusEvent, EventEnvelope, Handler } from './types.js';

/**
 * In-process observer/pub-sub hub. Three properties are correctness, not style:
 *  - sequential `await` in `emit` preserves the per-turn ordering the
 *    orchestrator's output chain already guarantees;
 *  - per-listener try/catch isolation — a thrown handler cannot break the
 *    emitter or its siblings (the one thing the private prototype lacks);
 *  - the contract is pure: `emit` returns Promise<void> and never reports
 *    per-listener success, so a critical write routed through a listener must
 *    keep its success-coupled follow-ups inside the same handler.
 *
 * Dispatch is O(listeners): a linear scan over [...catch-all, ...by-type].
 */
export class EventBus {
  private readonly anyHandlers: Handler[] = [];
  private readonly byType = new Map<DeusEvent['type'], Handler[]>();

  /** Subscribe to one event type. Returns an unsubscribe function. */
  subscribe<T extends DeusEvent['type']>(
    type: T,
    handler: Handler<Extract<DeusEvent, { type: T }>>,
  ): () => void {
    const list = this.byType.get(type) ?? [];
    list.push(handler as Handler);
    this.byType.set(type, list);
    return () => {
      const arr = this.byType.get(type);
      if (!arr) return;
      const idx = arr.indexOf(handler as Handler);
      if (idx !== -1) arr.splice(idx, 1);
    };
  }

  /**
   * Catch-all subscription — receives every event regardless of type. Reserved
   * for a future multi-family durability mirror (Phase 4+); no production
   * listener uses it yet. (Phase 2's ObservabilitySink uses the typed
   * `subscribe('pipeline.transition')` instead, since it mirrors one family.)
   * Returns an unsubscribe function.
   */
  on(handler: Handler): () => void {
    this.anyHandlers.push(handler);
    return () => {
      const idx = this.anyHandlers.indexOf(handler);
      if (idx !== -1) this.anyHandlers.splice(idx, 1);
    };
  }

  /**
   * Deliver an envelope to catch-all handlers then type-matched handlers, in
   * registration order, awaiting each sequentially. A handler that throws is
   * logged and isolated — it neither stops siblings nor rejects this promise.
   * Targets are snapshotted at emit time, so (un)subscribing mid-emit is safe.
   */
  async emit(env: EventEnvelope): Promise<void> {
    const targets = [...this.anyHandlers, ...(this.byType.get(env.type) ?? [])];
    for (const handler of targets) {
      try {
        await handler(env);
      } catch (err) {
        logger.error(
          { err, type: env.type, correlationId: env.correlationId },
          'event-bus: listener threw — isolated',
        );
      }
    }
  }
}

let _bus: EventBus | null = null;

/** Lazy app-wide singleton, wired at the composition root (index.ts). */
export function getBus(): EventBus {
  if (!_bus) _bus = new EventBus();
  return _bus;
}
