/**
 * Process-local fan-out broadcaster for `deus-native` runtime activity
 * (LIA-432 / G5).
 *
 * Deus is a single host process; the consumers of this stream are HTTP (SSE)
 * connections living in that same process — so this is deliberately NOT a
 * message broker. It is the Observer pattern: `RuntimeActivityBroadcaster` is
 * the subject, and each `/v1/activity` connection (see
 * `src/odysseus-server.ts`) is an independently removable observer.
 *
 * This is intentionally separate from `src/events/bus.ts`. `EventBus.emit()`
 * is an asynchronously and sequentially AWAITED pipeline-reaction bus over the
 * `DeusEvent` taxonomy (`src/events/types.ts`); routing every runtime
 * token/tool event through it would let a slow/disconnected SSE listener
 * back-pressure the agent turn itself, and would conflate that accepted
 * pipeline-event-hub contract (`docs/decisions/event-hub.md`) with a
 * high-frequency, best-effort observation stream. This module's delivery is
 * synchronous, isolated per-subscriber, and never throws into a producer.
 *
 * Reconnection contract: delivery is fire-and-forward ONLY. There is no
 * replay buffer and no `Last-Event-ID` support — a reconnecting client only
 * receives events published after it (re)subscribes. A stable `streamId`
 * (minted once per broadcaster instance/process epoch) plus a broadcaster-
 * wide, strictly increasing `sequence` (allocated once per published event,
 * before fan-out, identical for every subscriber) lets a client detect a gap
 * (same `streamId`, a jump in `sequence`) or a host restart (changed
 * `streamId`). This avoids retaining potentially sensitive `tool_call`
 * arguments in memory and avoids a new history-size/config contract. See
 * PLAN.md §6 for the full rationale.
 */
import crypto from 'crypto';
import { EventEmitter } from 'events';

import type {
  AgentRuntime,
  AgentRuntimeId,
  RunContext,
  RunResult,
  RuntimeEvent,
  RuntimeEventSink,
  RuntimeSession,
} from './types.js';
import { logger } from '../logger.js';

const ACTIVITY_EVENT = 'activity';

/**
 * Identity of the runtime/turn that produced an envelope. `backend` is
 * whatever `AgentRuntime.name()` reports for the wrapped runtime — in
 * production (see `src/index.ts`) this decorator is only ever applied to the
 * `deus-native` registration, so `backend` is always `'deus-native'` in
 * practice, but the type stays the general `AgentRuntimeId` rather than an
 * unsafe narrowing cast.
 */
export interface RuntimeActivitySource {
  backend: AgentRuntimeId;
  groupFolder: string;
  chatJid: string;
}

/** Variant-specific fields copied from `RuntimeEvent`, one shape per `type`. */
export type RuntimeActivityPayload =
  | { text: string } // output_text | activity
  | { name: string; arguments: Record<string, unknown> } // tool_call
  | {
      // usage — token fields are OMITTED (never fabricated as 0) when the
      // originating RuntimeEvent carried `undefined`, preserving the
      // "a model call happened but usage wasn't reported" distinction.
      sessionId: string;
      provider: string;
      model: string;
      inputTokens?: number;
      outputTokens?: number;
      totalTokens?: number;
    }
  | { sessionRef: RuntimeSession } // session
  | Record<string, never> // turn_complete
  | { error: string }; // error

/**
 * One immutable, fanned-out activity message. Carries both the SSE `id:`
 * line (`${streamId}:${sequence}`) and the JSON body delivered as the SSE
 * `data:` line; `type` doubles as the SSE `event:` line. See the module doc
 * comment above for the ordering and reconnection contract this shape exists
 * to support.
 */
export interface RuntimeActivityEnvelope {
  /** `${streamId}:${sequence}` — exactly matches the SSE `id:` line. */
  id: string;
  /** Random once per `RuntimeActivityBroadcaster` instance/process epoch. */
  streamId: string;
  /**
   * Allocated once by the broadcaster before fan-out, monotonically
   * increasing across every published event in THIS process — global to the
   * broadcaster, not per-client or per-turn.
   */
  sequence: number;
  type: RuntimeEvent['type'];
  /** Server publication time (ISO-8601), generated once before fan-out so
   * every subscriber sees the identical timestamp. Ordering is defined by
   * `sequence`, never by wall-clock time. */
  timestamp: string;
  /** Minted once per `AgentRuntime.runTurn()` call, shared by every event
   * from that turn, so consumers can correlate interleaved groups/turns and
   * identify the terminal event. */
  runId: string;
  source: RuntimeActivitySource;
  payload: RuntimeActivityPayload;
}

/** Exhaustive `RuntimeEvent` → `RuntimeActivityPayload` mapping (PLAN.md §2). */
function toPayload(event: RuntimeEvent): RuntimeActivityPayload {
  switch (event.type) {
    case 'output_text':
      return { text: event.text };
    case 'activity':
      return { text: event.text };
    case 'tool_call':
      return { name: event.name, arguments: event.arguments };
    case 'usage': {
      const payload: {
        sessionId: string;
        provider: string;
        model: string;
        inputTokens?: number;
        outputTokens?: number;
        totalTokens?: number;
      } = {
        sessionId: event.sessionId,
        provider: event.provider,
        model: event.model,
      };
      if (event.inputTokens !== undefined)
        payload.inputTokens = event.inputTokens;
      if (event.outputTokens !== undefined)
        payload.outputTokens = event.outputTokens;
      if (event.totalTokens !== undefined)
        payload.totalTokens = event.totalTokens;
      return payload;
    }
    case 'session':
      return { sessionRef: event.sessionRef };
    case 'turn_complete':
      return {};
    case 'error':
      return { error: event.error };
  }
}

/**
 * Process-wide, single-instance fan-out subject for `deus-native` runtime
 * activity. Owns a Node `EventEmitter`, a random `streamId`, the next
 * `sequence`, and a `closed` flag.
 *
 * `publish()` never throws — not for a closed broadcaster (a safe no-op that
 * allocates no id/sequence and invokes no subscriber, documented defense in
 * depth for a late event from a detached turn), not for a full sequence
 * counter (refused with a logged error rather than silently wrapping and
 * violating global ordering), and not for a subscriber handler that itself
 * throws (isolated per-subscriber so one broken/disconnected client can
 * never propagate into `AgentRuntime.runTurn()` or block delivery to the
 * rest). `subscribe()` on a closed broadcaster is likewise inert and returns
 * a no-op unsubscribe. `close()` is idempotent.
 */
export class RuntimeActivityBroadcaster {
  readonly streamId: string = crypto.randomUUID();
  private sequence = 0;
  private closed = false;
  private readonly emitter = new EventEmitter();

  constructor() {
    // Concurrent SSE subscribers (bounded by MAX_CONCURRENT_SSE in
    // odysseus-server.ts, but still > EventEmitter's default cap of 10) are
    // expected and not a leak — disable the default MaxListenersExceeded
    // warning rather than raising an arbitrary higher cap.
    this.emitter.setMaxListeners(0);
  }

  publish(
    runId: string,
    source: RuntimeActivitySource,
    event: RuntimeEvent,
  ): void {
    if (this.closed) return;
    if (this.sequence >= Number.MAX_SAFE_INTEGER) {
      logger.error(
        { streamId: this.streamId, runId, type: event.type },
        'RuntimeActivityBroadcaster: sequence exhausted — refusing publish rather than wrapping and violating global ordering',
      );
      return;
    }
    this.sequence += 1;
    const envelope: RuntimeActivityEnvelope = {
      id: `${this.streamId}:${this.sequence}`,
      streamId: this.streamId,
      sequence: this.sequence,
      type: event.type,
      timestamp: new Date().toISOString(),
      runId,
      source,
      payload: toPayload(event),
    };
    // `emitter.listeners()` returns a fresh array copy — a snapshot subscriber
    // list — so a handler that unsubscribes mid-publish cannot affect this
    // delivery pass. Each handler is invoked directly (not via `emit()`) and
    // wrapped in its own try/catch, so one throwing/disconnected subscriber
    // cannot stop delivery to the others (EventEmitter.emit would otherwise
    // let a thrown exception abort the remaining listener calls).
    const handlers = this.emitter.listeners(ACTIVITY_EVENT) as Array<
      (envelope: RuntimeActivityEnvelope) => void
    >;
    for (const handler of handlers) {
      try {
        handler(envelope);
      } catch (err) {
        logger.warn(
          { err, streamId: this.streamId },
          'RuntimeActivityBroadcaster: subscriber threw (isolated, ignored)',
        );
      }
    }
  }

  /** Returns an unsubscribe function. Inert (no-op unsubscribe) once closed. */
  subscribe(handler: (envelope: RuntimeActivityEnvelope) => void): () => void {
    if (this.closed) return () => {};
    this.emitter.on(ACTIVITY_EVENT, handler);
    return () => {
      this.emitter.off(ACTIVITY_EVENT, handler);
    };
  }

  /** Current subscriber count — exposed for lifecycle assertions/tests. */
  subscriberCount(): number {
    return this.emitter.listenerCount(ACTIVITY_EVENT);
  }

  /** Idempotent. Drops all subscribers; every later `publish()`/`subscribe()`
   * becomes a documented safe no-op. */
  close(): void {
    if (this.closed) return;
    this.closed = true;
    this.emitter.removeAllListeners(ACTIVITY_EVENT);
  }
}

/**
 * Decorates `runtime` so every `runTurn()` call tees its `RuntimeEvent`
 * stream to `broadcaster.publish()` — in addition to, never instead of, the
 * caller's own `eventSink` — before forwarding to it. `name()`,
 * `capabilities()`, `startOrResume()`, `close()`, downstream sink ordering,
 * and the returned `RunResult` are all otherwise unchanged.
 *
 * A `deus-native` turn that returns `RunResult.status === 'error'` WITHOUT
 * ever emitting a `RuntimeEvent` of `type: 'error'` (the current
 * `DeusNativeRuntime.runTurn()` catch path does exactly this) still needs a
 * terminal envelope so an activity-stream consumer can tell the turn ended
 * in failure — so this decorator publishes ONE broadcaster-only synthetic
 * `error` envelope from `RunResult.error` in that case. That synthetic event
 * is never injected into the caller's own `eventSink` — doing so would
 * change existing channel/task/Linear-dispatch behavior, which this ticket
 * must not do. If the wrapped call unexpectedly throws, the same terminal
 * error is published and the exception is rethrown unchanged, preserving the
 * caller's existing throw semantics.
 */
export function withRuntimeActivityBroadcast(
  runtime: AgentRuntime,
  broadcaster: RuntimeActivityBroadcaster,
): AgentRuntime {
  return {
    name: () => runtime.name(),
    capabilities: () => runtime.capabilities(),
    startOrResume: (runContext: RunContext) =>
      runtime.startOrResume(runContext),
    close: (sessionRef: RuntimeSession) => runtime.close(sessionRef),
    async runTurn(
      runContext: RunContext,
      sessionRef: RuntimeSession,
      eventSink: RuntimeEventSink,
    ): Promise<RunResult> {
      const runId = crypto.randomUUID();
      const source: RuntimeActivitySource = {
        backend: runtime.name(),
        groupFolder: runContext.groupFolder,
        chatJid: runContext.chatJid,
      };
      let sawErrorEvent = false;

      const teeSink: RuntimeEventSink = async (event) => {
        if (event.type === 'error') sawErrorEvent = true;
        // Publish BEFORE invoking the caller's own sink, per the documented
        // tee order — the broadcaster's delivery never depends on (or can be
        // delayed by) the downstream sink's own work.
        broadcaster.publish(runId, source, event);
        await eventSink(event);
      };

      try {
        const result = await runtime.runTurn(runContext, sessionRef, teeSink);
        if (result.status === 'error' && !sawErrorEvent) {
          broadcaster.publish(runId, source, {
            type: 'error',
            error: result.error || 'unknown error',
          });
        }
        return result;
      } catch (err) {
        if (!sawErrorEvent) {
          const message = err instanceof Error ? err.message : String(err);
          broadcaster.publish(runId, source, { type: 'error', error: message });
        }
        throw err;
      }
    },
  };
}
