/**
 * Auto-merge engine for agent PRs.
 *
 * Triggered after output-quality-gate SHIPs when LINEAR_AUTO_MERGE=1.
 * Polls CI status, merges on pass, comments and moves to Backlog on fail.
 */

import { execFile } from 'child_process';
import { promisify } from 'util';

import { isAutoMergeEnabled } from './config.js';
import {
  CIRCUIT_BREAKER_THRESHOLD,
  getConsecutiveFailCount,
  getIssuePr,
  getPendingAutoMerges,
  getPipelineEvents,
  getStageEntryTime,
  logPipelineEvent,
  updatePrAutoMergeState,
  upsertIssuePr,
} from './db.js';
import { logger } from './logger.js';
import { extractPrUrl } from './pr-url-extractor.js';
import { macosNotify, notifyPipelineStep } from './linear-notifications.js';
import type { LinearContext } from './linear-dispatcher.js';

const execFileAsync = promisify(execFile);

const CI_POLL_INTERVAL_MS = 60_000;
// Separate from linear-dispatcher.ts's inline version due to circular import (LinearContext)
async function tripCircuitBreaker(
  ctx: LinearContext,
  issueId: string,
  ident: string,
  failCount: number,
  reason: string,
): Promise<void> {
  const manualReviewState = ctx.stateByName.get('Manual Review Required');
  const parkState = manualReviewState ?? ctx.stateByName.get('Backlog')!;
  await ctx.client.updateIssue(issueId, { stateId: parkState.id });
  await ctx.client.createComment({
    issueId,
    body: `**Circuit breaker tripped** — ${failCount} consecutive CI/merge failures${reason ? ` (${reason})` : ''}. Moved to ${parkState.name}.\n\nTo retry: fix the underlying issue, then move back to **Ready for Agent**.`,
  });
  logPipelineEvent(
    issueId,
    ident,
    'circuit_breaker_tripped',
    `${failCount} consecutive automerge failures`,
  );
}
const CI_CHECK_TIMEOUT_MS = 30_000;
const MERGE_TIMEOUT_MS = 120_000;
const MAX_POLL_ATTEMPTS = 30;

type CiStatus = 'pass' | 'fail' | 'pending';

interface PrChecksResult {
  status: CiStatus;
  summary: string;
}

export async function queryPrState(
  prUrl: string,
): Promise<{ state: 'OPEN' | 'CLOSED' | 'MERGED' } | null> {
  const prNumber = prUrl.match(/\/pull\/(\d+)/)?.[1];
  if (!prNumber) return null;

  const repoMatch = prUrl.match(/github\.com\/([^/]+\/[^/]+)\/pull/);
  const repo = repoMatch?.[1];
  if (!repo) return null;

  try {
    const { stdout } = await execFileAsync(
      'gh',
      ['pr', 'view', prNumber, '--repo', repo, '--json', 'state'],
      { timeout: CI_CHECK_TIMEOUT_MS },
    );
    const data = JSON.parse(stdout) as { state: string };
    const VALID_STATES = new Set(['OPEN', 'CLOSED', 'MERGED']);
    if (!VALID_STATES.has(data.state)) return null;
    return { state: data.state as 'OPEN' | 'CLOSED' | 'MERGED' };
  } catch (err) {
    logger.warn({ prUrl, err }, 'auto-merge: failed to query PR state');
    return null;
  }
}

export async function queryPrChecks(prUrl: string): Promise<PrChecksResult> {
  const prNumber = prUrl.match(/\/pull\/(\d+)/)?.[1];
  if (!prNumber) {
    return { status: 'fail', summary: 'Invalid PR URL' };
  }

  const repoMatch = prUrl.match(/github\.com\/([^/]+\/[^/]+)\/pull/);
  const repo = repoMatch?.[1];
  if (!repo) {
    return { status: 'fail', summary: 'Could not extract repo from URL' };
  }

  try {
    const { stdout } = await execFileAsync(
      'gh',
      ['pr', 'checks', prNumber, '--repo', repo, '--json', 'bucket,name'],
      { timeout: CI_CHECK_TIMEOUT_MS },
    );
    const checks = JSON.parse(stdout) as Array<{
      bucket: string;
      name: string;
    }>;

    if (checks.length === 0) {
      return { status: 'pending', summary: 'No CI checks found yet' };
    }

    const failing = checks.filter((c) => c.bucket === 'fail');
    const pending = checks.filter((c) => c.bucket === 'pending');

    // Check pending BEFORE failing: if any checks are still running,
    // wait for completion before declaring failure (avoids premature redispatch)
    if (pending.length > 0) {
      return {
        status: 'pending',
        summary: `Pending: ${pending.map((c) => c.name).join(', ')}`,
      };
    }
    if (failing.length > 0) {
      return {
        status: 'fail',
        summary: `Failed: ${failing.map((c) => c.name).join(', ')}`,
      };
    }
    return { status: 'pass', summary: `All ${checks.length} checks passed` };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes('no checks')) {
      return { status: 'pending', summary: 'No CI checks found yet' };
    }
    logger.warn({ prUrl, err }, 'auto-merge: failed to query PR checks');
    return { status: 'pending', summary: `Check query error: ${msg}` };
  }
}

async function mergePr(
  prUrl: string,
): Promise<{ merged: boolean; error?: string }> {
  const prNumber = prUrl.match(/\/pull\/(\d+)/)?.[1];
  const repoMatch = prUrl.match(/github\.com\/([^/]+\/[^/]+)\/pull/);
  const repo = repoMatch?.[1];

  if (!prNumber || !repo) {
    return { merged: false, error: 'Invalid PR URL' };
  }

  const preCheck = await queryPrChecks(prUrl);
  if (preCheck.status !== 'pass') {
    return { merged: false, error: `CI not passing: ${preCheck.summary}` };
  }

  try {
    await execFileAsync(
      'gh',
      [
        'pr',
        'merge',
        prNumber,
        '--repo',
        repo,
        '--squash',
        '--delete-branch',
        '--admin',
      ],
      { timeout: MERGE_TIMEOUT_MS },
    );
    return { merged: true };
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    if (msg.includes('already been merged') || msg.includes('MERGED')) {
      logger.info({ prUrl }, 'auto-merge: PR already merged');
      return { merged: true };
    }
    return { merged: false, error: msg };
  }
}

export async function attemptAutoMerge(
  ctx: LinearContext,
  issueId: string,
  prUrl: string,
  identifier?: string,
  attempt = 0,
): Promise<void> {
  if (!isAutoMergeEnabled()) return;
  const ident = identifier ?? 'unknown';

  const checks = await queryPrChecks(prUrl);
  logger.info(
    { issueId, prUrl, status: checks.status, attempt },
    'auto-merge: CI status',
  );

  if (checks.status === 'pass') {
    const result = await mergePr(prUrl);
    if (result.merged) {
      updatePrAutoMergeState(issueId, 'merged');
      const doneState = ctx.stateByName.get('Done');
      if (doneState) {
        const labelUpdate: Record<string, unknown> = {
          stateId: doneState.id,
        };
        const addIds: string[] = [];
        const removeIds: string[] = [];
        if (ctx.gateLabels.wardenSkip) addIds.push(ctx.gateLabels.wardenSkip);
        if (ctx.gateLabels.revise) removeIds.push(ctx.gateLabels.revise);
        if (ctx.gateLabels.evaluating)
          removeIds.push(ctx.gateLabels.evaluating);
        if (addIds.length > 0) labelUpdate.addedLabelIds = addIds;
        if (removeIds.length > 0) labelUpdate.removedLabelIds = removeIds;
        await ctx.client.updateIssue(issueId, labelUpdate);
        await ctx.client.createComment({
          issueId,
          body: `**Auto-merged** - PR ${prUrl} merged after CI passed.`,
        });
      }
      notifyPipelineStep(ctx, issueId, ident, 'automerge_done', prUrl).catch(
        () => {},
      );
      logPipelineEvent(
        issueId,
        ident,
        'circuit_breaker_reset',
        'merge succeeded',
      );
      logger.info({ issueId, prUrl }, 'auto-merge: merged and moved to Done');
    } else {
      logger.warn(
        { issueId, prUrl, error: result.error },
        'auto-merge: merge failed despite passing CI',
      );
      updatePrAutoMergeState(issueId, 'failed');
      notifyPipelineStep(
        ctx,
        issueId,
        ident,
        'automerge_failed',
        result.error,
      ).catch(() => {});
      const mergeFailCount = getConsecutiveFailCount(
        issueId,
        'automerge_failed',
      );
      if (mergeFailCount >= CIRCUIT_BREAKER_THRESHOLD) {
        await tripCircuitBreaker(
          ctx,
          issueId,
          ident,
          mergeFailCount,
          'merge failure',
        );
      } else {
        await ctx.client.createComment({
          issueId,
          body: `**Auto-merge failed** - ${result.error}`,
        });
      }
    }
    return;
  }

  if (checks.status === 'pending') {
    if (attempt >= MAX_POLL_ATTEMPTS) {
      updatePrAutoMergeState(issueId, 'failed');
      notifyPipelineStep(
        ctx,
        issueId,
        ident,
        'automerge_failed',
        'Timed out',
      ).catch(() => {});
      const timeoutFailCount = getConsecutiveFailCount(
        issueId,
        'automerge_failed',
      );
      if (timeoutFailCount >= CIRCUIT_BREAKER_THRESHOLD) {
        await tripCircuitBreaker(
          ctx,
          issueId,
          ident,
          timeoutFailCount,
          'timeout',
        );
      } else {
        await ctx.client.createComment({
          issueId,
          body: `**Auto-merge timed out** - CI still pending after ${MAX_POLL_ATTEMPTS} attempts. PR: ${prUrl}\n\nMoving to Ready for Agent for re-dispatch.`,
        });
        const readyStateTimeout = ctx.stateByName.get('Ready for Agent');
        if (readyStateTimeout) {
          await ctx.client.updateIssue(issueId, {
            stateId: readyStateTimeout.id,
            priority: 1,
          });
        }
      }
      logger.warn({ issueId, prUrl }, 'auto-merge: timed out');
      return;
    }
    setTimeout(() => {
      attemptAutoMerge(ctx, issueId, prUrl, identifier, attempt + 1).catch(
        (err) => {
          logger.error({ issueId, err }, 'auto-merge: re-check failed');
        },
      );
    }, CI_POLL_INTERVAL_MS);
    return;
  }

  updatePrAutoMergeState(issueId, 'failed');
  notifyPipelineStep(
    ctx,
    issueId,
    ident,
    'automerge_failed',
    checks.summary,
  ).catch(() => {});

  const ciFailCount = getConsecutiveFailCount(issueId, 'automerge_failed');
  if (ciFailCount >= CIRCUIT_BREAKER_THRESHOLD) {
    await tripCircuitBreaker(ctx, issueId, ident, ciFailCount, 'CI failure');
    logger.warn(
      { issueId, prUrl, ciFailCount },
      'auto-merge: circuit breaker tripped, parked issue',
    );
  } else {
    await ctx.client.createComment({
      issueId,
      body: `**Auto-merge blocked** - CI failed: ${checks.summary}\n\nPR: ${prUrl}\n\nMoving to Ready for Agent for re-dispatch.`,
    });
    const readyState = ctx.stateByName.get('Ready for Agent');
    if (readyState) {
      await ctx.client.updateIssue(issueId, {
        stateId: readyState.id,
        priority: 1,
      });
    }
    logger.warn(
      { issueId, prUrl },
      'auto-merge: CI failed, moved to Ready for Agent',
    );
  }
}

export async function sweepPendingAutoMerges(
  ctx: LinearContext,
): Promise<void> {
  if (!isAutoMergeEnabled()) return;

  const pending = getPendingAutoMerges();
  if (pending.length === 0) return;

  logger.info(
    { count: pending.length },
    'auto-merge: sweeping pending merges on startup',
  );

  for (const { issue_id, pr_url, identifier: ident } of pending) {
    attemptAutoMerge(ctx, issue_id, pr_url, ident || 'unknown').catch((err) => {
      logger.error({ issueId: issue_id, err }, 'auto-merge: sweep failed');
    });
  }
}

export type CompletionChecker = (issueData: {
  id: string;
  identifier: string;
  title: string;
  description?: string | null;
  labels: Array<{ id: string; name: string }>;
}) => Promise<'SHIP' | 'REVISE'>;

export async function sweepStaleInReview(
  ctx: LinearContext,
  completionCheck?: CompletionChecker,
): Promise<void> {
  if (!isAutoMergeEnabled()) return;

  const inReviewState = ctx.stateByName.get('In Review');
  if (!inReviewState) return;

  try {
    const issues = await ctx.client.issues({
      filter: { state: { id: { eq: inReviewState.id } } },
    });

    let triggered = 0;
    for (const issue of issues.nodes) {
      const labels = await issue.labels();
      if (labels.nodes.some((l) => l.name === 'warden:skip')) continue;

      const pr = getIssuePr(issue.id);
      if (pr?.auto_merge_state === 'pending') continue;
      if (pr?.auto_merge_state === 'merged') {
        logger.warn(
          { issueId: issue.id },
          'auto-merge: issue still In Review but PR marked merged — data inconsistency',
        );
        continue;
      }

      if (completionCheck) {
        const issueData = {
          id: issue.id,
          identifier: issue.identifier,
          title: issue.title,
          description: issue.description,
          labels: labels.nodes.map((l) => ({ id: l.id, name: l.name })),
        };
        completionCheck(issueData)
          .then((verdict) => {
            if (verdict === 'SHIP') {
              return triggerAutoMerge(ctx, issue.id, issue.identifier);
            }
            logger.info(
              { issueId: issue.id },
              'auto-merge: sweep completion-gate REVISE, auto-merge blocked',
            );
          })
          .catch((err) => {
            logger.error(
              { issueId: issue.id, err },
              'auto-merge: stale sweep failed',
            );
          });
      } else {
        triggerAutoMerge(ctx, issue.id, issue.identifier).catch((err) => {
          logger.error(
            { issueId: issue.id, err },
            'auto-merge: stale sweep failed',
          );
        });
      }
      triggered++;
    }

    if (triggered > 0) {
      logger.info(
        { count: triggered },
        'auto-merge: swept stale In Review issues',
      );
    }
  } catch (err) {
    logger.warn({ err }, 'auto-merge: failed to sweep stale In Review issues');
  }
}

/** Issues stuck in Agent Working for longer than this are considered stale. */
const AGENT_WORKING_STALE_THRESHOLD_MS = 60 * 60_000; // 60 minutes

/**
 * Sweeps issues that are stuck in "Agent Working" and reconciles their state.
 *
 * Decision tree for an issue past the stale threshold:
 *   1. Has a merged PR           → move to Done
 *   2. Has an open/closed PR     → move to In Review (existing merge path handles it)
 *   3. No PR, agent_completed    → move to In Review (let completion gate decide)
 *   4. Most recent event is agent_failed → move to Ready for Agent (circuit-breaker aware)
 *   5. No signal at all          → move to Manual Review Required
 *
 * Issues that are actively in-flight or under the threshold are left alone.
 */
export async function sweepStaleAgentWorking(
  ctx: LinearContext,
  thresholdMs = AGENT_WORKING_STALE_THRESHOLD_MS,
): Promise<void> {
  if (!isAutoMergeEnabled()) return;

  const agentWorkingState = ctx.stateByName.get('Agent Working');
  if (!agentWorkingState) return;

  let issues;
  try {
    issues = await ctx.client.issues({
      filter: { state: { id: { eq: agentWorkingState.id } } },
    });
  } catch (err) {
    logger.warn({ err }, 'auto-merge: failed to query Agent Working issues');
    return;
  }

  const now = Date.now();
  let reconciled = 0;

  for (const issue of issues.nodes) {
    // Skip in-flight agents — they are actively running in this process
    if (ctx.inFlightDispatch.has(issue.id)) continue;

    const labels = await issue.labels();
    if (labels.nodes.some((l) => l.name === 'warden:skip')) continue;

    // Skip issues that haven't exceeded the stale threshold
    const entryTime = getStageEntryTime(issue.id, 'Agent Working');
    if (entryTime) {
      const ageMs = now - new Date(entryTime).getTime();
      if (ageMs < thresholdMs) continue;
    }
    // If no entry time in DB, use the issue's own updatedAt as a conservative fallback
    else {
      const updatedAt = issue.updatedAt as Date | string | undefined;
      if (updatedAt) {
        const ageMs =
          now -
          (updatedAt instanceof Date
            ? updatedAt.getTime()
            : new Date(updatedAt as string).getTime());
        if (ageMs < thresholdMs) continue;
      }
      // If we have no timestamp at all, fall through to reconcile
    }

    const ident = issue.identifier;

    // Check for a known PR
    const pr = getIssuePr(issue.id);
    if (pr) {
      if (pr.auto_merge_state === 'pending') continue; // merge already in progress

      const prState = await queryPrState(pr.pr_url);
      if (prState?.state === 'MERGED') {
        // PR is already merged — move straight to Done
        const doneState = ctx.stateByName.get('Done');
        if (doneState) {
          const labelUpdate: Record<string, unknown> = {
            stateId: doneState.id,
          };
          const addIds: string[] = [];
          const removeIds: string[] = [];
          if (ctx.gateLabels.wardenSkip) addIds.push(ctx.gateLabels.wardenSkip);
          if (ctx.gateLabels.revise) removeIds.push(ctx.gateLabels.revise);
          if (ctx.gateLabels.evaluating)
            removeIds.push(ctx.gateLabels.evaluating);
          if (addIds.length > 0) labelUpdate.addedLabelIds = addIds;
          if (removeIds.length > 0) labelUpdate.removedLabelIds = removeIds;
          await ctx.client.updateIssue(issue.id, labelUpdate);
          await ctx.client.createComment({
            issueId: issue.id,
            body: `**State reconciled** — PR ${pr.pr_url} was already merged. Moved to Done.`,
          });
          updatePrAutoMergeState(issue.id, 'merged');
          logPipelineEvent(
            issue.id,
            ident,
            'circuit_breaker_reset',
            'sweep: pr already merged',
          );
          logger.info(
            { issueId: issue.id, prUrl: pr.pr_url },
            'auto-merge: Agent Working sweep → Done (PR already merged)',
          );
          reconciled++;
        }
        continue;
      }

      // PR exists but isn't merged — move to In Review so the normal path handles it
      const reviewState = ctx.stateByName.get('In Review');
      if (reviewState) {
        await ctx.client.updateIssue(issue.id, { stateId: reviewState.id });
        await ctx.client.createComment({
          issueId: issue.id,
          body: `**State reconciled** — issue was stuck in Agent Working with an open PR. Moved to In Review for auto-merge processing.\n\nPR: ${pr.pr_url}`,
        });
        logPipelineEvent(
          issue.id,
          ident,
          'agent_completed',
          'sweep: moved to In Review (open PR found)',
        );
        logger.info(
          { issueId: issue.id, prUrl: pr.pr_url },
          'auto-merge: Agent Working sweep → In Review (open PR)',
        );
        reconciled++;
      }
      continue;
    }

    // No PR recorded — inspect pipeline events to distinguish completed vs. failed vs. no-signal
    const events = getPipelineEvents({ issueId: issue.id });
    const relevantEvents = events.filter(
      (e) =>
        e.event_type === 'agent_completed' ||
        e.event_type === 'agent_failed' ||
        e.event_type === 'agent_started',
    );

    // Find the most recent terminal event
    const lastRelevant = relevantEvents.at(-1);

    if (lastRelevant?.event_type === 'agent_completed') {
      // Agent finished cleanly but somehow state wasn't updated — move to In Review
      const reviewState = ctx.stateByName.get('In Review');
      if (reviewState) {
        await ctx.client.updateIssue(issue.id, {
          stateId: reviewState.id,
          assigneeId: ctx.viewerId,
        });
        await ctx.client.createComment({
          issueId: issue.id,
          body: `**State reconciled** — agent completed but issue was still in Agent Working. Moved to In Review.`,
        });
        logPipelineEvent(
          issue.id,
          ident,
          'circuit_breaker_reset',
          'sweep: agent_completed but stuck in Agent Working',
        );
        logger.info(
          { issueId: issue.id },
          'auto-merge: Agent Working sweep → In Review (agent_completed found)',
        );
        reconciled++;
      }
      continue;
    }

    if (lastRelevant?.event_type === 'agent_failed') {
      // Agent failed — check circuit breaker then route to Ready for Agent or Manual Review
      const failCount = getConsecutiveFailCount(issue.id, 'agent_failed');
      if (failCount >= CIRCUIT_BREAKER_THRESHOLD) {
        await tripCircuitBreaker(
          ctx,
          issue.id,
          ident,
          failCount,
          'stale sweep',
        );
        logger.warn(
          { issueId: issue.id, failCount },
          'auto-merge: Agent Working sweep → circuit breaker tripped',
        );
      } else {
        const readyState = ctx.stateByName.get('Ready for Agent');
        if (readyState) {
          await ctx.client.updateIssue(issue.id, {
            stateId: readyState.id,
          });
          await ctx.client.createComment({
            issueId: issue.id,
            body: `**State reconciled** — agent run failed and issue was stuck in Agent Working. Moved back to Ready for Agent for re-dispatch.`,
          });
          logPipelineEvent(
            issue.id,
            ident,
            'circuit_breaker_reset',
            'sweep: moved to Ready for Agent after agent_failed',
          );
          logger.info(
            { issueId: issue.id },
            'auto-merge: Agent Working sweep → Ready for Agent (agent_failed)',
          );
        }
      }
      reconciled++;
      continue;
    }

    // No recognisable terminal event — the issue is truly stuck with no progress signal
    const manualReviewState = ctx.stateByName.get('Manual Review Required');
    const parkState = manualReviewState ?? ctx.stateByName.get('Backlog')!;
    await ctx.client.updateIssue(issue.id, { stateId: parkState.id });
    await ctx.client.createComment({
      issueId: issue.id,
      body: `**State reconciled** — issue has been stuck in Agent Working with no progress signal for over an hour. Moved to ${parkState.name} for manual review.\n\nTo retry: investigate, then move back to **Ready for Agent**.`,
    });
    logPipelineEvent(
      issue.id,
      ident,
      'circuit_breaker_tripped',
      'sweep: no progress signal, moved to manual review',
    );
    logger.warn(
      { issueId: issue.id },
      'auto-merge: Agent Working sweep → Manual Review Required (no signal)',
    );
    reconciled++;
  }

  if (reconciled > 0) {
    logger.info(
      { count: reconciled },
      'auto-merge: swept stale Agent Working issues',
    );
  }
}

export async function triggerAutoMerge(
  ctx: LinearContext,
  issueId: string,
  identifier?: string,
): Promise<void> {
  if (!isAutoMergeEnabled()) return;

  let pr = getIssuePr(issueId);

  // Fallback: extract from latest agent comment on the issue
  if (!pr) {
    try {
      const issue = await ctx.client.issue(issueId);
      const comments = await issue.comments();
      for (const comment of comments.nodes) {
        const url = extractPrUrl(comment.body, ctx.repoSlug);
        if (url) {
          upsertIssuePr(issueId, url);
          pr = { pr_url: url, branch: null, auto_merge_state: 'none' };
          logger.info(
            { issueId, prUrl: url },
            'auto-merge: extracted PR URL from comment fallback',
          );
          break;
        }
      }
    } catch (err) {
      logger.warn(
        { issueId, err },
        'auto-merge: failed to fetch comments for PR URL fallback',
      );
    }
  }

  if (!pr) {
    logger.info({ issueId }, 'auto-merge: no PR URL found, skipping');
    return;
  }

  const ident = identifier || 'unknown';
  updatePrAutoMergeState(issueId, 'pending');
  notifyPipelineStep(ctx, issueId, ident, 'automerge_pending', pr.pr_url).catch(
    () => {},
  );
  await attemptAutoMerge(ctx, issueId, pr.pr_url, ident);
}
