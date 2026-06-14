import { createServer, Server } from 'http';
import { LinearWebhookClient } from '@linear/sdk/webhooks';
import type { EntityWebhookPayloadWithIssueData } from '@linear/sdk/webhooks';
import { logger } from './logger.js';
import { executeAgentRun, extractScopeBlock } from './linear-dispatcher.js';
import { escapeXmlForPrompt } from './prompt-utils.js';
import { formatLocalHHMM } from './timezone.js';
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
  computeEnrichmentHash,
  getIssuePr,
} from './db.js';
import { triggerAutoMerge, queryPrState } from './linear-auto-merge.js';
import { notifyPipelineStep } from './linear-notifications.js';
import { syncVaultPending } from './linear-vault-sync.js';
import { RetryableError, UserError, FatalError } from './errors/index.js';
import { fireAndForget } from './async/index.js';
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

/**
 * LIA-240: whether to skip a transition because the pipeline's own bot triggered
 * it (loop-prevention). Skips ONLY when a dedicated bot id is configured AND the
 * webhook actor matches it. An empty botUserId (no dedicated bot — see
 * resolveBotUserId) never skips, so CLI-initiated transitions made under the
 * shared operator identity are honored instead of being silently swallowed.
 */
export function shouldSkipBotTransition(
  actorId: string | undefined,
  botUserId: string,
): boolean {
  return Boolean(actorId) && Boolean(botUserId) && actorId === botUserId;
}

/**
 * LIA-169: An errored gate halts regardless of mode/verdict — its fallback
 * 'REVISE' would otherwise drive the strict revert and re-dispatch loop.
 * 'halt' = park (quiescent state); 'revert' = legit strict non-SHIP; 'none' = leave.
 */
export function gateTransitionAction(
  effectiveMode: 'advise' | 'strict',
  verdict: 'SHIP' | 'REVISE' | 'BLOCK',
  gateErrored: boolean,
): 'halt' | 'revert' | 'none' {
  if (gateErrored) return 'halt';
  if (effectiveMode === 'strict' && verdict !== 'SHIP') return 'revert';
  return 'none';
}

/**
 * LIA-241: Classify a gate run's raw output so a clean-exit-without-verdict is
 * never mistaken for a real verdict (LIA-169 only handled the non-empty-error path).
 *
 * - 'error'       = no parseable verdict AND a failure signal is present.
 * - 'malfunction' = clean exit but no parseable verdict (gate produced nothing usable).
 * - 'verdict'     = a parseable verdict — non-null parsedVerdict ALWAYS maps here,
 *                   even on a non-zero exit (honor a verdict the gate managed to emit).
 *
 * Both 'error' and 'malfunction' callers MUST set the gate-errored flag so the
 * outcome halts (parks to Manual Review Required) instead of reverting/escalating.
 */
export function classifyGateOutcome(
  parsedVerdict: 'SHIP' | 'REVISE' | 'BLOCK' | null,
  error: string,
): 'error' | 'malfunction' | 'verdict' {
  if (parsedVerdict) return 'verdict';
  return error ? 'error' : 'malfunction';
}

/** LIA-169: an errored gate logs `gate_error`, not the misleading `gate_revise`. */
export function gateOutcomeEventType(
  verdict: 'SHIP' | 'REVISE' | 'BLOCK',
  gateErrored: boolean,
): 'gate_ship' | 'gate_revise' | 'gate_error' {
  if (gateErrored) return 'gate_error';
  return verdict === 'SHIP' ? 'gate_ship' : 'gate_revise';
}

/**
 * Park an errored gate's issue in "Manual Review Required" (quiescent; nothing
 * polls it). Must be called from a finally, not the try, so a thrown exception
 * doesn't bypass it (LIA-175). Swallows its own errors — never rethrows over the
 * original gate error.
 */
export async function parkErroredGateIssue(
  ctx: LinearContext,
  issueId: string,
  gateName: string,
  logPrefix: string,
): Promise<void> {
  try {
    const manualState = ctx.stateByName.get('Manual Review Required');
    if (manualState) {
      await ctx.client.updateIssue(issueId, { stateId: manualState.id });
      logger.info(
        { issueId, gate: gateName },
        `${logPrefix}: gate errored — moved to Manual Review Required`,
      );
    } else {
      logger.warn(
        { issueId, gate: gateName },
        `${logPrefix}: Manual Review Required state not found — leaving issue in place`,
      );
    }
  } catch (err) {
    logger.warn(
      { issueId, err },
      `${logPrefix}: failed to park errored gate issue in Manual Review Required`,
    );
  }
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

/**
 * LIA-125: Extract an enrichment scope block from an issue description using
 * sentinel markers. Returns the block content if found, undefined otherwise.
 *
 * Used to recover enrichment when a gate container agent wrote the scope block
 * directly via MCP (no /workspace/project codebase access) rather than in its
 * text output. In that case parseEnrichment(output.text) returns null, but the
 * description already contains the gate:start/end block.
 */
export function extractScopeBlockFromDescription(
  description: string | null | undefined,
  gateName: string,
): string | undefined {
  if (!description) return undefined;
  const startMarker = `<!-- gate:${gateName}:start -->`;
  const endMarker = `<!-- gate:${gateName}:end -->`;
  const startIdx = description.indexOf(startMarker);
  const endIdx = description.indexOf(endMarker);
  if (startIdx === -1 || endIdx === -1 || endIdx <= startIdx) return undefined;
  const content = description
    .slice(startIdx + startMarker.length, endIdx)
    .trim();
  return content || undefined;
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

/**
 * Returns label IDs to remove when an issue enters a terminal state (Done / Cancelled).
 * Strips all transient Warden labels regardless of whether a gate ran.
 */
export function computeTerminalLabelCleanup(
  gateLabels: GateLabels,
  issueLabelIds: string[],
): string[] {
  const labelSet = new Set(issueLabelIds);
  return [
    gateLabels.revise,
    gateLabels.evaluating,
    gateLabels.bouncedUnscoped,
    gateLabels.bouncedStale,
    gateLabels.bouncedNoContext,
  ].filter((id): id is string => !!id && labelSet.has(id));
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

async function postNewGateComment(
  ctx: LinearContext,
  issueId: string,
  body: string,
): Promise<void> {
  try {
    const payload = await ctx.client.createComment({ issueId, body });
    if (!payload?.commentId) {
      logger.warn({ issueId }, 'linear-webhook: createComment returned no ID');
    }
  } catch (err) {
    logger.warn(
      { issueId, err },
      'linear-webhook: failed to create gate comment',
    );
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
        comments
          .map(
            (c) =>
              `[${escapeXmlForPrompt(c.author)}]: ${escapeXmlForPrompt(c.body)}`,
          )
          .join('\n\n') +
        '\n</comments>';
    }

    const prompt =
      [
        `<gate-spec>\n${completionGateSpec.content}\n</gate-spec>`,
        `<invocation-context>pre-merge</invocation-context>`,
        `<issue>\nTitle: ${escapeXmlForPrompt(issueData.title)}\nID: ${issueData.identifier}\n\n${escapeXmlForPrompt(issueData.description ?? '(no description)')}\n</issue>`,
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
    if (verdict === 'REVISE') {
      await postNewGateComment(ctx, issueData.id, commentBody);
    } else {
      await postOrUpdateComment(
        ctx,
        issueData.id,
        'Done:pre-merge',
        commentBody,
      );
    }

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
        await postNewGateComment(ctx, issueId, guideBody);
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
  if (shouldSkipBotTransition(actorId, ctx.botUserId)) {
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

  // Terminal state: strip transient Warden labels and bail out — no gate fires on Done/Cancelled
  if (toState.type === 'completed' || toState.type === 'cancelled') {
    if (data.labels.some((l) => l.name === 'Done: Pre-implemented')) {
      logger.info(
        { issueId: data.id },
        'linear-webhook: Done: Pre-implemented label present, skipping gate',
      );
    }
    const removeIds = computeTerminalLabelCleanup(
      ctx.gateLabels,
      data.labels.map((l) => l.id),
    );
    if (removeIds.length > 0) {
      retryLabelUpdate(ctx.client, data.id, { removedLabelIds: removeIds });
      logger.info(
        { issueId: data.id, toState: toState.name, count: removeIds.length },
        'linear-webhook: stripped transient labels on terminal state entry',
      );
    }
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
    await postNewGateComment(ctx, data.id, body);

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
          const resetTime = formatLocalHHMM(resetAt.toISOString());
          fireAndForget(
            notifyPipelineStep(
              ctx,
              data.id,
              data.identifier,
              'gate_cooldown',
              `${gateSpec.name}: ${remainMin}min remaining, resets at ${resetTime}`,
            ),
            {
              name: 'webhook.notify.cooldown',
              onError: (e) =>
                logger.error(
                  { issueId: data.id, gate: gateSpec.name, err: e },
                  'notifyPipelineStep failed (cooldown)',
                ),
            },
          );
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

  // LIA-119: skip bouncer eval when the issue already has an open or merged PR.
  // Triage solves this on fresh agent dispatch (triage_skip_open_pr) but the
  // bypass doesn't reach the bouncer when a state transition re-enters it (e.g.
  // In Review → Todo). Inserting BEFORE inFlightGate.add keeps the fast-path
  // clean — no lock to release on early return.
  // CLOSED (abandoned) PRs fall through to normal eval so a fresh agent can run.
  if (gateSpec.name === 'bouncer-gate') {
    const prRec = getIssuePr(data.id);
    if (prRec?.pr_url) {
      const prState = await queryPrState(prRec.pr_url);
      if (prState && (prState.state === 'OPEN' || prState.state === 'MERGED')) {
        updateWebhookEventStatus(eventKey, 'done', { verdict: 'skipped' });
        logger.info(
          { issueId: data.id, pr: prRec.pr_url, prState: prState.state },
          'linear-webhook: bouncer skip — open/merged PR',
        );
        return;
      }
    }
  }

  ctx.inFlightGate.add(data.id);

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
          fireAndForget(
            notifyPipelineStep(
              ctx,
              data.id,
              data.identifier,
              'gate_ship',
              `${gateSpec.name}: SHIP (hash fast-path)`,
            ),
            {
              name: 'webhook.notify.hashFastPath',
              onError: (e) =>
                logger.error(
                  { issueId: data.id, gate: gateSpec.name, err: e },
                  'notifyPipelineStep failed (hash fast-path)',
                ),
            },
          );
          logger.info(
            {
              issueId: data.id,
              gate: gateSpec.name,
              ageHours: Math.round(ageHours),
            },
            'linear-webhook: bouncer fast-path SHIP (hash match)',
          );
          ctx.inFlightGate.delete(data.id);
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
      fireAndForget(ctx.client.updateIssue(data.id, startUpdate), {
        name: 'webhook.updateIssue.startLabel',
        onError: (e) =>
          logger.error(
            { issueId: data.id, err: e },
            'updateIssue start-label failed',
          ),
      });
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
          comments
            .map(
              (c) =>
                `[${escapeXmlForPrompt(c.author)}]: ${escapeXmlForPrompt(c.body)}`,
            )
            .join('\n\n') +
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
        `<issue>\nTitle: ${escapeXmlForPrompt(data.title)}\nID: ${data.identifier}\n\n${escapeXmlForPrompt(data.description ?? '(no description)')}\n</issue>`,
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
    let verdictText = '';

    const gateOutcome = classifyGateOutcome(parsedVerdict, error);
    if (gateOutcome === 'error') {
      gateDidError = true;
      verdict = gateSpec.fallback;
      logger.warn(
        {
          issueId: data.id,
          gate: gateSpec.name,
          error,
        },
        'linear-webhook: gate agent error, applying Warden: Error label',
      );
      commentBody = formatGateComment(
        gateSpec.name,
        'ERROR',
        `Gate agent error:\n\`\`\`\n${error}\n\`\`\``,
        gateSpec.mode,
      );
    } else if (gateOutcome === 'malfunction') {
      // LIA-241: clean exit but no parseable `## Verdict:` line. Treat as a gate
      // malfunction (not a genuine REVISE) — byte-symmetric with the 'error'
      // branch: gateDidError halts/parks, applies Warden: Error, skips the
      // bounced label + attempt escalation, and never merges the raw output into
      // the issue description (finalEnrichment stays undefined). verdictText also
      // stays '' so the pipeline detail reads '(no detail)', never raw output.
      gateDidError = true;
      verdict = gateSpec.fallback;
      const preview = output.length > 500 ? `${output.slice(0, 500)}…` : output;
      logger.warn(
        {
          issueId: data.id,
          gate: gateSpec.name,
          outputLen: output.length,
        },
        'linear-webhook: gate agent exited cleanly but produced no ## Verdict marker — treating as gate malfunction',
      );
      commentBody = formatGateComment(
        gateSpec.name,
        'ERROR',
        `Gate agent exited cleanly but produced no \`## Verdict:\` marker (malfunction).\n\nOutput preview:\n\`\`\`\n${preview}\n\`\`\``,
        gateSpec.mode,
      );
    } else {
      // gateOutcome === 'verdict' here, so parsedVerdict is non-null (the
      // ?? fallback only satisfies the type checker). LIA-241 removed the old
      // "no markers → use full output as enrichment" path: it is now structurally
      // unreachable (a marker-less clean exit routes to 'malfunction' above), and
      // it was the vector that merged raw malformed output into the description.
      verdict = parsedVerdict ?? gateSpec.fallback;
      const enrichmentBody = parseEnrichment(output);

      finalEnrichment = enrichmentBody ?? undefined;

      // LIA-125: Gate container agents without /workspace/project access write
      // the scope block directly via mcp__linear__update_issue instead of as
      // text output. In that case parseEnrichment returns null, leaving
      // finalEnrichment undefined and computeScopeLabelChanges skipping Scoped.
      // Recover by fetching the fresh description and extracting the sentinel block.
      if (
        finalEnrichment === undefined &&
        verdict === 'SHIP' &&
        (gateSpec.name === 'enrichment-gate' ||
          gateSpec.name === 'agent-readiness-gate')
      ) {
        try {
          const freshIssue = await ctx.client.issue(data.id);
          const recovered = extractScopeBlockFromDescription(
            freshIssue.description,
            gateSpec.name,
          );
          if (recovered) {
            finalEnrichment = recovered;
            logger.info(
              { issueId: data.id, gate: gateSpec.name },
              'linear-webhook: enrichment recovered from description (agent used MCP write instead of text output)',
            );
          }
        } catch (recoverErr) {
          logger.warn(
            { issueId: data.id, gate: gateSpec.name, err: recoverErr },
            'linear-webhook: failed to fetch fresh description for enrichment recovery',
          );
        }
      }

      verdictText = stripEnrichmentSection(output);
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

    if (verdict === 'SHIP') {
      await postOrUpdateComment(ctx, data.id, toState.name, commentBody);
    } else {
      await postNewGateComment(ctx, data.id, commentBody);
    }

    const effectiveMode = data.labels.some((l) => l.name === 'warden:strict')
      ? 'strict'
      : gateSpec.mode;

    const transitionAction = gateTransitionAction(
      effectiveMode,
      verdict,
      gateDidError,
    );
    // LIA-175: the 'halt' (gate-errored) park now lives in the finally block,
    // keyed on gateDidError, so it ALSO fires when an exception is caught below
    // (gateTransitionAction returns 'halt' iff gateErrored, so this is
    // behaviorally equivalent for the verdict-error case).
    if (transitionAction === 'revert') {
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
          priority: 1,
        });
        logger.info(
          {
            issueId: data.id,
            gate: gateSpec.name,
            verdict,
            revertTo: revertStateName,
          },
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
    const eventType = gateOutcomeEventType(verdict, gateDidError);
    const reasonLine =
      verdictText.split('\n').find((l) => l.trim()) || '(no detail)';
    const pipelineDetail =
      verdict !== 'SHIP'
        ? `${gateSpec.name}: ${verdict} — ${reasonLine}`.slice(0, 120)
        : `${gateSpec.name}: ${verdict}`;
    fireAndForget(
      notifyPipelineStep(
        ctx,
        data.id,
        data.identifier,
        eventType,
        pipelineDetail,
      ),
      { name: 'linear-webhook.notify-pipeline' },
    );
    logger.info(
      { issueId: data.id, gate: gateSpec.name, verdict, mode: effectiveMode },
      'linear-webhook: gate evaluation complete',
    );
  } catch (err) {
    gateDidError = true;
    const errorMsg = err instanceof Error ? err.message : String(err);
    updateWebhookEventStatus(eventKey, 'error', { error: errorMsg });
    fireAndForget(
      notifyPipelineStep(
        ctx,
        data.id,
        data.identifier,
        'gate_error',
        `${gateSpec.name}: ${errorMsg}`,
      ),
      { name: 'linear-webhook.notify-pipeline' },
    );
    logger.error(
      { issueId: data.id, gate: gateSpec.name, err },
      'linear-webhook: gate evaluation failed',
    );

    // Apply fallback verdict on infrastructure errors (matching agent-error path)
    finalVerdict = gateSpec.fallback;
    finalEnrichment = `Gate infrastructure error: ${errorMsg}`;
    const errorComment = formatGateComment(
      gateSpec.name,
      finalVerdict,
      `Gate infrastructure error (fallback: ${gateSpec.fallback}):\n\`\`\`\n${errorMsg}\n\`\`\``,
      gateSpec.mode,
    );
    fireAndForget(postNewGateComment(ctx, data.id, errorComment), {
      name: 'linear-webhook.error-comment',
      onError: (e) =>
        logger.error(
          { issueId: data.id, err: e },
          'linear-webhook: failed to post gate error comment',
        ),
    });
  } finally {
    ctx.inFlightGate.delete(data.id);
    const removeIds: string[] = [];
    const addIds: string[] = [];
    if (ctx.gateLabels.evaluating) removeIds.push(ctx.gateLabels.evaluating);
    if (gateDidError && ctx.gateLabels.error) addIds.push(ctx.gateLabels.error);
    // Only apply scope label changes when the gate ran cleanly (no infrastructure error or agent crash)
    if (!gateDidError) {
      const scopeLabels = computeScopeLabelChanges(
        gateSpec.name,
        finalVerdict,
        finalEnrichment,
        ctx.gateLabels,
      );
      addIds.push(...scopeLabels.addIds);
      removeIds.push(...scopeLabels.removeIds);
    }

    // Bouncer: apply bounced:<reason> label on REVISE, strip on SHIP.
    // LIA-169: skip on gate error — an errored bouncer's REVISE is a fallback,
    // not a real "unscoped" verdict, so don't stamp a misleading bounced label.
    if (gateSpec.name === 'bouncer-gate' && !gateDidError) {
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

    // LIA-175: park an errored gate's issue in a quiescent state. The inline
    // halt-park only ran for verdict-errors inside the try; an EXCEPTION caught
    // above skipped it, leaving the issue in "In Review" exposed to
    // sweepStaleInReview. Runs concurrently with the fire-and-forget label
    // update above — independent Linear calls, no ordering requirement.
    if (gateDidError) {
      await parkErroredGateIssue(ctx, data.id, gateSpec.name, 'linear-webhook');
    }

    // LIA-169: skip on gate error — the outer catch sets finalVerdict=REVISE +
    // finalEnrichment, which would otherwise increment the REVISE attempt count
    // on an infra failure (the gate never actually ran).
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
  let finalVerdict: string | undefined;
  let finalEnrichment: string | undefined;
  let gateAgentError = false;

  try {
    if (ctx.gateLabels.evaluating) {
      fireAndForget(
        ctx.client.updateIssue(issue.id, {
          addedLabelIds: [ctx.gateLabels.evaluating],
        }),
        {
          name: 'linear-webhook.eval-label',
          onError: (e) =>
            logger.error(
              { issueId: issue.id, err: e },
              'linear-webhook: failed to add evaluating label',
            ),
        },
      );
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
          comments
            .map(
              (c) =>
                `[${escapeXmlForPrompt(c.author)}]: ${escapeXmlForPrompt(c.body)}`,
            )
            .join('\n\n') +
          '\n</comments>';
      }
    }

    const prompt =
      [
        `<gate-spec>\n${gateSpec.content}\n</gate-spec>`,
        `<invocation-context>startup-sweep</invocation-context>`,
        `<issue>\nTitle: ${escapeXmlForPrompt(issue.title)}\nID: ${issue.identifier}\n\n${escapeXmlForPrompt(issue.description ?? '(no description)')}\n</issue>`,
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

    const { text, error } = await retryWithBackoff(
      () => executeAgentRun(ctx, runContext),
      WEBHOOK_MAX_RETRIES,
      WEBHOOK_BASE_DELAY_MS,
    );

    const output = text || error || '';
    const parsedVerdict = parseVerdict(output);
    let verdict: 'SHIP' | 'REVISE' | 'BLOCK';
    let commentBody: string;
    let verdictText = '';

    const sweepOutcome = classifyGateOutcome(parsedVerdict, error);
    if (sweepOutcome === 'error') {
      verdict = 'REVISE';
      gateAgentError = true;
      commentBody = formatGateComment(
        gateSpec.name,
        'ERROR',
        `Gate agent error (startup sweep):\n\`\`\`\n${error}\n\`\`\``,
        gateSpec.mode,
      );
    } else if (sweepOutcome === 'malfunction') {
      // LIA-241: clean exit, no parseable `## Verdict:` line — same malfunction
      // handling as handleIssueUpdate. gateAgentError halts/parks; finalEnrichment
      // and verdictText stay unset so no raw output reaches the description or
      // pipeline detail.
      verdict = 'REVISE';
      gateAgentError = true;
      const preview = output.length > 500 ? `${output.slice(0, 500)}…` : output;
      logger.warn(
        {
          issueId: issue.id,
          gate: gateSpec.name,
          outputLen: output.length,
        },
        'startup-sweep: gate agent exited cleanly but produced no ## Verdict marker — treating as gate malfunction',
      );
      commentBody = formatGateComment(
        gateSpec.name,
        'ERROR',
        `Gate agent exited cleanly but produced no \`## Verdict:\` marker (malfunction, startup sweep).\n\nOutput preview:\n\`\`\`\n${preview}\n\`\`\``,
        gateSpec.mode,
      );
    } else {
      verdict = parsedVerdict ?? 'REVISE';
      const enrichmentBody = parseEnrichment(output);
      finalEnrichment = enrichmentBody ?? undefined;

      // LIA-125: Same recovery as in handleIssueUpdate — see comment there.
      if (
        finalEnrichment === undefined &&
        verdict === 'SHIP' &&
        (gateSpec.name === 'enrichment-gate' ||
          gateSpec.name === 'agent-readiness-gate')
      ) {
        try {
          const freshIssue = await ctx.client.issue(issue.id);
          const recovered = extractScopeBlockFromDescription(
            freshIssue.description,
            gateSpec.name,
          );
          if (recovered) {
            finalEnrichment = recovered;
            logger.info(
              { issueId: issue.id, gate: gateSpec.name },
              'linear-webhook: enrichment recovered from description (agent used MCP write instead of text output)',
            );
          }
        } catch (recoverErr) {
          logger.warn(
            { issueId: issue.id, gate: gateSpec.name, err: recoverErr },
            'linear-webhook: failed to fetch fresh description for enrichment recovery',
          );
        }
      }

      verdictText = stripEnrichmentSection(output);
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
        fireAndForget(
          ctx.client.updateIssue(issue.id, { description: newDesc }),
          {
            name: 'linear-webhook.enrichment-desc',
            onError: (e) =>
              logger.error(
                { issueId: issue.id, err: e },
                'linear-webhook: failed to update description with enrichment',
              ),
          },
        );
      }
    }

    if (verdict === 'SHIP') {
      await postOrUpdateComment(ctx, issue.id, stateName, commentBody);
    } else {
      await postNewGateComment(ctx, issue.id, commentBody);
    }

    const effectiveMode = labels.some((l) => l.name === 'warden:strict')
      ? 'strict'
      : gateSpec.mode;

    const transitionAction = gateTransitionAction(
      effectiveMode,
      verdict,
      gateAgentError,
    );
    // LIA-175: the 'halt' (gate-errored) park now lives in the finally block,
    // keyed on gateAgentError, so it also covers the exception path. See
    // handleIssueUpdate for the rationale.
    if (transitionAction === 'revert') {
      if (gateSpec.revertTo) {
        const revertState = ctx.stateByName.get(gateSpec.revertTo);
        if (revertState) {
          await ctx.client.updateIssue(issue.id, {
            stateId: revertState.id,
            priority: 1,
          });
        }
      } else {
        logger.warn(
          { issueId: issue.id, gate: gateSpec.name },
          'startup-sweep: strict mode revert skipped — no revert_to configured',
        );
      }
    }

    finalVerdict = verdict;
    const eventType = gateOutcomeEventType(verdict, gateAgentError);
    const sweepReasonLine =
      verdictText.split('\n').find((l) => l.trim()) || '(no detail)';
    const sweepDetail =
      verdict !== 'SHIP'
        ? `${gateSpec.name}: ${verdict} — ${sweepReasonLine} (startup sweep)`.slice(
            0,
            140,
          )
        : `${gateSpec.name}: ${verdict} (startup sweep)`;
    // notifyPipelineStep is the authoritative writer (row + status_summary +
    // comment); a bare logPipelineEvent here would double-write via the sink.
    fireAndForget(
      notifyPipelineStep(
        ctx,
        issue.id,
        issue.identifier,
        eventType,
        sweepDetail,
      ),
      { name: 'linear-webhook.notify-pipeline' },
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
    fireAndForget(postNewGateComment(ctx, issue.id, errorComment), {
      name: 'linear-webhook.startup-error-comment',
      onError: (e) =>
        logger.error(
          { issueId: issue.id, err: e },
          'linear-webhook: failed to post startup sweep error comment',
        ),
    });
    fireAndForget(
      notifyPipelineStep(
        ctx,
        issue.id,
        issue.identifier,
        'gate_error',
        `${gateSpec.name}: ${errorMsg} (startup sweep)`,
      ),
      { name: 'linear-webhook.notify-pipeline' },
    );
    logger.error(
      { issueId: issue.id, gate: gateSpec.name, err },
      'startup-sweep: gate evaluation failed',
    );
    // Never fallback-SHIP on infrastructure errors — gate didn't actually run.
    // Use gateSpec.fallback for symmetry with handleIssueUpdate (1285); the
    // !gateAgentError guard below keeps this value out of real behavior anyway.
    gateAgentError = true;
    finalVerdict = gateSpec.fallback;
    finalEnrichment = `Gate infrastructure error (startup sweep): ${errorMsg}`;
  } finally {
    ctx.inFlightGate.delete(issue.id);

    const removeIds: string[] = [];
    const addIds: string[] = [];
    if (ctx.gateLabels.evaluating) removeIds.push(ctx.gateLabels.evaluating);
    if (gateAgentError && ctx.gateLabels.error)
      addIds.push(ctx.gateLabels.error);
    if (!gateAgentError) {
      const scopeLabels = computeScopeLabelChanges(
        gateSpec.name,
        finalVerdict,
        finalEnrichment,
        ctx.gateLabels,
      );
      addIds.push(...scopeLabels.addIds);
      removeIds.push(...scopeLabels.removeIds);
    }
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

    // LIA-175: park an errored gate's issue so an exception caught above doesn't
    // leave it in "In Review". See handleIssueUpdate for the rationale.
    if (gateAgentError) {
      await parkErroredGateIssue(ctx, issue.id, gateSpec.name, 'startup-sweep');
    }

    // LIA-169: skip on gate error — see handleIssueUpdate; the outer catch sets
    // finalVerdict=REVISE + finalEnrichment, which would pollute the attempt count.
    if (finalVerdict && finalEnrichment && !gateAgentError) {
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

// Test-only export — allows unit tests to drive handleIssueUpdate directly
// without starting the full webhook server. Pattern mirrors _setSleepFnForTests.
export const _handleIssueUpdateForTest = handleIssueUpdate;
