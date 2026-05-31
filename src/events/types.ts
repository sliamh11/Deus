/**
 * Event-hub envelope + taxonomy (Phase 1).
 *
 * One namespaced envelope unifies the orchestrator's divergent event models so
 * any number of observers can subscribe by type and react independently — the
 * pattern is sender -> event -> listener. Phase 1 defines only `agent.done`;
 * later phases add variants additively.
 *
 * See Research/2026-05-31-event-hub-evolution-design.md.
 */

/**
 * Correlates an event to the thing it is about. A namespaced union because many
 * events have no Linear issue — only a group or run — which is what lets ONE bus
 * carry chat-driven turns AND Linear-driven outcomes. Phase 1 exercises only
 * `issue`; the other variants are the forward contract.
 */
export type CorrelationRef =
  | { kind: 'issue'; id: string; identifier?: string }
  | { kind: 'task'; id: string }
  | { kind: 'group'; id: string }
  | { kind: 'run'; id: string };

/**
 * Discriminated union of every event the hub carries. Phase 1: `agent.done`.
 * Phase 2 adds `pipeline.transition` (every pipeline-event log becomes an emit,
 * reusing the existing eventType vocabulary). Add a member here when a phase
 * introduces a new event type.
 */
export type DeusEvent =
  | {
      type: 'agent.done';
      payload: { output?: string; prUrl?: string };
    }
  | {
      type: 'pipeline.transition';
      payload: { eventType: string; detail?: string };
    };

/**
 * What an emitter sends and a handler receives. `actor` is metadata for
 * listeners (e.g. 'bot' marks a self-write so downstream consumers can reason
 * about loops) — it is NOT the Linear webhook actor, which is a separate Linear
 * API field.
 */
export interface EventEnvelope<E extends DeusEvent = DeusEvent> {
  readonly type: E['type'];
  readonly source: string;
  readonly actor: 'agent' | 'bot' | 'human' | 'system';
  readonly correlationId: CorrelationRef;
  readonly ts: string; // ISO 8601
  readonly payload: E['payload'];
}

export type Handler<E extends DeusEvent = DeusEvent> = (
  env: EventEnvelope<E>,
) => void | Promise<void>;
