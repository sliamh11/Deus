import crypto from 'crypto';
import path from 'path';

import { envPositiveInt } from './env-utils.js';
import { logger } from './logger.js';
import { writeSessionLogAndIndex } from './memory-session-log.js';
import { messageText } from './openai-messages.js';
import { resolveVaultPath } from './solutions/index.js';

/**
 * Web UI conversation auto-consolidation into vault memory (LIA-295).
 *
 * The /compress and /handoff skills operate on a developer's host Claude Code
 * session — they never see the container assistant's end-user conversations.
 * Web UI (Odysseus /v1) turns run on fresh, non-persisted sessions (LIA-294)
 * and never hit the DB, so nothing consolidates those conversations into
 * long-term memory. This closes that gap: when a Web UI conversation grows past
 * a threshold, write it to the vault as a session log and index it via
 * `memory_indexer.py --add` (atom extraction + L2 dedup live inside the
 * indexer, which is the pollution control). Reuses the auto-compress write+index
 * tail (memory-session-log.ts).
 *
 * The Web UI control group is the operator themselves (isControlGroup) — these
 * are the user's own chats into the user's own vault, the same trust model as
 * /compress. So the real risk is vault pollution (handled by --add dedup), not
 * third-party privacy.
 */

// Kill switch — ON by default. Set DEUS_WEBUI_CONSOLIDATE=0 (or false) to
// disable Web UI consolidation without a code change (LIA-295).
function consolidationEnabled(): boolean {
  const raw = process.env.DEUS_WEBUI_CONSOLIDATE;
  return raw !== '0' && raw !== 'false';
}

// Consolidate once the conversation reaches EITHER threshold (env-overridable;
// envPositiveInt clamps a non-positive/NaN override back to the fallback).
const MIN_TURNS = () => envPositiveInt('DEUS_WEBUI_CONSOLIDATE_MIN_TURNS', 3);
const MIN_CHARS = () => envPositiveInt('DEUS_WEBUI_CONSOLIDATE_MIN_CHARS', 200);

// In-flight guard keyed by conversation hash. A per-request boolean would not
// survive across the queued turns GroupQueue serialises on the shared jid; this
// module-level Set blocks a re-entrant consolidation for the same conversation
// while a prior detached `--add` is still running (released on spawn settle).
const inFlightConsolidation = new Set<string>();

interface ChatMessage {
  role?: unknown;
  content?: unknown;
}

function extractMessages(body: unknown): ChatMessage[] {
  const messages = (body as { messages?: unknown })?.messages;
  return Array.isArray(messages) ? (messages as ChatMessage[]) : [];
}

function roleLabel(role: unknown): string {
  if (role === 'user') return 'User';
  if (role === 'assistant') return 'Assistant';
  if (role === 'system') return 'System';
  return typeof role === 'string' && role ? role : 'unknown';
}

/**
 * Consolidate a completed Web UI turn's conversation into vault memory.
 *
 * Fully fire-and-forget: returns void (NOT a Promise) and must never be
 * awaited — the SSE response (res.end via finalize) must not wait on a vault
 * write or a detached indexer spawn. The spawn touches no `res` object, so it
 * can never run after the response ends.
 */
export function consolidateWebConversation(body: unknown): void {
  if (!consolidationEnabled()) return;

  const messages = extractMessages(body);
  if (messages.length === 0) return;

  const userMessages = messages.filter((m) => m.role === 'user');
  // Hash the RAW first user message (messages[], not the budget-pruned prompt
  // transcript) so the key stays stable once long histories get truncated.
  const firstUser = userMessages[0];
  if (!firstUser) return;
  const firstUserText = messageText(firstUser.content);
  if (!firstUserText.trim()) return;

  // Transcript = user + assistant turns (skip system/snapshot noise).
  const transcriptLines: string[] = [];
  for (const m of messages) {
    if (m.role !== 'user' && m.role !== 'assistant') continue;
    const text = messageText(m.content).trim();
    if (!text) continue;
    transcriptLines.push(`**${roleLabel(m.role)}**: ${text}`);
  }
  if (transcriptLines.length === 0) return;
  const transcript = transcriptLines.join('\n\n');

  // Threshold: consolidate once EITHER the user-turn count OR the total
  // transcript length crosses its bound. Below both → not worth persisting yet.
  if (userMessages.length < MIN_TURNS() && transcript.length < MIN_CHARS()) {
    return;
  }

  const vaultPath = resolveVaultPath();
  if (!vaultPath) {
    logger.debug('Web UI consolidation skipped: no vault configured');
    return;
  }

  const key = crypto
    .createHash('sha256')
    .update(firstUserText)
    .digest('hex')
    .slice(0, 16);

  // Drop re-entrant consolidations for the same conversation while a prior
  // --add is still running (released in onSettle below).
  if (inFlightConsolidation.has(key)) {
    logger.debug({ key }, 'Web UI consolidation: already in-flight, skipped');
    return;
  }

  const now = new Date();
  const dateStr = now.toISOString().slice(0, 10);
  // One file per conversation, overwritten as it grows — re-index re-dedups.
  const savedPath = path.join(
    vaultPath,
    'Session-Logs',
    dateStr,
    `webui-${key}.md`,
  );

  // JSON.stringify yields a valid double-quoted YAML scalar (YAML ⊇ JSON), so
  // untrusted first-message text (leading spaces, colons, quotes) can't corrupt
  // the frontmatter the indexer parses. Collapse whitespace + cap length first.
  const tldr = JSON.stringify(
    firstUserText.replace(/\s+/g, ' ').trim().slice(0, 120),
  );
  const content = `---
type: web-session
date: ${dateStr}
topics: [webui, auto-consolidate]
tldr: ${tldr}
---

${transcript}
`;

  inFlightConsolidation.add(key);
  try {
    writeSessionLogAndIndex(
      savedPath,
      content,
      'webui-consolidation-index',
      () => inFlightConsolidation.delete(key),
    );
  } catch (err) {
    // Synchronous mkdir/write failure throws BEFORE the indexer spawn launches,
    // so the onSettle release never fires — drop the guard here so the key isn't
    // stuck. (Success path releases via onSettle when the spawn settles.)
    inFlightConsolidation.delete(key);
    logger.warn({ err }, 'Web UI consolidation: vault write failed');
    return;
  }

  logger.info({ path: savedPath }, 'Web UI conversation consolidated to vault');
}
