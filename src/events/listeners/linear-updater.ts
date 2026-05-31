import { logger } from '../../logger.js';
import { logPipelineEvent } from '../../db.js';
import { notifyPipelineStep } from '../../linear-notifications.js';
import type { LinearContext } from '../../linear-dispatcher.js';
import type { EventBus } from '../bus.js';

/**
 * Step-1 (dual-run) default: the listener is log-only and the dispatcher's
 * inline write stays authoritative, so we can observe the live emit path on
 * real traffic without changing behavior. At Step-2 cutover this flips to
 * `false` and the inline block in linear-dispatcher.ts is deleted — removing
 * this constant (and the `dryRun` plumbing) IS the cutover diff.
 */
const DRY_RUN_DEFAULT: boolean = true;

/**
 * The flagship event-hub listener. Phase 1 handles only `agent.done`, absorbing
 * the dispatcher's "-> In Review" transition plus its two success-coupled
 * bookkeeping events (`agent_completed`, `circuit_breaker_reset`).
 *
 * The three writes are co-located and `updateIssue` is awaited FIRST, so a write
 * failure (caught and isolated by the bus) exits the handler before the
 * follow-ups run — exact parity with the post-run try/catch in the dispatcher's
 * `runIssue`. This is load-bearing: firing `circuit_breaker_reset` on a failed
 * transition would zero the consecutive-failure counter (`getConsecutiveFailCount`
 * in db.ts) and mask repeat failures.
 *
 * Loop-safe: the write reuses ctx.client (the bot user), so the Linear webhook's
 * bot-actor guard (it skips transitions whose actor is the bot user) suppresses
 * this self-triggered transition exactly as the inline write does today.
 *
 * Returns an unsubscribe function.
 */
export function registerLinearUpdater(
  bus: EventBus,
  ctx: LinearContext,
  opts: { dryRun?: boolean } = {},
): () => void {
  const dryRun = opts.dryRun ?? DRY_RUN_DEFAULT;

  return bus.subscribe('agent.done', async (e) => {
    if (e.correlationId.kind !== 'issue') return;
    const issueId = e.correlationId.id;

    if (dryRun) {
      logger.info(
        { issueId },
        'linear-updater[dry-run]: would set In Review + agent_completed + circuit_breaker_reset',
      );
      return;
    }

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
