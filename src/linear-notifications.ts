/**
 * Pipeline notifications for Linear automation.
 *
 * Orchestrates three outputs per pipeline step:
 * 1. DB event log (append-only audit trail)
 * 2. macOS native notification (operator desktop)
 * 3. Unified Linear comment (single rolling timeline per issue)
 */

import { execFile } from 'child_process';
import { logger } from './logger.js';
import { fireAndForget } from './async/index.js';
import {
  logPipelineEvent,
  getPipelineEvents,
  upsertPipelineComment,
  getPipelineCommentId,
  updatePipelineEventStatusSummary,
} from './db.js';
import type { LinearContext } from './linear-dispatcher.js';

export function macosNotify(title: string, message: string): void {
  try {
    execFile(
      'osascript',
      [
        '-e',
        `display notification "${message.replace(/"/g, '\\"')}" with title "${title.replace(/"/g, '\\"')}"`,
      ],
      { timeout: 5000 },
      (err) => {
        if (err) logger.debug({ err }, 'notification: osascript failed');
      },
    );
  } catch {
    /* best-effort */
  }
}

export const EVENT_LABELS: Record<string, string> = {
  gate_ship: 'Gate → SHIP',
  gate_revise: 'Gate → REVISE',
  gate_cooldown: 'Gate cooldown',
  gate_error: 'Gate error',
  agent_dispatched: 'Agent dispatched',
  agent_started: 'Agent started working',
  agent_completed: 'Agent completed',
  agent_failed: 'Agent failed',
  pr_created: 'PR created',
  automerge_pending: 'Auto-merge pending',
  automerge_done: 'Auto-merged → Done',
  automerge_failed: 'Auto-merge failed',
  moved_done: 'Moved to Done',
  state_changed: 'State changed',
};

export function buildPipelineCommentBody(
  events: Array<{
    event_type: string;
    detail: string | null;
    created_at: string;
  }>,
): string {
  if (events.length === 0) return '**Pipeline Log**\n\n_No events yet._';

  const MAX_COMMENT_EVENTS = 50;
  const truncated = events.length > MAX_COMMENT_EVENTS;
  const visible = truncated ? events.slice(-MAX_COMMENT_EVENTS) : events;

  const lines: string[] = [];
  if (truncated) {
    lines.push(
      `_...${events.length - MAX_COMMENT_EVENTS} earlier events omitted_`,
    );
  }
  for (const e of visible) {
    const time = e.created_at.slice(11, 16); // HH:MM
    const label = EVENT_LABELS[e.event_type] || e.event_type;
    const detail = e.detail ? ` — ${e.detail}` : '';
    lines.push(`${time} — ${label}${detail}`);
  }

  return `**Pipeline Log**\n\n${lines.join('\n')}`;
}

const commentLocks = new Map<string, Promise<void>>();

export async function updateUnifiedComment(
  ctx: LinearContext,
  issueId: string,
): Promise<void> {
  const prev = commentLocks.get(issueId) ?? Promise.resolve();
  const current = prev.then(() => doUpdateUnifiedComment(ctx, issueId));
  const tracked = current.catch(() => {});
  commentLocks.set(issueId, tracked);
  tracked.then(() => {
    if (commentLocks.get(issueId) === tracked) commentLocks.delete(issueId);
  });
  return current;
}

async function doUpdateUnifiedComment(
  ctx: LinearContext,
  issueId: string,
): Promise<void> {
  try {
    const events = getPipelineEvents({ issueId });
    const body = buildPipelineCommentBody(events);
    const existingCommentId = getPipelineCommentId(issueId);

    if (existingCommentId) {
      await ctx.client.updateComment(existingCommentId, { body });
      upsertPipelineComment(issueId, existingCommentId);
    } else {
      const payload = await ctx.client.createComment({ issueId, body });
      if (payload?.commentId) {
        upsertPipelineComment(issueId, payload.commentId);
      }
    }
  } catch (err) {
    logger.debug(
      { issueId, err },
      'notification: failed to update unified comment',
    );
  }
}

const OLLAMA_URL =
  process.env.OLLAMA_URL || 'http://localhost:11434/api/generate';
const OLLAMA_MODEL = process.env.OLLAMA_MODEL || 'gemma4:e4b';
const STATUS_SUMMARY_TIMEOUT_MS = 5_000;
const SKIP_SUMMARY_EVENTS = new Set(['gate_cooldown']);

async function generateStatusSummary(
  eventType: string,
  identifier: string,
  detail?: string,
): Promise<string | null> {
  if (SKIP_SUMMARY_EVENTS.has(eventType)) return null;

  const label = EVENT_LABELS[eventType] || eventType;
  const prompt = `Summarize in 10 words or fewer what is happening. Event: ${label}. Issue: ${identifier}. Detail: ${detail ?? 'none'}. Reply with ONLY the summary, no quotes.`;

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), STATUS_SUMMARY_TIMEOUT_MS);

  try {
    const res = await fetch(OLLAMA_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: OLLAMA_MODEL,
        prompt,
        stream: false,
        options: { temperature: 0.2, num_ctx: 512 },
      }),
      signal: controller.signal,
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { response?: string };
    return data.response?.trim() || null;
  } catch (err) {
    logger.debug(
      { err, eventType },
      'pipeline: status summary generation failed',
    );
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export async function notifyPipelineStep(
  ctx: LinearContext,
  issueId: string,
  identifier: string,
  eventType: string,
  detail?: string,
): Promise<void> {
  const rowId = logPipelineEvent(issueId, identifier, eventType, detail);

  const label = EVENT_LABELS[eventType] || eventType;
  macosNotify('Deus', `${identifier}: ${label}`);

  if (rowId !== undefined) {
    fireAndForget(
      async () => {
        const summary = await generateStatusSummary(
          eventType,
          identifier,
          detail,
        );
        if (summary) updatePipelineEventStatusSummary(rowId, summary);
      },
      { name: 'pipeline.status-summary' },
    );
  }

  await updateUnifiedComment(ctx, issueId);
}
