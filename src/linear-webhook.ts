import { createServer, Server } from 'http';
import { LinearWebhookClient } from '@linear/sdk/webhooks';
import type { EntityWebhookPayloadWithIssueData } from '@linear/sdk/webhooks';
import { logger } from './logger.js';
import { executeAgentRun } from './linear-dispatcher.js';
import type { LinearContext, GateLabels } from './linear-dispatcher.js';
import type { GateSpec } from './linear-gate-specs.js';
import type { RunContext } from './agent-runtimes/types.js';
import {
  insertWebhookEvent,
  updateWebhookEventStatus,
  getLastCompletedGateRun,
  upsertGateComment,
  getGateCommentId,
  upsertIssueCache,
  softDeleteIssueCache,
  getPipelineEvents,
} from './db.js';
import { triggerAutoMerge } from './linear-auto-merge.js';
import { notifyPipelineStep } from './linear-notifications.js';
import { syncVaultPending } from './linear-vault-sync.js';
import { RetryableError, UserError, FatalError } from './errors/index.js';
import { WEBHOOK_MAX_RETRIES, WEBHOOK_BASE_DELAY_MS } from './config.js';

const DEFAULT_WEBHOOK_PORT = 3005;
const LABEL_RETRY_MAX = 3;
const LABEL_RETRY_BASE_MS = 5_000;
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
  if (gateName !== 'agent-readiness-gate') return { addIds, removeIds };
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

    const { text, error } = await retryWithBackoff(
      () => executeAgentRun(ctx, runContext),
      WEBHOOK_MAX_RETRIES,
      WEBHOOK_BASE_DELAY_MS,
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
  }
}

async function handleIssueUpdate(
  payload: EntityWebhookPayloadWithIssueData,
  ctx: LinearContext,
  gateSpecs: Map<string, GateSpec>,
): Promise<void> {
  const { data, updatedFrom, action } = payload;

  if (action !== 'update') return;

  logger.info(
    { issueId: data.id, action, identifier: data.identifier },
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

  if (data.labels.some((l) => l.name === 'warden:skip')) {
    logger.info(
      { issueId: data.id },
      'linear-webhook: warden:skip label present, skipping',
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

  updateWebhookEventStatus(eventKey, 'running');

  // Visual feedback: add evaluating label + initial comment
  if (ctx.gateLabels.evaluating) {
    ctx.client
      .updateIssue(data.id, { addedLabelIds: [ctx.gateLabels.evaluating] })
      .catch(() => {});
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

    const prompt =
      [
        `<gate-spec>\n${gateSpec.content}\n</gate-spec>`,
        `<issue>\nTitle: ${data.title}\nID: ${data.identifier}\n\n${data.description ?? '(no description)'}\n</issue>`,
        `<transition>\nFrom: ${fromState.name}\nTo: ${toState.name}\n</transition>`,
      ].join('\n\n') + commentBlock;

    const runContext: RunContext = {
      prompt,
      groupFolder: ctx.dispatchGroup.folder,
      chatJid,
      isControlGroup: false,
      isScheduledTask: true,
      effort: gateSpec.effort ?? 'medium',
    };

    const { text, error } = await retryWithBackoff(
      () => executeAgentRun(ctx, runContext),
      WEBHOOK_MAX_RETRIES,
      WEBHOOK_BASE_DELAY_MS,
    );

    // Container may exit non-zero (e.g., docker kill) but still have output in either field
    const output = text || error || '';
    const parsedVerdict = parseVerdict(output);

    let verdict: 'SHIP' | 'REVISE' | 'BLOCK';
    let commentBody: string;

    if (!parsedVerdict && error) {
      verdict = gateSpec.fallback;
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

    if (effectiveMode === 'strict' && verdict !== 'SHIP') {
      try {
        await ctx.client.updateIssue(data.id, { stateId: fromStateId });
        logger.info(
          { issueId: data.id, gate: gateSpec.name, verdict },
          'linear-webhook: reverted transition (strict mode)',
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
