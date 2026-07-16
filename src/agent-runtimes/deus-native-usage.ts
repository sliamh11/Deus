/**
 * Turn-scoped usage accounting collector for the `deus-native` runtime
 * (LIA-408 / B8 extraction).
 *
 * Extracts the exact per-AI-message semantics of B6's usage loop (formerly
 * inline in `deus-native-backend.ts`'s `runTurn`, lines ~403-465 pre-B8) into
 * a small reusable object so both the PARENT turn and every nested-dispatch
 * CHILD (B8/LIA-408) emit `RuntimeEvent.type === 'usage'` through the exact
 * same code path — never two divergent implementations of "how a usage
 * event is built".
 *
 * Behavior is unchanged from B6 for the parent: one `'usage'` event per
 * AIMessage, unconditionally; token fields are `undefined` (never a
 * fabricated zero) when the provider reported no `usage_metadata`; the
 * turn-level aggregate sums only the AIMessages that DID report usage and is
 * omitted entirely when none did.
 *
 * New for B8: `record()` also returns the LOCAL aggregate for just the
 * messages passed in that call, so a nested dispatch's own
 * `metadata.usage` reflects only that child's usage while the collector's
 * running `aggregate()` keeps growing to the combined parent-plus-every-
 * child total for the final `RunResult.usage`. Every event still uses the
 * PARENT's `outgoingSessionId` (child dispatches have no session of their
 * own — see `nested-dispatch.ts`'s doc comment), and the collector never
 * inserts child messages into the parent's own message list — callers pass
 * each source's messages in separately, so the parent's later message scan
 * cannot double-count child usage.
 */

import type { UsageMetadata, BaseMessage } from '@langchain/core/messages';

import type { RuntimeEventSink } from './types.js';

export interface UsageAggregate {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
}

export interface TurnUsageCollectorDeps {
  /** The PARENT turn's session id — used on every emitted event, including
   *  events raised for a nested-dispatch child's messages (children have no
   *  session id of their own). */
  sessionId: string;
  eventSink: RuntimeEventSink;
}

/** Per-model-call attribution for one `record()` call. */
export interface UsageRecordContext {
  provider: string;
  model: string;
}

function addUsage(
  aggregate: UsageAggregate | undefined,
  usage: UsageMetadata,
): UsageAggregate {
  return {
    inputTokens: (aggregate?.inputTokens ?? 0) + usage.input_tokens,
    outputTokens: (aggregate?.outputTokens ?? 0) + usage.output_tokens,
    totalTokens: (aggregate?.totalTokens ?? 0) + usage.total_tokens,
  };
}

export class TurnUsageCollector {
  private combined: UsageAggregate | undefined;

  constructor(private readonly deps: TurnUsageCollectorDeps) {}

  /**
   * Emits one `'usage'` event per AIMessage in `messages` (identified via
   * the `type: 'ai'` discriminator, matching the original B6 loop — never
   * `instanceof`, never duck-typed on `usage_metadata` presence alone) and
   * folds any reported usage into both the returned LOCAL aggregate and the
   * collector's running combined total.
   */
  async record(
    messages: readonly BaseMessage[],
    ctx: UsageRecordContext,
  ): Promise<UsageAggregate | undefined> {
    let local: UsageAggregate | undefined;
    for (const message of messages) {
      if ((message as { type?: string }).type !== 'ai') continue;
      const usage = (message as { usage_metadata?: UsageMetadata })
        .usage_metadata;
      await this.deps.eventSink({
        type: 'usage',
        sessionId: this.deps.sessionId,
        provider: ctx.provider,
        model: ctx.model,
        inputTokens: usage?.input_tokens,
        outputTokens: usage?.output_tokens,
        totalTokens: usage?.total_tokens,
      });
      if (usage) {
        local = addUsage(local, usage);
        this.combined = addUsage(this.combined, usage);
      }
    }
    return local;
  }

  /** The running combined aggregate across every `record()` call so far —
   *  parent plus every nested-dispatch child. `undefined` when nothing in
   *  the turn has reported usage yet (never a fabricated zero-object). */
  aggregate(): UsageAggregate | undefined {
    return this.combined;
  }
}

export function createTurnUsageCollector(
  deps: TurnUsageCollectorDeps,
): TurnUsageCollector {
  return new TurnUsageCollector(deps);
}
