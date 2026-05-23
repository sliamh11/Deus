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
  getIssuePr,
  getPendingAutoMerges,
  updatePrAutoMergeState,
  upsertIssuePr,
} from './db.js';
import { logger } from './logger.js';
import { extractPrUrl } from './pr-url-extractor.js';
import { macosNotify, notifyPipelineStep } from './linear-notifications.js';
import type { LinearContext } from './linear-dispatcher.js';

const execFileAsync = promisify(execFile);

const CI_POLL_INTERVAL_MS = 60_000;
const CI_CHECK_TIMEOUT_MS = 30_000;
const MERGE_TIMEOUT_MS = 120_000;
const MAX_POLL_ATTEMPTS = 30;

type CiStatus = 'pass' | 'fail' | 'pending';

interface PrChecksResult {
  status: CiStatus;
  summary: string;
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
      await ctx.client.createComment({
        issueId,
        body: `**Auto-merge failed** - ${result.error}`,
      });
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
      logger.warn(
        { issueId, prUrl },
        'auto-merge: timed out, moved to Ready for Agent',
      );
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

  // CI failed — re-dispatch the agent to fix it
  updatePrAutoMergeState(issueId, 'failed');
  notifyPipelineStep(
    ctx,
    issueId,
    ident,
    'automerge_failed',
    checks.summary,
  ).catch(() => {});
  await ctx.client.createComment({
    issueId,
    body: `**Auto-merge blocked** - CI failed: ${checks.summary}\n\nPR: ${prUrl}\n\nMoving to Ready for Agent for re-dispatch.`,
  });
  const readyState = ctx.stateByName.get('Ready for Agent');
  if (readyState) {
    // priority 1 = urgent; ensures CI-fix re-dispatch runs before new work
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

export async function sweepStaleInReview(ctx: LinearContext): Promise<void> {
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

      triggerAutoMerge(ctx, issue.id, issue.identifier).catch((err) => {
        logger.error(
          { issueId: issue.id, err },
          'auto-merge: stale sweep failed',
        );
      });
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
