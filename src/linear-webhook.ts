import { createServer, Server } from 'http';
import { LinearWebhookClient } from '@linear/sdk/webhooks';
import type { EntityWebhookPayloadWithIssueData } from '@linear/sdk/webhooks';
import { logger } from './logger.js';
import { executeAgentRun } from './linear-dispatcher.js';
import type { LinearContext } from './linear-dispatcher.js';
import type { GateSpec } from './linear-gate-specs.js';
import type { RunContext } from './agent-runtimes/types.js';
import {
  insertWebhookEvent,
  updateWebhookEventStatus,
  getLastCompletedGateRun,
  upsertGateComment,
  getGateCommentId,
} from './db.js';
import { triggerAutoMerge } from './linear-auto-merge.js';

const DEFAULT_WEBHOOK_PORT = 3005;

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
        return;
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

    const { text, error } = await executeAgentRun(ctx, runContext);

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
    logger.info(
      { issueId: data.id, gate: gateSpec.name, verdict, mode: effectiveMode },
      'linear-webhook: gate evaluation complete',
    );
  } catch (err) {
    const errorMsg = err instanceof Error ? err.message : String(err);
    updateWebhookEventStatus(eventKey, 'error', { error: errorMsg });
    logger.error(
      { issueId: data.id, gate: gateSpec.name, err },
      'linear-webhook: gate evaluation failed',
    );
  } finally {
    ctx.inFlightGate.delete(data.id);
    const removeIds: string[] = [];
    const addIds: string[] = [];
    if (ctx.gateLabels.evaluating) removeIds.push(ctx.gateLabels.evaluating);
    if (finalVerdict === 'SHIP' && ctx.gateLabels.scoped) {
      addIds.push(ctx.gateLabels.scoped);
      if (ctx.gateLabels.revise) removeIds.push(ctx.gateLabels.revise);
    } else if (finalVerdict === 'REVISE' && ctx.gateLabels.revise) {
      addIds.push(ctx.gateLabels.revise);
      if (ctx.gateLabels.scoped) removeIds.push(ctx.gateLabels.scoped);
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
      ctx.client.updateIssue(data.id, update).catch((err) => {
        logger.warn(
          { issueId: data.id, addIds, safeRemoveIds, err },
          'linear-webhook: failed to update gate labels',
        );
      });
    }

    if (finalVerdict === 'SHIP' && gateSpec.name === 'output-quality-gate') {
      triggerAutoMerge(ctx, data.id).catch((err) => {
        logger.warn(
          { issueId: data.id, err },
          'linear-webhook: auto-merge trigger failed',
        );
      });
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

  // handler.on('Issue') provides typed payload but needs narrowing to the issue-specific union member
  handler.on('Issue', (payload) => {
    handleIssueUpdate(
      payload as EntityWebhookPayloadWithIssueData,
      ctx,
      gateSpecs,
    ).catch((err) => {
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
