/**
 * Auto-merge engine for agent PRs.
 *
 * Triggered after output-quality-gate SHIPs when LINEAR_AUTO_MERGE=1.
 * Polls CI status, merges on pass, comments and moves to Backlog on fail.
 */

import { execFile } from 'child_process';
import { promisify } from 'util';

import { LINEAR_AUTO_MERGE } from './config.js';
import {
  getIssuePr,
  getPendingAutoMerges,
  updatePrAutoMergeState,
  upsertIssuePr,
} from './db.js';
import { logger } from './logger.js';
import { extractPrUrl } from './pr-url-extractor.js';
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

    if (failing.length > 0) {
      return {
        status: 'fail',
        summary: `Failed: ${failing.map((c) => c.name).join(', ')}`,
      };
    }
    if (pending.length > 0) {
      return {
        status: 'pending',
        summary: `Pending: ${pending.map((c) => c.name).join(', ')}`,
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
      ['pr', 'merge', prNumber, '--repo', repo, '--squash', '--delete-branch'],
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
  attempt = 0,
): Promise<void> {
  if (!LINEAR_AUTO_MERGE) return;

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
        await ctx.client.updateIssue(issueId, { stateId: doneState.id });
        await ctx.client.createComment({
          issueId,
          body: `**Auto-merged** - PR ${prUrl} merged after CI passed.`,
        });
      }
      logger.info({ issueId, prUrl }, 'auto-merge: merged and moved to Done');
    } else {
      logger.warn(
        { issueId, prUrl, error: result.error },
        'auto-merge: merge failed despite passing CI',
      );
      updatePrAutoMergeState(issueId, 'failed');
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
      await ctx.client.createComment({
        issueId,
        body: `**Auto-merge timed out** - CI still pending after ${MAX_POLL_ATTEMPTS} attempts. PR: ${prUrl}`,
      });
      logger.warn({ issueId, prUrl }, 'auto-merge: timed out');
      return;
    }
    setTimeout(() => {
      attemptAutoMerge(ctx, issueId, prUrl, attempt + 1).catch((err) => {
        logger.error({ issueId, err }, 'auto-merge: re-check failed');
      });
    }, CI_POLL_INTERVAL_MS);
    return;
  }

  // CI failed
  updatePrAutoMergeState(issueId, 'failed');
  await ctx.client.createComment({
    issueId,
    body: `**Auto-merge blocked** - CI failed: ${checks.summary}\n\nPR: ${prUrl}`,
  });
  const backlogState = ctx.stateByName.get('Backlog');
  if (backlogState) {
    await ctx.client.updateIssue(issueId, { stateId: backlogState.id });
  }
  logger.warn({ issueId, prUrl }, 'auto-merge: CI failed, moved to Backlog');
}

export async function sweepPendingAutoMerges(
  ctx: LinearContext,
): Promise<void> {
  if (!LINEAR_AUTO_MERGE) return;

  const pending = getPendingAutoMerges();
  if (pending.length === 0) return;

  logger.info(
    { count: pending.length },
    'auto-merge: sweeping pending merges on startup',
  );

  for (const { issue_id, pr_url } of pending) {
    attemptAutoMerge(ctx, issue_id, pr_url).catch((err) => {
      logger.error({ issueId: issue_id, err }, 'auto-merge: sweep failed');
    });
  }
}

export async function triggerAutoMerge(
  ctx: LinearContext,
  issueId: string,
): Promise<void> {
  if (!LINEAR_AUTO_MERGE) return;

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

  updatePrAutoMergeState(issueId, 'pending');
  await attemptAutoMerge(ctx, issueId, pr.pr_url);
}
