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

const DEFAULT_WEBHOOK_PORT = 3005;

function parseVerdict(output: string): 'SHIP' | 'REVISE' | 'BLOCK' | null {
  const match = output.match(/^## Verdict:\s*(SHIP|REVISE|BLOCK)/m);
  return match ? (match[1] as 'SHIP' | 'REVISE' | 'BLOCK') : null;
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

async function handleIssueUpdate(
  payload: EntityWebhookPayloadWithIssueData,
  ctx: LinearContext,
  gateSpecs: Map<string, GateSpec>,
): Promise<void> {
  const { data, updatedFrom, action } = payload;

  if (action !== 'update') return;

  // SDK types updatedFrom as JSONObject; stateId is present at runtime for state changes
  const fromStateId = (updatedFrom as Record<string, unknown> | undefined)
    ?.stateId;
  if (typeof fromStateId !== 'string') return;

  const toStateId = data.stateId;
  const toState = ctx.stateById.get(toStateId);
  const fromState = ctx.stateById.get(fromStateId);
  if (!toState || !fromState) return;

  // Loop-break: skip if the bot triggered this transition
  const actorId = payload.actor?.id;
  if (actorId && actorId === ctx.botUserId) {
    logger.debug(
      { issueId: data.id, gate: toState.name },
      'linear-webhook: skipping bot-triggered transition',
    );
    return;
  }

  if (data.labels.some((l) => l.name === 'warden:skip')) {
    logger.debug(
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

    // Revert regardless of mode for illegal jumps
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
        logger.debug(
          { issueId: data.id, gate: gateSpec.name, elapsed },
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
    logger.debug(
      { issueId: data.id },
      'linear-webhook: gate already in flight for issue',
    );
    return;
  }
  ctx.inFlightGate.add(data.id);

  updateWebhookEventStatus(eventKey, 'running');

  try {
    const chatJid = `linear-gate-${gateSpec.name}-${data.id.slice(0, 8)}`;
    const prompt = [
      `<gate-spec>\n${gateSpec.content}\n</gate-spec>`,
      `<issue>\nTitle: ${data.title}\nID: ${data.identifier}\n\n${data.description ?? '(no description)'}\n</issue>`,
      `<transition>\nFrom: ${fromState.name}\nTo: ${toState.name}\n</transition>`,
    ].join('\n\n');

    const runContext: RunContext = {
      prompt,
      groupFolder: ctx.dispatchGroup.folder,
      chatJid,
      isControlGroup: false,
      isScheduledTask: true,
      effort: 'low',
    };

    const { text, error } = await executeAgentRun(ctx, runContext);

    let verdict: 'SHIP' | 'REVISE' | 'BLOCK';
    if (error) {
      verdict = gateSpec.fallback;
      const body = formatGateComment(
        gateSpec.name,
        verdict,
        `Gate agent error (fallback: ${gateSpec.fallback}):\n\`\`\`\n${error}\n\`\`\``,
        gateSpec.mode,
      );
      await postOrUpdateComment(ctx, data.id, toState.name, body);
    } else {
      verdict = parseVerdict(text) ?? gateSpec.fallback;
      const body = formatGateComment(
        gateSpec.name,
        verdict,
        text,
        gateSpec.mode,
      );
      await postOrUpdateComment(ctx, data.id, toState.name, body);
    }

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
