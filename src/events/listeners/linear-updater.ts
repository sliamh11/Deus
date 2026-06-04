import { logger } from '../../logger.js';
import { logPipelineEvent } from '../../db.js';
import { notifyPipelineStep } from '../../linear-notifications.js';
import type { LinearContext } from '../../linear-dispatcher.js';
import type { EventBus } from '../bus.js';

/**
 * The flagship event-hub listener. Phase 1 handles only `agent.done` and is the
 * sole writer of the "-> In Review" transition plus its two success-coupled
 * bookkeeping events (`agent_completed`, `circuit_breaker_reset`). The
 * dispatcher's inline copy of this write was deleted at the Step-2 cutover;
 * `runIssue` now only emits `agent.done`.
 *
 * The three writes are co-located and `updateIssue` is awaited FIRST, so a write
 * failure (caught and isolated by the bus) exits the handler before the
 * follow-ups run. This is load-bearing: firing `circuit_breaker_reset` on a failed
 * transition would zero the consecutive-failure counter (`getConsecutiveFailCount`
 * in db.ts) and mask repeat failures.
 *
 * Loop-safe: the write reuses ctx.client (the bot user), so the Linear webhook's
 * bot-actor guard (it skips transitions whose actor is the bot user) suppresses
 * this self-triggered transition.
 *
 * Returns an unsubscribe function.
 */
export function registerLinearUpdater(
  bus: EventBus,
  ctx: LinearContext,
): () => void {
  return bus.subscribe('agent.done', async (e) => {
    if (e.correlationId.kind !== 'issue') return;
    const issueId = e.correlationId.id;

    const identifier = e.correlationId.identifier ?? '';
    const reviewState = ctx.stateByName.get('In Review');
    if (!reviewState) {
      logger.warn(
        { issueId },
        'linear-updater: no "In Review" workflow state — skipping transition',
      );
      return;
    }

    // updateIssue awaited first: a throw here exits the handler (and is isolated
    // by the bus), skipping both follow-ups below — never a false reset.
    await ctx.client.updateIssue(issueId, {
      stateId: reviewState.id,
      assigneeId: ctx.viewerId,
    });
    notifyPipelineStep(ctx, issueId, identifier, 'agent_completed').catch(
      () => {},
    );
    logPipelineEvent(
      issueId,
      identifier,
      'circuit_breaker_reset',
      'agent completed successfully',
    );
    logger.info({ issueId }, 'linear-updater: issue moved to In Review');
  });
}
