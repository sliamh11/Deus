import { createServer, Server } from 'http';
import { LinearWebhookClient } from '@linear/sdk/webhooks';
import type { EntityWebhookPayloadWithIssueData } from '@linear/sdk/webhooks';
import { logger } from './logger.js';
import {
  executeAgentRun,
  extractScopeBlock,
  getDispatcherHealth,
  withWatchdog,
} from './linear-dispatcher.js';
import type { LinearContext, GateLabels } from './linear-dispatcher.js';
import type { GateSpec } from './linear-gate-specs.js';
import type { RunContext } from './agent-runtimes/types.js';
import {
  insertWebhookEvent,
  updateWebhookEventStatus,
  getLastCompletedGateRun,
  logPipelineEvent,
  upsertGateComment,
  getGateCommentId,
  upsertIssueCache,
  softDeleteIssueCache,
  getPipelineEvents,
  upsertGateMeta,
  getGateMeta,
  incrementAttemptCount,
  appendReviseHistory,
  clearLiveness,
  computeEnrichmentHash,
  getLiveness,
  stampLiveness,
} from './db.js';
import { triggerAutoMerge } from './linear-auto-merge.js';
import { notifyPipelineStep } from './linear-notifications.js';
import { syncVaultPending } from './linear-vault-sync.js';
import { RetryableError, UserError, FatalError } from './errors/index.js';
import { WEBHOOK_MAX_RETRIES, WEBHOOK_BASE_DELAY_MS } from './config.js';

const DEFAULT_WEBHOOK_PORT = 3005;
const DEFAULT_GATE_WATCHDOG_MS = 30 * 60 * 1000;
const _parsedGateWatchdog = parseInt(process.env.GATE_WATCHDOG_MS || '', 10);
const GATE_WATCHDOG_MS =
  isNaN(_parsedGateWatchdog) || _parsedGateWatchdog < 1000
    ? DEFAULT_GATE_WATCHDOG_MS
    : _parsedGateWatchdog;
const LABEL_RETRY_MAX = 3;
const LABEL_RETRY_BASE_MS = 5_000;

function withGateWatchdog<T>(
  label: string,
  promise: Promise<T>,
  issueId: string,
  identifier: string,
): Promise<T> {
  return withWatchdog(
    `gate:${label}:${identifier}`,
    promise,
    GATE_WATCHDOG_MS,
    () => {
      logPipelineEvent(
        issueId,
        identifier,
        'gate_stalled',
        `gate=${label} threshold=${GATE_WATCHDOG_MS}ms`,
      );
      stampLiveness(`gate_stalled:${issueId}`, {
        gate: label,
        thresholdMs: GATE_WATCHDOG_MS,
      });
    },
  ).finally(() => clearLiveness(`gate_stalled:${issueId}`));
}
const WEBHOOK_MAX_DELAY_MS = 30_000;

// Map<issueId, timeout> for deferred auto-merge after genuine cooldowns.
// Volatile: does not survive restart (sweepStaleInReview covers that case).
const pendingCooldownRetries = new Map<string, ReturnType<typeof setTimeout>>();

// Async sleep — swapped out in tests for deterministic timing.
let sleepFn = (ms: number): Promise<void> =>
  new Promise((resolve) => setTimeout(resolve, ms));

/** Replace the sleep implementation (test-only). */
export function _setSleepFnForTests(fn: (ms: number) => Promise<void>): void {
  sleepFn = fn;
}

async function retryLabelUpdate(
  client: LinearContext['client'],
  issueId: string,
  update: Record<string, unknown>,
): Promise<void> {
  for (let i = 0; i < LABEL_RETRY_MAX; i++) {
    try {
      await client.updateIssue(issueId, update);
      return;
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      const isRetryable =
        msg.includes('rate limit') ||
        msg.includes('429') ||
        msg.includes('Too Many');
      if (!isRetryable || i === LABEL_RETRY_MAX - 1) {
        logger.warn(
          { issueId, err, attempt: i + 1 },
          'retryLabelUpdate: exhausted retries or non-retryable error',
        );
        return;
      }
      logger.info(
        { issueId, attempt: i + 1 },
        'retryLabelUpdate: retrying after rate limit',
      );
      await new Promise((r) =>
        setTimeout(r, LABEL_RETRY_BASE_MS * Math.pow(3, i)),
      );
    }
  }
}

export async function retryWithBackoff<T>(
  fn: () => Promise<T>,
  maxAttempts: number,
  baseDelayMs: number,
): Promise<T> {
  let lastErr: unknown;
  let firstErr: unknown;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (attempt === 0) firstErr = err;

      if (err instanceof UserError) {
        throw err;
      }

      if (isNonRetryableHttpError(err)) {
        throw new UserError(
          `Webhook dispatch failed with non-retryable HTTP error`,
          { cause: err },
        );
      }

      const isLastAttempt = attempt === maxAttempts - 1;
      if (isLastAttempt) {
        break;
      }

      const retryAfterMs = extractRetryAfterMs(err);
      const delayMs =
        retryAfterMs !== null
          ? Math.min(retryAfterMs, WEBHOOK_MAX_DELAY_MS)
          : Math.min(
              baseDelayMs * Math.pow(2, attempt) + Math.random() * baseDelayMs,
              WEBHOOK_MAX_DELAY_MS,
            );

      const errMsg = err instanceof Error ? err.message : String(err);
      logger.warn(
        {
          attempt: attempt + 1,
          maxAttempts,
          delayMs: Math.round(delayMs),
          error: errMsg,
          ...(retryAfterMs !== null && { retryAfterHeader: true }),
        },
        'webhook.retry',
      );

      await sleepFn(delayMs);
    }
  }

  const lastMsg = lastErr instanceof Error ? lastErr.message : String(lastErr);
  const firstMsg =
    firstErr instanceof Error ? firstErr.message : String(firstErr);
  logger.error(
    {
      attempts_exhausted: maxAttempts,
      first_error: firstMsg,
      error: lastMsg,
    },
    'webhook.failed',
  );
  throw new FatalError(
    `Webhook dispatch failed after ${maxAttempts} attempts`,
    {
      cause: lastErr,
    },
  );
}

function extractRetryAfterMs(err: unknown): number | null {
  if (!(err instanceof Error)) return null;
  const typed = err as Error & {
    headers?: Record<string, string>;
    response?: {
      headers?: Record<string, string> & { get?: (k: string) => string };
    };
  };
  const raw =
    typed.headers?.['retry-after'] ??
    typed.response?.headers?.['retry-after'] ??
    typed.response?.headers?.get?.('retry-after');
  if (!raw) return null;

  const seconds = Number(raw);
  if (!Number.isNaN(seconds) && seconds > 0) {
    return seconds * 1000;
  }
  const dateMs = Date.parse(raw);
  if (!Number.isNaN(dateMs)) {
    const delta = dateMs - Date.now();
    return delta > 0 ? delta : 0;
  }
  return null;
}

function isNonRetryableHttpError(err: unknown): boolean {
  if (err instanceof RetryableError) return false;
  if (!(err instanceof Error)) return false;

  const statusCode =
    (err as Error & { status?: number; statusCode?: number }).status ??
    (err as Error & { status?: number; statusCode?: number }).statusCode;

  if (typeof statusCode === 'number') {
    if (statusCode === 429) return false;
    if (statusCode >= 400 && statusCode < 500) return true;
  }

  return false;
}

let _syncTimer: ReturnType<typeof setTimeout> | null = null;

function debouncedVaultSync(ctx: LinearContext, vaultPath: string): void {
  if (_syncTimer) clearTimeout(_syncTimer);
  _syncTimer = setTimeout(async () => {
    _syncTimer = null;
    try {
      await syncVaultPending(ctx.client, ctx.teamId, vaultPath);
    } catch (err) {
      logger.debug({ err }, 'vault-sync: failed to sync pending block');
    }
  }, 2000);
}

export function parseVerdict(
  output: string,
): 'SHIP' | 'REVISE' | 'BLOCK' | null {
  const match = output.match(/^## Verdict:\s*(SHIP|REVISE|BLOCK)/m);
  return match ? (match[1] as 'SHIP' | 'REVISE' | 'BLOCK') : null;
}

export function parseEnrichment(output: string): string | null {
  const match = output.match(/^## Enrichment\s*\n([\s\S]*?)(?=^## Verdict)/m);
  return match ? match[1].trim() : null;
}

export function parseRatings(enrichment: string): {
  effort?: number;
  complexity?: number;
} {
  const effort = enrichment.match(/[-*]\s*Effort:\s*(\d)/);
  const complexity = enrichment.match(/[-*]\s*Complexity:\s*(\d)/);
  return {
    effort: effort ? parseInt(effort[1], 10) : undefined,
    complexity: complexity ? parseInt(complexity[1], 10) : undefined,
  };
}

export function mergeEnrichment(
  currentDesc: string,
  gateName: string,
  body: string,
): string {
  const start = `<!-- gate:${gateName}:start -->`;
  const end = `<!-- gate:${gateName}:end -->`;
  const block = `${start}\n${body}\n${end}`;
  const pattern = new RegExp(
    `${escapeRegex(start)}[\\s\\S]*?${escapeRegex(end)}`,
  );
  if (pattern.test(currentDesc)) {
    return currentDesc.replace(pattern, block);
  }
  return currentDesc ? `${currentDesc}\n\n${block}` : block;
}

export function stripEnrichmentSection(text: string): string {
  return text.replace(/^## Enrichment\s*\n[\s\S]*?(?=^## Verdict)/m, '').trim();
}

export function computeScopeLabelChanges(
  gateName: string,
  finalVerdict: string | undefined,
  finalEnrichment: string | undefined,
  gateLabels: GateLabels,
): { addIds: string[]; removeIds: string[] } {
  const addIds: string[] = [];
  const removeIds: string[] = [];
  if (gateName !== 'agent-readiness-gate' && gateName !== 'enrichment-gate')
    return { addIds, removeIds };
  if (finalVerdict === 'SHIP' && finalEnrichment && gateLabels.scoped) {
    addIds.push(gateLabels.scoped);
    if (gateLabels.revise) removeIds.push(gateLabels.revise);
  } else if (finalVerdict === 'REVISE' && gateLabels.revise) {
    addIds.push(gateLabels.revise);
    if (gateLabels.scoped) removeIds.push(gateLabels.scoped);
  }
  return { addIds, removeIds };
}

function escapeRegex(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function formatGateComment(
  gateName: string,
  verdict: string,
  output: string,
  mode: string,
): string {
  const lines = [
    `**Warden: ${gateName}** - ${verdict}`,
    '',
    output,
    '',
    '---',
    `*Gate: ${gateName} | Mode: ${mode} | ${new Date().toISOString()}*`,
  ];
  return lines.join('\n');
}

async function postOrUpdateComment(
  ctx: LinearContext,
  issueId: string,
  gateTo: string,
  body: string,
): Promise<void> {
  const existingCommentId = getGateCommentId(issueId, gateTo);

  if (existingCommentId) {
    try {
      await ctx.client.updateComment(existingCommentId, { body });
      upsertGateComment(issueId, gateTo, existingCommentId);
      return;
    } catch (err) {
      logger.warn(
        { issueId, commentId: existingCommentId, err },
        'linear-webhook: failed to update existing comment, creating new one',
      );
    }
  }

  const payload = await ctx.client.createComment({ issueId, body });
  const commentId = payload?.commentId;
  if (commentId) {
    upsertGateComment(issueId, gateTo, commentId);
  }
}

async function fetchIssueComments(
  ctx: LinearContext,
  issueId: string,
): Promise<Array<{ author: string; body: string }>> {
  try {
    const issue = await ctx.client.issue(issueId);
    const issueComments = await issue.comments();
    return Promise.all(
      issueComments.nodes.map(async (c) => {
        const user = await c.user;
        return { author: user?.displayName ?? 'Unknown', body: c.body };
      }),
    );
  } catch (err) {
    logger.warn({ issueId, err }, 'linear-webhook: failed to fetch comments');
    return [];
  }
}

export async function runInlineCompletionCheck(
  ctx: LinearContext,
  issueData: {
    id: string;
    identifier: string;
    title: string;
    description?: string | null;
    labels: Array<{ id: string; name: string }>;
  },
  gateSpecs: Map<string, GateSpec>,
): Promise<'SHIP' | 'REVISE'> {
  const completionGateSpec = gateSpecs.get('Done');
  if (!completionGateSpec) {
    logger.warn(
      { issueId: issueData.id },
      'inline-completion-check: gate spec for Done not found, blocking merge',
    );
    return 'REVISE';
  }

  ctx.inFlightGate.add(issueData.id);
  stampLiveness(`gate_in_flight:${issueData.id}`, {
    gate: 'completion-check',
    startedAt: new Date().toISOString(),
  });
  try {
    let commentBlock = '';
    const comments = await fetchIssueComments(ctx, issueData.id);
    if (comments.length > 0) {
      commentBlock =
        '\n\n<comments>\n' +
        comments.map((c) => `[${c.author}]: ${c.body}`).join('\n\n') +
        '\n</comments>';
    }

    const prompt =
      [
        `<gate-spec>\n${completionGateSpec.content}\n</gate-spec>`,
        `<invocation-context>pre-merge</invocation-context>`,
        `<issue>\nTitle: ${issueData.title}\nID: ${issueData.identifier}\n\n${issueData.description ?? '(no description)'}\n</issue>`,
        `<transition>\nFrom: In Review\nTo: Done (pre-merge check)\n</transition>`,
      ].join('\n\n') + commentBlock;

    const chatJid = `linear-gate-completion-pre-merge-${issueData.id.slice(0, 8)}`;
    const runContext: RunContext = {
      prompt,
      groupFolder: ctx.dispatchGroup.folder,
      chatJid,
      isControlGroup: false,
      isScheduledTask: true,
      effort: completionGateSpec.effort ?? 'medium',
    };

    const { text, error } = await withGateWatchdog(
      'completion-check',
      retryWithBackoff(
        () => executeAgentRun(ctx, runContext),
        WEBHOOK_MAX_RETRIES,
        WEBHOOK_BASE_DELAY_MS,
      ),
      issueData.id,
      issueData.identifier,
    );

    const output = text || error || '';
    const parsedVerdict = parseVerdict(output);
    const verdict: 'SHIP' | 'REVISE' =
      parsedVerdict === 'SHIP' ? 'SHIP' : 'REVISE';

    const commentBody = formatGateComment(
      completionGateSpec.name,
      verdict,
      parsedVerdict ? stripEnrichmentSection(output) : output,
      'pre-merge',
    );
    await postOrUpdateComment(ctx, issueData.id, 'Done:pre-merge', commentBody);

    logger.info(
      { issueId: issueData.id, verdict },
      'inline-completion-check: verdict',
    );
    return verdict;
  } catch (err) {
    logger.warn(
      { issueId: issueData.id, err },
      'inline-completion-check: failed, blocking merge',
    );
    return 'REVISE';
  } finally {
    ctx.inFlightGate.delete(issueData.id);
    clearLiveness(`gate_in_flight:${issueData.id}`);
  }
}

async function trackGateMetaAndEscalate(
  ctx: LinearContext,
  issueId: string,
  issueIdentifier: string,
  gateSpec: GateSpec,
  toStateName: string,
  finalVerdict: string,
  finalEnrichment: string,
): Promise<void> {
  if (finalVerdict === 'SHIP') {
    // Strip sentinel markers before hashing so it matches extractScopeBlock output
    const startMarker = `<!-- gate:${gateSpec.name}:start -->`;
    const endMarker = `<!-- gate:${gateSpec.name}:end -->`;
    const cleanedForHash = finalEnrichment
      .replace(new RegExp(escapeRegex(startMarker), 'g'), '')
      .replace(new RegExp(escapeRegex(endMarker), 'g'), '')
      .trim();
    const hash = computeEnrichmentHash(cleanedForHash);
    upsertGateMeta(issueId, gateSpec.name, {
      enrichmentHash: hash,
      enrichmentSnapshot: cleanedForHash,
      shippedAt: new Date().toISOString(),
      resetReviseHistory: true,
    });
    logger.info(
      { issueId, gate: gateSpec.name },
      'linear-webhook: stored enrichment hash + snapshot',
    );
  } else if (finalVerdict === 'REVISE') {
    const count = incrementAttemptCount(issueId, gateSpec.name);
    // Extract actionable gaps section; fall back to truncated enrichment
    const gapsMatch = finalEnrichment.match(
      /\*\*Gaps found\*\*:\s*([\s\S]*?)(?=\n##|\n\*\*|$)/,
    );
    const summary = gapsMatch
      ? gapsMatch[1].trim().slice(0, 300)
      : finalEnrichment.slice(0, 200) +
        (finalEnrichment.length > 200 ? '...' : '');
    appendReviseHistory(issueId, gateSpec.name, summary);
    logger.info(
      { issueId, gate: gateSpec.name, attemptCount: count },
      'linear-webhook: incremented attempt count + appended revise history',
    );

    if (gateSpec.maxAttempts && count >= gateSpec.maxAttempts) {
      const manualState = ctx.stateByName.get('Manual Review Required');
      if (manualState) {
        const meta = getGateMeta(issueId, gateSpec.name);
        const historyLines = (meta?.reviseHistory ?? [])
          .map((r, i) => `${i + 1}. ${r}`)
          .join('\n');
        const guideBody = [
          `**Warden: ${gateSpec.name}** - BLOCKED`,
          '',
          `This issue has been revised ${count} times without passing the ${gateSpec.name} gate. Moving to manual review.`,
          '',
          '### REVISE history',
          historyLines || '(no history recorded)',
          '',
          '### How to unblock',
          '1. Review the REVISE history above to understand what gaps remain',
          '2. Fix the issue description to address the specific gaps',
          '3. Remove the `warden:blocked` label',
          '4. Drag the issue back to **Todo** to re-enter enrichment',
          '',
          '---',
          `*Gate: ${gateSpec.name} | BLOCKED after ${count} attempts | ${new Date().toISOString()}*`,
        ].join('\n');
        await postOrUpdateComment(ctx, issueId, toStateName, guideBody);
        try {
          const blockLabelIds = ctx.gateLabels.blocked
            ? [ctx.gateLabels.blocked]
            : [];
          await ctx.client.updateIssue(issueId, {
            stateId: manualState.id,
            ...(blockLabelIds.length > 0
              ? { addedLabelIds: blockLabelIds }
              : {}),
          });
          logPipelineEvent(
            issueId,
            issueIdentifier,
            'gate_blocked',
            `${gateSpec.name}: BLOCKED after ${count} attempts`,
          );
        } catch (blockErr) {
          logger.warn(
            { issueId, err: blockErr },
            'linear-webhook: failed to move issue to Manual Review Required',
          );
        }
      }
    }
  }
}

async function handleIssueUpdate(
  payload: EntityWebhookPayloadWithIssueData,
  ctx: LinearContext,
  gateSpecs: Map<string, GateSpec>,
): Promise<void> {
  const { data, updatedFrom, action } = payload;

  if (action !== 'update') return;

  const webhookLagMs =
    Date.now() - new Date(payload.webhookTimestamp).getTime();
  stampLiveness('webhook_ingest', { lagMs: webhookLagMs });

  logger.info(
    { issueId: data.id, action, identifier: data.identifier, webhookLagMs },
    'linear-webhook: received event',
  );

  // SDK types updatedFrom as JSONObject; stateId is present at runtime for state changes
  const fromStateId = (updatedFrom as Record<string, unknown> | undefined)
    ?.stateId;
  if (typeof fromStateId !== 'string') return;

  const toStateId = data.stateId;
  const toState = ctx.stateById.get(toStateId);
  const fromState = ctx.stateById.get(fromStateId);
  if (!toState || !fromState) return;

  const actorId = payload.actor?.id;
  if (actorId && actorId === ctx.botUserId) {
    logger.info(
      { issueId: data.id, gate: toState.name, actorId },
      'linear-webhook: skipping bot-triggered transition',
    );
    return;
  }

  if (toState.name === 'Ready for Agent') {
    logPipelineEvent(
      data.id,
      data.identifier,
      'circuit_breaker_reset',
      'manual move to Ready for Agent',
    );
    // Auto-strip warden:skip on RfA entry so gates work normally on the next cycle
    if (
      ctx.gateLabels.wardenSkip &&
      data.labels.some((l) => l.name === 'warden:skip')
    ) {
      retryLabelUpdate(ctx.client, data.id, {
        removedLabelIds: [ctx.gateLabels.wardenSkip],
      });
      logger.info(
        { issueId: data.id },
        'linear-webhook: stripped warden:skip label on Ready for Agent entry',
      );
    }
    logger.info(
      { issueId: data.id },
      'linear-webhook: circuit breaker reset (manual Ready for Agent)',
    );
  }

  if (data.labels.some((l) => l.name === 'warden:skip')) {
    logger.info(
      { issueId: data.id },
      'linear-webhook: warden:skip label present, skipping',
    );
    return;
  }

  if (
    toState.name === 'Done' &&
    data.labels.some((l) => l.name === 'Done: Pre-implemented')
  ) {
    logger.info(
      { issueId: data.id },
      'linear-webhook: Done: Pre-implemented label present, skipping gate',
    );
    return;
  }

  const gateSpec = gateSpecs.get(toState.name);
  if (!gateSpec) return;

  const eventKey = `${data.id}:${fromStateId}:${toStateId}:${payload.webhookTimestamp}`;
  const inserted = insertWebhookEvent({
    event_key: eventKey,
    issue_id: data.id,
    gate_to: toState.name,
    from_state_id: fromStateId,
    to_state_id: toStateId,
    webhook_ts: new Date(payload.webhookTimestamp).toISOString(),
  });
  if (!inserted) {
    logger.debug({ eventKey }, 'linear-webhook: duplicate event, skipping');
    return;
  }

  if (
    gateSpec.allowedFrom.length > 0 &&
    !gateSpec.allowedFrom.includes(fromState.name)
  ) {
    const body = formatGateComment(
      gateSpec.name,
      'REVISE',
      `Illegal transition: **${fromState.name}** → **${toState.name}**.\n\nAllowed source states: ${gateSpec.allowedFrom.join(', ')}.`,
      gateSpec.mode,
    );
    await postOrUpdateComment(ctx, data.id, toState.name, body);

    try {
      await ctx.client.updateIssue(data.id, { stateId: fromStateId });
    } catch (err) {
      logger.warn(
        { issueId: data.id, err },
        'linear-webhook: failed to revert illegal transition',
      );
    }

    updateWebhookEventStatus(eventKey, 'done', { verdict: 'REVISE' });
    logger.info(
      { issueId: data.id, from: fromState.name, to: toState.name },
      'linear-webhook: reverted illegal transition',
    );
    return;
  }

  if (gateSpec.cooldownMinutes > 0) {
    const lastRun = getLastCompletedGateRun(data.id, toState.name);
    if (lastRun) {
      const elapsed =
        (Date.now() - new Date(lastRun.finished_at).getTime()) / 60_000;
      if (elapsed < gateSpec.cooldownMinutes) {
        // Bypass cooldown if agent produced new work since last gate run
        const agentEvents = getPipelineEvents({
          issueId: data.id,
          eventType: 'agent_completed',
        });
        const lastAgentDone = agentEvents.at(-1);
        if (
          lastAgentDone &&
          new Date(lastAgentDone.created_at) > new Date(lastRun.finished_at)
        ) {
          logger.info(
            { issueId: data.id, gate: gateSpec.name },
            'linear-webhook: bypassing cooldown, new agent work detected',
          );
          // New agent work post-last-gate means the previous verdict is stale
        } else {
          const remainMin = Math.ceil(gateSpec.cooldownMinutes - elapsed);
          const resetAt = new Date(
            new Date(lastRun.finished_at).getTime() +
              gateSpec.cooldownMinutes * 60_000,
          );
          const resetTime = resetAt.toISOString().slice(11, 16);
          notifyPipelineStep(
            ctx,
            data.id,
            data.identifier,
            'gate_cooldown',
            `${gateSpec.name}: ${remainMin}min remaining, resets at ${resetTime}`,
          ).catch(() => {});
          logger.info(
            {
              issueId: data.id,
              gate: gateSpec.name,
              elapsedMin: Math.round(elapsed),
              cooldownMin: gateSpec.cooldownMinutes,
            },
            'linear-webhook: within cooldown, skipping',
          );
          updateWebhookEventStatus(eventKey, 'done', {
            verdict: lastRun.verdict,
          });

          // Schedule deferred auto-merge after cooldown expires
          const retryDelayMs =
            (gateSpec.cooldownMinutes - elapsed) * 60_000 + 5_000;
          if (!pendingCooldownRetries.has(data.id)) {
            const issueId = data.id;
            const identifier = data.identifier;
            const targetStateName = toState.name;
            const timeout = setTimeout(async () => {
              pendingCooldownRetries.delete(issueId);
              try {
                const issue = await ctx.client.issue(issueId);
                const currentState = await issue.state;
                if (currentState?.name === targetStateName) {
                  const labels = await issue.labels();
                  const issueData = {
                    id: issue.id,
                    identifier: issue.identifier,
                    title: issue.title,
                    description: issue.description,
                    labels: labels.nodes.map((l) => ({
                      id: l.id,
                      name: l.name,
                    })),
                  };
                  const cgVerdict = await runInlineCompletionCheck(
                    ctx,
                    issueData,
                    gateSpecs,
                  );
                  if (cgVerdict === 'SHIP') {
                    await triggerAutoMerge(ctx, issueId, identifier);
                  } else {
                    logger.info(
                      { issueId },
                      'linear-webhook: deferred completion-gate REVISE, auto-merge blocked',
                    );
                  }
                }
              } catch (retryErr) {
                logger.warn(
                  { issueId, err: retryErr },
                  'linear-webhook: deferred auto-merge after cooldown failed',
                );
              }
            }, retryDelayMs);
            pendingCooldownRetries.set(issueId, timeout);
            logger.info(
              { issueId: data.id, delayMs: retryDelayMs },
              'linear-webhook: scheduled deferred auto-merge after cooldown',
            );
          }

          return;
        }
      }
    }
  }

  if (ctx.inFlightGate.has(data.id)) {
    logger.info(
      { issueId: data.id },
      'linear-webhook: gate already in flight for issue',
    );
    return;
  }
  ctx.inFlightGate.add(data.id);
  stampLiveness(`gate_in_flight:${data.id}`, {
    gate: gateSpec.name,
    startedAt: new Date().toISOString(),
  });

  // --- Bouncer entry-path router: short-circuit on fresh enrichment hash ---
  if (gateSpec.name === 'bouncer-gate') {
    const enrichmentMeta = getGateMeta(data.id, 'enrichment-gate');
    if (enrichmentMeta?.enrichmentHash && enrichmentMeta.shippedAt) {
      const ageHours =
        (Date.now() - new Date(enrichmentMeta.shippedAt).getTime()) / 3_600_000;
      // 72h: beyond a typical sprint's scope-change window
      if (ageHours < 72) {
        const currentScope = extractScopeBlock(
          data.description ?? '',
          'enrichment-gate',
        );
        const currentHash = computeEnrichmentHash(currentScope);
        if (currentHash === enrichmentMeta.enrichmentHash) {
          // Path A: hash matches, enrichment is fresh -- SHIP without LLM
          const body = formatGateComment(
            gateSpec.name,
            'SHIP',
            'Enrichment hash valid and fresh -- fast-path approval.',
            gateSpec.mode,
          );
          await postOrUpdateComment(ctx, data.id, toState.name, body);
          updateWebhookEventStatus(eventKey, 'done', { verdict: 'SHIP' });
          notifyPipelineStep(
            ctx,
            data.id,
            data.identifier,
            'gate_ship',
            `${gateSpec.name}: SHIP (hash fast-path)`,
          ).catch(() => {});
          logger.info(
            {
              issueId: data.id,
              gate: gateSpec.name,
              ageHours: Math.round(ageHours),
            },
            'linear-webhook: bouncer fast-path SHIP (hash match)',
          );
          ctx.inFlightGate.delete(data.id);
          clearLiveness(`gate_in_flight:${data.id}`);
          return;
        }
      }
    }
  }

  // Strip bounced labels on RfA entry so they don't persist after re-validation
  if (toState.name === 'Ready for Agent') {
    const bouncedLabelIds = [
      ctx.gateLabels.bouncedUnscoped,
      ctx.gateLabels.bouncedStale,
      ctx.gateLabels.bouncedNoContext,
    ].filter((id): id is string => !!id);
    if (bouncedLabelIds.length > 0) {
      const issueBouncedIds = bouncedLabelIds.filter((id) =>
        data.labels.some((l) => l.id === id),
      );
      if (issueBouncedIds.length > 0) {
        retryLabelUpdate(ctx.client, data.id, {
          removedLabelIds: issueBouncedIds,
        });
      }
    }
  }

  updateWebhookEventStatus(eventKey, 'running');

  // Visual feedback: add evaluating label, remove any prior error label
  {
    const startAdded: string[] = [];
    const startRemoved: string[] = [];
    if (ctx.gateLabels.evaluating) startAdded.push(ctx.gateLabels.evaluating);
    if (ctx.gateLabels.error) startRemoved.push(ctx.gateLabels.error);
    if (startAdded.length > 0 || startRemoved.length > 0) {
      const startUpdate: Record<string, string[]> = {};
      if (startAdded.length > 0) startUpdate.addedLabelIds = startAdded;
      if (startRemoved.length > 0) startUpdate.removedLabelIds = startRemoved;
      ctx.client.updateIssue(data.id, startUpdate).catch(() => {});
    }
  }
  const runningComment = formatGateComment(
    gateSpec.name,
    'RUNNING',
    `Evaluating transition **${fromState.name}** → **${toState.name}**...`,
    gateSpec.mode,
  );
  await postOrUpdateComment(ctx, data.id, toState.name, runningComment);

  let finalVerdict: string | undefined;
  let finalEnrichment: string | undefined;
  let gateDidError = false;
  try {
    const chatJid = `linear-gate-${gateSpec.name}-${data.id.slice(0, 8)}`;

    let commentBlock = '';
    if (gateSpec.fetchComments) {
      const comments = await fetchIssueComments(ctx, data.id);
      if (comments.length > 0) {
        commentBlock =
          '\n\n<comments>\n' +
          comments.map((c) => `[${c.author}]: ${c.body}`).join('\n\n') +
          '\n</comments>';
      }
    }

    // Path C: inject prior feedback for post-REVISE re-entry
    let priorFeedbackBlock = '';
    if (gateSpec.name === 'bouncer-gate') {
      const meta = getGateMeta(data.id, 'bouncer-gate');
      if (meta && meta.attemptCount > 0 && meta.reviseHistory.length > 0) {
        priorFeedbackBlock =
          '\n\n<prior-feedback>\nThis issue was previously bounced. Prior REVISE reasons:\n' +
          meta.reviseHistory.map((r, i) => `${i + 1}. ${r}`).join('\n') +
          '\n\nVerify these gaps have been addressed.\n</prior-feedback>';
      }
    }

    const prompt =
      [
        `<gate-spec>\n${gateSpec.content}\n</gate-spec>`,
        `<issue>\nTitle: ${data.title}\nID: ${data.identifier}\n\n${data.description ?? '(no description)'}\n</issue>`,
        `<transition>\nFrom: ${fromState.name}\nTo: ${toState.name}\n</transition>`,
      ].join('\n\n') +
      commentBlock +
      priorFeedbackBlock;

    const runContext: RunContext = {
      prompt,
      groupFolder: ctx.dispatchGroup.folder,
      chatJid,
      isControlGroup: false,
      isScheduledTask: true,
      effort: gateSpec.effort ?? 'medium',
    };

    const { text, error } = await withGateWatchdog(
      gateSpec.name,
      retryWithBackoff(
        () => executeAgentRun(ctx, runContext),
        WEBHOOK_MAX_RETRIES,
        WEBHOOK_BASE_DELAY_MS,
      ),
      data.id,
      data.identifier,
    );

    // Container may exit non-zero (e.g., docker kill) but still have output in either field
    const output = text || error || '';
    const parsedVerdict = parseVerdict(output);

    let verdict: 'SHIP' | 'REVISE' | 'BLOCK';
    let commentBody: string;

    if (!parsedVerdict && error) {
      verdict = gateSpec.fallback;
      logger.warn(
        {
          issueId: data.id,
          gate: gateSpec.name,
          fallback: gateSpec.fallback,
          error,
        },
        'linear-webhook: gate agent error, applying fallback verdict',
      );
      commentBody = formatGateComment(
        gateSpec.name,
        verdict,
        `Gate agent error (fallback: ${gateSpec.fallback}):\n\`\`\`\n${error}\n\`\`\``,
        gateSpec.mode,
      );
    } else {
      verdict = parsedVerdict ?? gateSpec.fallback;
      let enrichmentBody = parseEnrichment(output);

      if (!enrichmentBody && !parsedVerdict && output.length > 100) {
        logger.warn(
          {
            issueId: data.id,
            gate: gateSpec.name,
            outputLen: output.length,
          },
          'linear-webhook: agent output missing ## Enrichment/## Verdict markers, using full output as enrichment',
        );
        enrichmentBody = output;
      }

      finalEnrichment = enrichmentBody ?? undefined;
      const verdictText = stripEnrichmentSection(output);
      commentBody = formatGateComment(
        gateSpec.name,
        verdict,
        verdictText,
        gateSpec.mode,
      );

      if (enrichmentBody) {
        // Strip markers if the agent included them in the output
        const startMarker = `<!-- gate:${gateSpec.name}:start -->`;
        const endMarker = `<!-- gate:${gateSpec.name}:end -->`;
        const cleanedBody = enrichmentBody
          .replace(new RegExp(escapeRegex(startMarker), 'g'), '')
          .replace(new RegExp(escapeRegex(endMarker), 'g'), '')
          .trim();
        const currentDesc = data.description ?? '';
        const newDesc = mergeEnrichment(
          currentDesc,
          gateSpec.name,
          cleanedBody,
        );
        try {
          await ctx.client.updateIssue(data.id, { description: newDesc });
          logger.info(
            { issueId: data.id, gate: gateSpec.name },
            'linear-webhook: enriched issue description',
          );
        } catch (err) {
          logger.warn(
            { issueId: data.id, err },
            'linear-webhook: failed to update issue description',
          );
        }
      }
    }

    await postOrUpdateComment(ctx, data.id, toState.name, commentBody);

    const effectiveMode = data.labels.some((l) => l.name === 'warden:strict')
      ? 'strict'
      : gateSpec.mode;

    if (verdict !== 'SHIP') {
      try {
        let revertStateId = fromStateId;
        let revertStateName = fromState.name;
        if (gateSpec.revertTo) {
          const revertState = ctx.stateByName.get(gateSpec.revertTo);
          if (revertState) {
            revertStateId = revertState.id;
            revertStateName = revertState.name;
          } else {
            logger.warn(
              {
                issueId: data.id,
                gate: gateSpec.name,
                revertTo: gateSpec.revertTo,
              },
              'linear-webhook: revert_to state not found, falling back to fromState',
            );
          }
        }
        await ctx.client.updateIssue(data.id, {
          stateId: revertStateId,
          ...(effectiveMode === 'strict' ? { priority: 1 } : {}),
        });
        logger.info(
          {
            issueId: data.id,
            gate: gateSpec.name,
            verdict,
            revertTo: revertStateName,
            mode: effectiveMode,
          },
          'linear-webhook: reverted transition on REVISE',
        );
      } catch (err) {
        logger.warn(
          { issueId: data.id, err },
          'linear-webhook: failed to revert transition',
        );
      }
    }

    finalVerdict = verdict;
    updateWebhookEventStatus(eventKey, 'done', { verdict });
    const eventType = verdict === 'SHIP' ? 'gate_ship' : 'gate_revise';
    notifyPipelineStep(
      ctx,
      data.id,
      data.identifier,
      eventType,
      `${gateSpec.name}: ${verdict}`,
    ).catch(() => {});
    logger.info(
      { issueId: data.id, gate: gateSpec.name, verdict, mode: effectiveMode },
      'linear-webhook: gate evaluation complete',
    );
  } catch (err) {
    gateDidError = true;
    const errorMsg = err instanceof Error ? err.message : String(err);
    updateWebhookEventStatus(eventKey, 'error', { error: errorMsg });
    notifyPipelineStep(
      ctx,
      data.id,
      data.identifier,
      'gate_error',
      `${gateSpec.name}: ${errorMsg}`,
    ).catch(() => {});
    logger.error(
      { issueId: data.id, gate: gateSpec.name, err },
      'linear-webhook: gate evaluation failed',
    );

    // Apply fallback verdict on infrastructure errors (matching agent-error path)
    finalVerdict = gateSpec.fallback;
    const errorComment = formatGateComment(
      gateSpec.name,
      finalVerdict,
      `Gate infrastructure error (fallback: ${gateSpec.fallback}):\n\`\`\`\n${errorMsg}\n\`\`\``,
      gateSpec.mode,
    );
    postOrUpdateComment(ctx, data.id, toState.name, errorComment).catch(
      () => {},
    );
  } finally {
    ctx.inFlightGate.delete(data.id);
    clearLiveness(`gate_in_flight:${data.id}`);
    const removeIds: string[] = [];
    const addIds: string[] = [];
    if (ctx.gateLabels.evaluating) removeIds.push(ctx.gateLabels.evaluating);
    if (gateDidError && ctx.gateLabels.error) addIds.push(ctx.gateLabels.error);
    const scopeLabels = computeScopeLabelChanges(
      gateSpec.name,
      finalVerdict,
      finalEnrichment,
      ctx.gateLabels,
    );
    addIds.push(...scopeLabels.addIds);
    removeIds.push(...scopeLabels.removeIds);

    // Bouncer: apply bounced:<reason> label on REVISE, strip on SHIP
    if (gateSpec.name === 'bouncer-gate') {
      const allBouncedIds = [
        ctx.gateLabels.bouncedUnscoped,
        ctx.gateLabels.bouncedStale,
        ctx.gateLabels.bouncedNoContext,
      ].filter((id): id is string => !!id);
      if (finalVerdict === 'REVISE') {
        // Always 'unscoped' until bouncer output parsing is wired
        const bouncedId = ctx.gateLabels.bouncedUnscoped;
        if (bouncedId) addIds.push(bouncedId);
      } else if (finalVerdict === 'SHIP') {
        // Strip all bounced labels on SHIP
        removeIds.push(...allBouncedIds);
      }
    }

    // Apply effort/complexity labels only when ratings are actually present
    if (finalEnrichment) {
      const ratings = parseRatings(finalEnrichment);
      if (ratings.effort || ratings.complexity) {
        for (const id of Object.values(ctx.gateLabels.effort))
          removeIds.push(id);
        for (const id of Object.values(ctx.gateLabels.complexity))
          removeIds.push(id);
        if (ratings.effort && ctx.gateLabels.effort[ratings.effort]) {
          addIds.push(ctx.gateLabels.effort[ratings.effort]);
        }
        if (
          ratings.complexity &&
          ctx.gateLabels.complexity[ratings.complexity]
        ) {
          addIds.push(ctx.gateLabels.complexity[ratings.complexity]);
        }
      }
    }
    const issueLabels = new Set(data.labels.map((l) => l.id));
    if (ctx.gateLabels.evaluating) issueLabels.add(ctx.gateLabels.evaluating);
    const safeRemoveIds = removeIds.filter((id) => issueLabels.has(id));
    if (safeRemoveIds.length > 0 || addIds.length > 0) {
      const update: Record<string, unknown> = {};
      if (safeRemoveIds.length > 0) update.removedLabelIds = safeRemoveIds;
      if (addIds.length > 0) update.addedLabelIds = addIds;
      retryLabelUpdate(ctx.client, data.id, update);
    }

    if (finalVerdict && finalEnrichment && !gateDidError) {
      try {
        await trackGateMetaAndEscalate(
          ctx,
          data.id,
          data.identifier,
          gateSpec,
          toState.name,
          finalVerdict,
          finalEnrichment,
        );
      } catch (metaErr) {
        logger.warn(
          { issueId: data.id, gate: gateSpec.name, err: metaErr },
          'linear-webhook: failed to update gate meta',
        );
      }
    }

    if (finalVerdict === 'SHIP' && gateSpec.name === 'output-quality-gate') {
      void (async () => {
        try {
          const cgVerdict = await runInlineCompletionCheck(
            ctx,
            data,
            gateSpecs,
          );
          if (cgVerdict === 'SHIP') {
            await triggerAutoMerge(ctx, data.id, data.identifier);
          } else {
            logger.info(
              { issueId: data.id },
              'linear-webhook: inline completion-gate REVISE, auto-merge blocked',
            );
          }
        } catch (err) {
          logger.warn(
            { issueId: data.id, err },
            'linear-webhook: inline completion check failed, merge blocked',
          );
        }
      })();
    }
  }
}

async function runGateForIssue(
  ctx: LinearContext,
  issue: {
    id: string;
    identifier: string;
    title: string;
    description?: string | null;
  },
  labels: Array<{ id: string; name: string }>,
  gateSpec: GateSpec,
  stateName: string,
): Promise<void> {
  ctx.inFlightGate.add(issue.id);
  stampLiveness(`gate_in_flight:${issue.id}`, {
    gate: gateSpec.name,
    startedAt: new Date().toISOString(),
  });
  let finalVerdict: string | undefined;
  let finalEnrichment: string | undefined;

  try {
    if (ctx.gateLabels.evaluating) {
      ctx.client
        .updateIssue(issue.id, { addedLabelIds: [ctx.gateLabels.evaluating] })
        .catch(() => {});
    }

    const runningComment = formatGateComment(
      gateSpec.name,
      'RUNNING',
      'Evaluating (startup sweep)...',
      gateSpec.mode,
    );
    await postOrUpdateComment(ctx, issue.id, stateName, runningComment);

    let commentBlock = '';
    if (gateSpec.fetchComments) {
      const comments = await fetchIssueComments(ctx, issue.id);
      if (comments.length > 0) {
        commentBlock =
          '\n\n<comments>\n' +
          comments.map((c) => `[${c.author}]: ${c.body}`).join('\n\n') +
          '\n</comments>';
      }
    }

    const prompt =
      [
        `<gate-spec>\n${gateSpec.content}\n</gate-spec>`,
        `<invocation-context>startup-sweep</invocation-context>`,
        `<issue>\nTitle: ${issue.title}\nID: ${issue.identifier}\n\n${issue.description ?? '(no description)'}\n</issue>`,
        `<transition>\nFrom: (startup scan)\nTo: ${stateName}\n</transition>`,
      ].join('\n\n') + commentBlock;

    const chatJid = `linear-gate-sweep-${gateSpec.name}-${issue.id.slice(0, 8)}`;
    const runContext: RunContext = {
      prompt,
      groupFolder: ctx.dispatchGroup.folder,
      chatJid,
      isControlGroup: false,
      isScheduledTask: true,
      effort: gateSpec.effort ?? 'medium',
    };

    const { text, error } = await withGateWatchdog(
      gateSpec.name,
      retryWithBackoff(
        () => executeAgentRun(ctx, runContext),
        WEBHOOK_MAX_RETRIES,
        WEBHOOK_BASE_DELAY_MS,
      ),
      issue.id,
      issue.identifier,
    );

    const output = text || error || '';
    const parsedVerdict = parseVerdict(output);
    let verdict: 'SHIP' | 'REVISE' | 'BLOCK';
    let commentBody: string;

    if (!parsedVerdict && error) {
      // Agent returned error without a verdict — gate didn't run successfully
      verdict = 'REVISE';
      commentBody = formatGateComment(
        gateSpec.name,
        'ERROR',
        `Gate agent error (startup sweep):\n\`\`\`\n${error}\n\`\`\``,
        gateSpec.mode,
      );
    } else {
      verdict = parsedVerdict ?? 'REVISE';
      const enrichmentBody = parseEnrichment(output);
      finalEnrichment = enrichmentBody ?? undefined;
      const verdictText = stripEnrichmentSection(output);
      commentBody = formatGateComment(
        gateSpec.name,
        verdict,
        verdictText,
        gateSpec.mode,
      );

      if (enrichmentBody) {
        const startMarker = `<!-- gate:${gateSpec.name}:start -->`;
        const endMarker = `<!-- gate:${gateSpec.name}:end -->`;
        const cleanedBody = enrichmentBody
          .replace(new RegExp(escapeRegex(startMarker), 'g'), '')
          .replace(new RegExp(escapeRegex(endMarker), 'g'), '')
          .trim();
        const currentDesc = issue.description ?? '';
        const newDesc = mergeEnrichment(
          currentDesc,
          gateSpec.name,
          cleanedBody,
        );
        ctx.client
          .updateIssue(issue.id, { description: newDesc })
          .catch(() => {});
      }
    }

    await postOrUpdateComment(ctx, issue.id, stateName, commentBody);

    const effectiveMode = labels.some((l) => l.name === 'warden:strict')
      ? 'strict'
      : gateSpec.mode;

    if (verdict !== 'SHIP') {
      if (gateSpec.revertTo) {
        const revertState = ctx.stateByName.get(gateSpec.revertTo);
        if (revertState) {
          await ctx.client.updateIssue(issue.id, {
            stateId: revertState.id,
            ...(effectiveMode === 'strict' ? { priority: 1 } : {}),
          });
        }
      } else {
        logger.warn(
          { issueId: issue.id, gate: gateSpec.name },
          'startup-sweep: revert skipped — no revert_to configured',
        );
      }
    }

    finalVerdict = verdict;
    const eventType = verdict === 'SHIP' ? 'gate_ship' : 'gate_revise';
    notifyPipelineStep(
      ctx,
      issue.id,
      issue.identifier,
      eventType,
      `${gateSpec.name}: ${verdict} (startup sweep)`,
    ).catch(() => {});
    logPipelineEvent(
      issue.id,
      issue.identifier,
      eventType,
      `${gateSpec.name}: ${verdict} (startup sweep)`,
    );
    logger.info(
      { issueId: issue.id, gate: gateSpec.name, verdict, mode: effectiveMode },
      'startup-sweep: gate evaluation complete',
    );
  } catch (err) {
    const errorMsg = err instanceof Error ? err.message : String(err);
    const errorComment = formatGateComment(
      gateSpec.name,
      'ERROR',
      `Gate infrastructure error (startup sweep):\n\`\`\`\n${errorMsg}\n\`\`\``,
      gateSpec.mode,
    );
    postOrUpdateComment(ctx, issue.id, stateName, errorComment).catch(() => {});
    notifyPipelineStep(
      ctx,
      issue.id,
      issue.identifier,
      'gate_error',
      `${gateSpec.name}: ${errorMsg} (startup sweep)`,
    ).catch(() => {});
    logger.error(
      { issueId: issue.id, gate: gateSpec.name, err },
      'startup-sweep: gate evaluation failed',
    );
    // Never fallback-SHIP on infrastructure errors — gate didn't actually run
    finalVerdict = 'REVISE';
  } finally {
    ctx.inFlightGate.delete(issue.id);
    clearLiveness(`gate_in_flight:${issue.id}`);

    const removeIds: string[] = [];
    const addIds: string[] = [];
    if (ctx.gateLabels.evaluating) removeIds.push(ctx.gateLabels.evaluating);
    const scopeLabels = computeScopeLabelChanges(
      gateSpec.name,
      finalVerdict,
      finalEnrichment,
      ctx.gateLabels,
    );
    addIds.push(...scopeLabels.addIds);
    removeIds.push(...scopeLabels.removeIds);
    if (finalEnrichment) {
      const ratings = parseRatings(finalEnrichment);
      if (ratings.effort || ratings.complexity) {
        for (const id of Object.values(ctx.gateLabels.effort))
          removeIds.push(id);
        for (const id of Object.values(ctx.gateLabels.complexity))
          removeIds.push(id);
        if (ratings.effort && ctx.gateLabels.effort[ratings.effort]) {
          addIds.push(ctx.gateLabels.effort[ratings.effort]);
        }
        if (
          ratings.complexity &&
          ctx.gateLabels.complexity[ratings.complexity]
        ) {
          addIds.push(ctx.gateLabels.complexity[ratings.complexity]);
        }
      }
    }
    const issueLabels = new Set(labels.map((l) => l.id));
    // Include evaluating in the set so safeRemoveIds always cleans it up
    if (ctx.gateLabels.evaluating) issueLabels.add(ctx.gateLabels.evaluating);
    const safeRemoveIds = removeIds.filter((id) => issueLabels.has(id));
    if (safeRemoveIds.length > 0 || addIds.length > 0) {
      const update: Record<string, unknown> = {};
      if (safeRemoveIds.length > 0) update.removedLabelIds = safeRemoveIds;
      if (addIds.length > 0) update.addedLabelIds = addIds;
      retryLabelUpdate(ctx.client, issue.id, update);
    }

    if (finalVerdict && finalEnrichment) {
      try {
        await trackGateMetaAndEscalate(
          ctx,
          issue.id,
          issue.identifier,
          gateSpec,
          stateName,
          finalVerdict,
          finalEnrichment,
        );
      } catch (metaErr) {
        logger.warn(
          { issueId: issue.id, gate: gateSpec.name, err: metaErr },
          'startup-sweep: failed to update gate meta',
        );
      }
    }
  }
}

export async function sweepStaleGatedIssues(
  ctx: LinearContext,
  gateSpecs: Map<string, GateSpec>,
): Promise<void> {
  const sweepableStates = ['Todo', 'Ready for Agent'];

  for (const stateName of sweepableStates) {
    const gateSpec = gateSpecs.get(stateName);
    const state = ctx.stateByName.get(stateName);
    if (!gateSpec || !state) continue;

    let issues;
    try {
      issues = await ctx.client.issues({
        filter: { state: { id: { eq: state.id } } },
      });
    } catch (err) {
      logger.warn(
        { err, state: stateName },
        'startup-sweep: failed to query issues',
      );
      continue;
    }

    let triggered = 0;
    for (const issue of issues.nodes) {
      const labels = await issue.labels();
      if (labels.nodes.some((l) => l.name === 'warden:skip')) continue;
      if (ctx.inFlightGate.has(issue.id)) continue;
      if (labels.nodes.some((l) => l.name === 'Scoped')) continue;

      const lastRun = getLastCompletedGateRun(issue.id, stateName);
      if (lastRun) {
        const elapsed =
          (Date.now() - new Date(lastRun.finished_at).getTime()) / 60_000;
        if (elapsed < gateSpec.cooldownMinutes) continue;
      }

      runGateForIssue(
        ctx,
        {
          id: issue.id,
          identifier: issue.identifier,
          title: issue.title,
          description: issue.description,
        },
        labels.nodes.map((l) => ({ id: l.id, name: l.name })),
        gateSpec,
        stateName,
      ).catch((err) => {
        logger.error({ issueId: issue.id, err }, 'startup-sweep: gate failed');
      });
      triggered++;
    }

    if (triggered > 0) {
      logger.info(
        { state: stateName, triggered, total: issues.nodes.length },
        'startup-sweep: fired gates for stale issues',
      );
    } else {
      logger.debug(
        { state: stateName, total: issues.nodes.length },
        'startup-sweep: no stale issues found',
      );
    }
  }
}

export function startLinearWebhookServer(
  ctx: LinearContext,
  gateSpecs: Map<string, GateSpec>,
): Promise<Server> {
  const secret = process.env.LINEAR_WEBHOOK_SECRET;
  if (!secret) {
    throw new Error('LINEAR_WEBHOOK_SECRET is required for webhook server');
  }

  const port = parseInt(
    process.env.LINEAR_WEBHOOK_PORT || String(DEFAULT_WEBHOOK_PORT),
    10,
  );
  const webhookPort = isNaN(port) ? DEFAULT_WEBHOOK_PORT : port;

  const webhookClient = new LinearWebhookClient(secret);
  const handler = webhookClient.createHandler();

  handler.on('Issue', (payload) => {
    const raw = payload as EntityWebhookPayloadWithIssueData;
    const d = raw.data;

    if (raw.action === 'remove') {
      softDeleteIssueCache(d.id);
    } else if (d.state?.name) {
      upsertIssueCache({
        issue_id: d.id,
        identifier: d.identifier,
        title: d.title,
        state_name: d.state.name,
        team_id: d.teamId,
        priority: d.priority,
        created_at: d.createdAt,
        updated_at: d.updatedAt,
      });
    }

    if (ctx.vaultPath) {
      debouncedVaultSync(ctx, ctx.vaultPath);
    }

    handleIssueUpdate(raw, ctx, gateSpecs).catch((err) => {
      logger.error({ err }, 'linear-webhook: unhandled error in issue handler');
    });
  });

  return new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      if (req.method === 'GET' && req.url === '/health') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ status: 'ok', gates: [...gateSpecs.keys()] }));
        return;
      }

      if (req.method === 'GET' && req.url === '/health/live') {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ alive: true }));
        return;
      }

      if (req.method === 'GET' && req.url === '/health/ready') {
        const dispatcherHealth = getDispatcherHealth();
        const liveness = getLiveness();
        const now = Date.now();

        const dispatcherLag = dispatcherHealth
          ? now - dispatcherHealth.lastTickAt
          : Infinity;
        const effectivePollMs = dispatcherHealth?.pollMs ?? 30_000;

        const safeParseDetail = (
          raw: string | null,
        ): Record<string, unknown> => {
          if (!raw) return {};
          try {
            return JSON.parse(raw) as Record<string, unknown>;
          } catch {
            return {};
          }
        };

        const inFlight = liveness
          .filter((r) => r.subsystem.includes('_in_flight:'))
          .map((r) => {
            const detail = safeParseDetail(r.detail);
            const startedAt = (detail.startedAt as string) ?? r.last_seen_at;
            return {
              subsystem: r.subsystem,
              startedAt,
              elapsedMs: now - new Date(startedAt).getTime(),
            };
          });

        const webhookRow = liveness.find(
          (r) => r.subsystem === 'webhook_ingest',
        );
        const webhookDetail = safeParseDetail(webhookRow?.detail ?? null);

        const reasons: string[] = [];
        const maxInFlightMs = inFlight.reduce(
          (max, f) => Math.max(max, f.elapsedMs),
          0,
        );

        let status: 'ok' | 'degraded' | 'stalled' = 'ok';
        if (
          dispatcherLag > 5 * effectivePollMs ||
          maxInFlightMs > 120 * 60_000
        ) {
          status = 'stalled';
          if (dispatcherLag > 5 * effectivePollMs)
            reasons.push(
              `dispatcher lag ${Math.round(dispatcherLag / 1000)}s > 5× poll`,
            );
          if (maxInFlightMs > 120 * 60_000)
            reasons.push(
              `in-flight entry stalled ${Math.round(maxInFlightMs / 60_000)}min`,
            );
        } else if (
          dispatcherLag > 2 * effectivePollMs ||
          maxInFlightMs > 60 * 60_000
        ) {
          status = 'degraded';
          if (dispatcherLag > 2 * effectivePollMs)
            reasons.push(
              `dispatcher lag ${Math.round(dispatcherLag / 1000)}s > 2× poll`,
            );
          if (maxInFlightMs > 60 * 60_000)
            reasons.push(
              `in-flight entry ${Math.round(maxInFlightMs / 60_000)}min`,
            );
        }

        const body = {
          status,
          dispatcher: {
            lastTickAt: dispatcherHealth?.lastTickAt ?? null,
            lagMs: dispatcherHealth ? Math.round(dispatcherLag) : null,
            pollMs: effectivePollMs,
            inFlightCount: ctx.inFlightDispatch.size,
          },
          webhook: {
            lastIngestAt: webhookRow?.last_seen_at ?? null,
            recentLagMs: webhookDetail?.lagMs ?? null,
          },
          inFlight,
          gates: [...gateSpecs.keys()],
          reasons,
        };

        const statusCode = status === 'ok' ? 200 : 503;
        res.writeHead(statusCode, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify(body));
        return;
      }

      handler(req, res).catch((err) => {
        logger.error({ err }, 'linear-webhook: handler error');
        if (!res.headersSent) {
          res.writeHead(500);
          res.end('Internal Server Error');
        }
      });
    });

    server.on('error', (err) => {
      logger.error(
        { err, port: webhookPort },
        'linear-webhook: server bind failed',
      );
      reject(err);
    });

    server.listen(webhookPort, '0.0.0.0', () => {
      logger.info(
        { port: webhookPort, gates: [...gateSpecs.keys()] },
        'linear-webhook: server started',
      );
      resolve(server);
    });
  });
}
