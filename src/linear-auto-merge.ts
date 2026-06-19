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
  getOpenPrsForActiveIssues,
  getPendingAutoMerges,
  logPipelineEvent,
  updatePrAutoMergeState,
  upsertIssuePr,
} from './db.js';
import { logger } from './logger.js';
import { extractPrUrl } from './pr-url-extractor.js';
import { notifyPipelineStep } from './linear-notifications.js';
import { fireAndForget } from './async/index.js';
import { envPositiveInt } from './env-utils.js';
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

// --admin is the only viable merge path on this solo repo (LIA-215/LIA-147):
// branch protection requires an approving review no second human can give, so
// GitHub-native --auto can never complete (it sits BLOCKED forever) — and gh
// rejects --admin+--auto together anyway. Require CI green first, then merge.
// The "wait for CI" responsibility lives in pollUntilMergeable, not in gh.
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

  const args = [
    'pr',
    'merge',
    prNumber,
    '--repo',
    repo,
    '--squash',
    '--delete-branch',
    '--admin',
  ];

  try {
    await execFileAsync('gh', args, { timeout: MERGE_TIMEOUT_MS });
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

async function handleMergeSuccess(
  ctx: LinearContext,
  issueId: string,
  prUrl: string,
  ident: string,
): Promise<void> {
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
    if (ctx.gateLabels.evaluating) removeIds.push(ctx.gateLabels.evaluating);
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
  logPipelineEvent(issueId, ident, 'circuit_breaker_reset', 'merge succeeded');
  logger.info({ issueId, prUrl }, 'auto-merge: merged and moved to Done');
}

async function handleMergeFailure(
  ctx: LinearContext,
  issueId: string,
  prUrl: string,
  ident: string,
  reason: string,
  failEvent: 'automerge_failed',
  requeue: boolean,
): Promise<void> {
  updatePrAutoMergeState(issueId, 'failed');
  notifyPipelineStep(ctx, issueId, ident, failEvent, reason).catch(() => {});

  const failCount = getConsecutiveFailCount(issueId, failEvent);
  if (failCount >= CIRCUIT_BREAKER_THRESHOLD) {
    await tripCircuitBreaker(ctx, issueId, ident, failCount, reason);
    logger.warn(
      { issueId, prUrl, failCount },
      'auto-merge: circuit breaker tripped, parked issue',
    );
  } else if (requeue) {
    await ctx.client.createComment({
      issueId,
      body: `**Auto-merge blocked** - ${reason}\n\nPR: ${prUrl}\n\nMoving to Ready for Agent for re-dispatch.`,
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
      'auto-merge: failed, moved to Ready for Agent',
    );
  } else {
    await ctx.client.createComment({
      issueId,
      body: `**Auto-merge failed** - ${reason}`,
    });
  }
}

export async function attemptAutoMerge(
  ctx: LinearContext,
  issueId: string,
  prUrl: string,
  identifier?: string,
): Promise<void> {
  if (!isAutoMergeEnabled()) return;
  const ident = identifier ?? 'unknown';

  // Decide on the CURRENT CI state. GitHub-native --auto is dead on this repo
  // (see mergePr), so we poll to green ourselves (LIA-215).
  const checks = await queryPrChecks(prUrl);
  logger.info(
    { issueId, prUrl, status: checks.status },
    'auto-merge: initial CI status',
  );

  if (checks.status === 'fail') {
    // A real CI failure — re-dispatch a fresh agent attempt.
    await handleMergeFailure(
      ctx,
      issueId,
      prUrl,
      ident,
      `CI failed: ${checks.summary}`,
      'automerge_failed',
      true,
    );
    return;
  }

  if (checks.status === 'pending') {
    // CI still running — hand off to a detached bounded poll so the caller (an
    // awaited webhook cooldown callback) returns immediately. The PR stays
    // auto_merge_state='pending'; the startup sweep re-runs this on restart.
    fireAndForget(() => pollUntilMergeable(ctx, issueId, prUrl, ident), {
      name: 'auto-merge.poll',
    });
    return;
  }

  // CI already green — merge now.
  await mergeOrFail(ctx, issueId, prUrl, ident);
}

/**
 * --admin-merge a green PR and route the outcome. Shared by attemptAutoMerge
 * and pollUntilMergeable. A merge-call failure here is NOT requeued (requeue
 * false): the build was green, so a fresh agent attempt would not help.
 */
async function mergeOrFail(
  ctx: LinearContext,
  issueId: string,
  prUrl: string,
  ident: string,
): Promise<void> {
  const result = await mergePr(prUrl);
  if (result.merged) {
    await handleMergeSuccess(ctx, issueId, prUrl, ident);
  } else {
    await handleMergeFailure(
      ctx,
      issueId,
      prUrl,
      ident,
      result.error ?? 'unknown merge error',
      'automerge_failed',
      false,
    );
  }
}

/**
 * GitHub-ingress entry: merge the PR IFF its CI is currently green, else NO-OP.
 *
 * SECURITY (LIA-315 Phase 4): this is the ONLY merge entry the public `/github` webhook may
 * call. Unlike `attemptAutoMerge`, it NEVER reaches `handleMergeFailure(requeue=true)` — on a
 * `fail`/`pending` re-query it does nothing (the existing poller handles those). So a
 * webhook delivery can never move an issue to "Ready for Agent" / spawn an agent. We re-query
 * authoritative CI here rather than trust the webhook payload, so a forged-but-signed success
 * event for a since-red PR is a safe no-op.
 */
export async function mergeIfGreen(
  ctx: LinearContext,
  issueId: string,
  prUrl: string,
  identifier?: string,
): Promise<void> {
  if (!isAutoMergeEnabled()) return;
  const ident = identifier ?? 'unknown';
  const checks = await queryPrChecks(prUrl);
  if (checks.status === 'pass') {
    await mergeOrFail(ctx, issueId, prUrl, ident);
  } else {
    logger.info(
      { issueId, prUrl, status: checks.status },
      'github-webhook: CI not green — no-op (poller handles fail/pending)',
    );
  }
}

/**
 * GitHub-ingress entry: advance the issue to "Done" IFF the PR is actually merged, else NO-OP.
 * Re-queries authoritative PR state (never trusts the payload). `handleMergeSuccess` only moves
 * to "Done" — it can never spawn an agent. Intentionally NOT gated on `isAutoMergeEnabled()`
 * (unlike `mergeIfGreen`): a genuinely-merged PR should reach "Done" regardless of that flag.
 */
export async function markDoneIfMerged(
  ctx: LinearContext,
  issueId: string,
  prUrl: string,
  identifier?: string,
): Promise<void> {
  const ident = identifier ?? 'unknown';
  const state = await queryPrState(prUrl);
  if (state?.state === 'MERGED') {
    await handleMergeSuccess(ctx, issueId, prUrl, ident);
  }
}

/**
 * Poll a PR's CI until it reaches a terminal state, then --admin-merge on pass.
 * Detached via fireAndForget so it never blocks the dispatch path (LIA-215).
 *
 *  - pass    → --admin merge (mergeOrFail).
 *  - fail    → failure(requeue): a broken build warrants a fresh agent attempt.
 *  - pending → wait CI_POLL_INTERVAL_MS, retry, up to AUTO_MERGE_POLL_MAX_ATTEMPTS.
 *  - exhausted (still pending past the cap) → PARK: leave auto_merge_state
 *    'pending', no requeue, no circuit-breaker, but POST a comment so the parked
 *    PR is visible, not dark. pending != failure, so we must NOT thrash the
 *    agent. sweepPendingAutoMerges re-runs this on the next restart. NOTE: that
 *    sweep is startup-only, so a parked PR waits until the next deploy/restart —
 *    acceptable for a solo pipeline that restarts on every deploy; a periodic
 *    sweep is a deferred option.
 *
 * CAVEAT: queryPrChecks maps a transient gh error (auth/network/rate-limit) to
 * 'pending' (see its catch), so a SUSTAINED gh outage looks like slow CI and
 * parks after the cap rather than alerting. A distinct 'error' status that
 * requeues after K consecutive errors is a deferred improvement; the
 * cap-exhaustion comment keeps such a PR visible in the meantime.
 */
async function pollUntilMergeable(
  ctx: LinearContext,
  issueId: string,
  prUrl: string,
  ident: string,
): Promise<void> {
  // Max CI-poll cycles before parking a still-pending PR (LIA-215). 20 × 60s ≈
  // 20 min — generous headroom over this repo's ~3-min CI. Read at call time so
  // it's env-overridable (and test-tunable) without a restart.
  const maxAttempts = envPositiveInt('AUTO_MERGE_POLL_MAX_ATTEMPTS', 20);
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    await new Promise<void>((resolve) =>
      setTimeout(resolve, CI_POLL_INTERVAL_MS),
    );

    const checks = await queryPrChecks(prUrl);
    logger.info(
      { issueId, prUrl, status: checks.status, attempt: attempt + 1 },
      'auto-merge: CI poll',
    );

    if (checks.status === 'pending') continue;

    if (checks.status === 'fail') {
      await handleMergeFailure(
        ctx,
        issueId,
        prUrl,
        ident,
        `CI failed: ${checks.summary}`,
        'automerge_failed',
        true,
      );
      return;
    }

    // pass
    await mergeOrFail(ctx, issueId, prUrl, ident);
    return;
  }

  // Cap exhausted, CI still pending. PARK — do not requeue or trip the breaker
  // (pending != failure), but post a comment so the parked PR is visible rather
  // than going dark. The startup sweep re-runs this attempt on the next restart.
  logger.warn(
    { issueId, prUrl, attempts: maxAttempts },
    'auto-merge: CI still pending after poll cap, leaving pending for the next startup sweep',
  );
  await ctx.client
    .createComment({
      issueId,
      body: `**Auto-merge waiting** - CI still pending after ${maxAttempts} poll cycles. Left in pending (not failed); will retry on the next sweep. PR: ${prUrl}`,
    })
    .catch(() => {});
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
    // Check whether GitHub's auto-merge already completed while we were down.
    queryPrState(pr_url)
      .then(async (state) => {
        if (state?.state === 'MERGED') {
          logger.info(
            { issueId: issue_id, prUrl: pr_url },
            'auto-merge: PR already merged by GitHub auto-merge, syncing state',
          );
          await handleMergeSuccess(ctx, issue_id, pr_url, ident || 'unknown');
        } else {
          // Not yet merged — re-run the full attempt (re-checks CI and either
          // --admin-merges now or schedules the poll, LIA-215).
          await attemptAutoMerge(ctx, issue_id, pr_url, ident || 'unknown');
        }
      })
      .catch((err) => {
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

/**
 * Builds a rebase context prompt to inject when routing a conflicting PR back
 * to Ready for Agent.
 */
export function buildConflictRebasePrompt(
  prUrl: string,
  changedFiles: number,
  baseBranch: string,
): string {
  const lines = [
    '<conflict-context>',
    'The PR for this issue has a merge conflict with the base branch.',
    `- PR: ${prUrl}`,
    `- Changed files: ${changedFiles}`,
    `- Base branch: ${baseBranch}`,
    '',
    'To resolve: check out the branch, rebase onto the latest base branch',
    `(\`git fetch origin && git rebase origin/${baseBranch}\`), resolve any`,
    'conflicts, then force-push the branch.',
    '</conflict-context>',
  ];
  return lines.join('\n');
}

type MergeableState = 'MERGEABLE' | 'CONFLICTING' | 'UNKNOWN';

interface PrMergeabilityResult {
  mergeable: MergeableState;
  mergeStateStatus: string;
  changedFiles: number;
  headRefName: string;
  baseRefName: string;
}

async function queryPrMergeability(
  prUrl: string,
): Promise<PrMergeabilityResult | null> {
  const prNumber = prUrl.match(/\/pull\/(\d+)/)?.[1];
  if (!prNumber) return null;
  const repoMatch = prUrl.match(/github\.com\/([^/]+\/[^/]+)\/pull/);
  const repo = repoMatch?.[1];
  if (!repo) return null;

  try {
    const { stdout } = await execFileAsync(
      'gh',
      [
        'pr',
        'view',
        prNumber,
        '--repo',
        repo,
        '--json',
        'mergeable,mergeStateStatus,changedFiles,headRefName,baseRefName',
      ],
      { timeout: CI_CHECK_TIMEOUT_MS },
    );
    return JSON.parse(stdout) as PrMergeabilityResult;
  } catch (err) {
    logger.warn(
      { prUrl, err },
      'conflict-check: failed to query PR mergeability',
    );
    return null;
  }
}

const LARGE_PR_FILE_THRESHOLD = 10;

/**
 * Sweeps active issues (Agent Working + In Review) for conflicting PRs.
 * On CONFLICTING: logs event, applies Conflict label, routes to Manual Review
 * Required (>=10 changed files) or Ready for Agent with rebase context (<10).
 * Skips issues already labeled "conflict" or in Manual Review Required.
 * Runs every poll cycle; lightweight because it only hits the GitHub API for
 * PRs that exist in the DB.
 */
export async function checkConflictingPrs(ctx: LinearContext): Promise<void> {
  const activePrs = getOpenPrsForActiveIssues();
  if (activePrs.length === 0) return;

  for (const { issue_id, pr_url, identifier } of activePrs) {
    const ident = identifier ?? 'unknown';

    try {
      const mergeability = await queryPrMergeability(pr_url);
      if (!mergeability) continue;

      // UNKNOWN means GitHub is still computing — skip, check next poll
      if (mergeability.mergeable === 'UNKNOWN') {
        logger.debug(
          { issueId: issue_id, prUrl: pr_url },
          'conflict-check: mergeable=UNKNOWN, will retry next poll',
        );
        continue;
      }

      if (mergeability.mergeable !== 'CONFLICTING') continue;

      // Fetch current labels to check skip conditions
      let currentLabels: Array<{ id: string; name: string }> = [];
      try {
        const issue = await ctx.client.issue(issue_id);
        const labelConn = await issue.labels();
        currentLabels = labelConn.nodes.map((l) => ({
          id: l.id,
          name: l.name,
        }));
      } catch (err) {
        logger.warn(
          { issueId: issue_id, err },
          'conflict-check: failed to fetch issue labels, skipping',
        );
        continue;
      }

      // Skip if already labeled conflict
      if (currentLabels.some((l) => l.name === 'conflict')) {
        logger.debug(
          { issueId: issue_id },
          'conflict-check: already labeled conflict, skipping',
        );
        continue;
      }

      // Skip if already in Manual Review Required
      const issueObj = await ctx.client.issue(issue_id);
      const issueState = await issueObj.state;
      if (issueState?.name === 'Manual Review Required') {
        logger.debug(
          { issueId: issue_id },
          'conflict-check: already in Manual Review Required, skipping',
        );
        continue;
      }

      // notifyPipelineStep is the authoritative writer (row + status_summary +
      // comment); a bare logPipelineEvent here would double-write via the sink.
      notifyPipelineStep(ctx, issue_id, ident, 'merge_conflict', pr_url).catch(
        () => {},
      );

      // Apply Conflict label
      const labelUpdates: Record<string, unknown> = {};
      if (ctx.gateLabels.conflict) {
        labelUpdates.addedLabelIds = [ctx.gateLabels.conflict];
      }

      const isLargePr = mergeability.changedFiles >= LARGE_PR_FILE_THRESHOLD;

      if (isLargePr) {
        // Large PR — park in Manual Review Required
        const manualReviewState = ctx.stateByName.get('Manual Review Required');
        if (manualReviewState) {
          await ctx.client.updateIssue(issue_id, {
            stateId: manualReviewState.id,
            ...labelUpdates,
          });
          await ctx.client.createComment({
            issueId: issue_id,
            body: `**Merge conflict detected** — PR ${pr_url} has conflicts with the base branch.\n\nThis PR touches ${mergeability.changedFiles} files (≥${LARGE_PR_FILE_THRESHOLD}), so it has been moved to **Manual Review Required** for a human to resolve.\n\nTo retry after resolving: rebase the branch and move back to **Ready for Agent**.`,
          });
          logger.info(
            {
              issueId: issue_id,
              prUrl: pr_url,
              changedFiles: mergeability.changedFiles,
            },
            'conflict-check: large PR conflict, moved to Manual Review Required',
          );
        } else {
          logger.warn(
            { issueId: issue_id },
            'conflict-check: Manual Review Required state not found, skipping routing',
          );
        }
      } else {
        // Small PR — route back to Ready for Agent with rebase context
        const readyState = ctx.stateByName.get('Ready for Agent');
        if (readyState) {
          await ctx.client.updateIssue(issue_id, {
            stateId: readyState.id,
            priority: 1,
            ...labelUpdates,
          });
          const rebasePrompt = buildConflictRebasePrompt(
            pr_url,
            mergeability.changedFiles,
            mergeability.baseRefName,
          );
          await ctx.client.createComment({
            issueId: issue_id,
            body: `**Merge conflict detected** — PR ${pr_url} has conflicts with \`${mergeability.baseRefName}\`.\n\nMoved back to **Ready for Agent** with rebase instructions.\n\n\`\`\`\n${rebasePrompt}\n\`\`\``,
          });
          logger.info(
            {
              issueId: issue_id,
              prUrl: pr_url,
              changedFiles: mergeability.changedFiles,
            },
            'conflict-check: small PR conflict, moved to Ready for Agent',
          );
        } else {
          logger.warn(
            { issueId: issue_id },
            'conflict-check: Ready for Agent state not found, skipping routing',
          );
        }
      }
    } catch (err) {
      logger.warn(
        { issueId: issue_id, prUrl: pr_url, err },
        'conflict-check: error processing PR, skipping',
      );
    }
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
