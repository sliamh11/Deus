import path from 'path';

import { ASSISTANT_NAME } from './config.js';
import { consolidateSessionLog } from './consolidation-core.js';
import {
  getAutoCompressWatermark,
  getMessagesSince,
  setAutoCompressWatermark,
} from './db.js';
import { logger } from './logger.js';
import { resolveVaultPath } from './solutions/index.js';
import type { RegisteredGroup } from './types.js';

/**
 * Save the current session's conversation to the vault before an idle reset
 * clears it. Lightweight: no LLM calls, no atom extraction — just raw
 * conversation as a session log + fire-and-forget embedding indexing.
 *
 * Path/envelope assembly + index dispatch live in the shared consolidation
 * core (LIA-302); this surface owns the trigger, the message source, the
 * empty-guard, and the `type: session` format. It also short-circuits on a
 * missing vault BEFORE the (non-trivial) message query — the core re-checks
 * the vault as the authoritative write gate.
 */
export async function autoCompressSession(
  group: RegisteredGroup,
  chatJid: string,
  effectiveIdleHours: number,
  lastUsed?: string,
): Promise<void> {
  // Short-circuit before the DB query when there is nowhere to write.
  if (!resolveVaultPath()) {
    logger.debug('Auto-compress skipped: no vault configured');
    return;
  }

  // Anchor to the persisted watermark (last successful capture), not
  // `now()`; only a chat with no prior capture falls back to the pre-fix
  // `lastUsed - 2x idle` heuristic as a one-time bootstrap.
  const watermark = getAutoCompressWatermark(chatJid);
  const anchor = watermark
    ? new Date(watermark).getTime()
    : lastUsed
      ? new Date(lastUsed).getTime() - effectiveIdleHours * 2 * 3_600_000
      : Date.now() - effectiveIdleHours * 2 * 3_600_000;
  const sinceTimestamp = new Date(anchor).toISOString();

  const messages = getMessagesSince(
    chatJid,
    sinceTimestamp,
    ASSISTANT_NAME,
    500,
    true,
  );

  if (messages.length === 0) {
    logger.info(
      { group: group.name, chatJid },
      'Auto-compress: no messages in window',
    );
    return;
  }

  const now = new Date();
  const dateStr = now.toISOString().slice(0, 10);
  const timeStr = now.toISOString().slice(11, 16).replace(':', '');
  const safeFolder = path.basename(group.folder);

  const firstUserMsg = messages.find((m) => !m.is_from_me);
  const tldr = firstUserMsg
    ? firstUserMsg.content.replace(/\n/g, ' ').slice(0, 120)
    : 'auto-compressed session';

  const body = messages
    .map((m) => {
      const time = new Date(m.timestamp).toLocaleTimeString('en-US', {
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      });
      const sender = m.sender_name || m.sender || 'unknown';
      return `**${sender}** (${time}): ${m.content}`;
    })
    .join('\n\n');

  const frontmatter = `type: session
date: ${dateStr}
topics: [auto-compress]
tldr: |
  ${tldr}`;

  const savedPath = consolidateSessionLog({
    dateStr,
    fileStem: `auto-${safeFolder}-${timeStr}`,
    frontmatter,
    body,
    spawnLabel: 'auto-compress-index',
  });

  if (savedPath) {
    // WhatsApp message timestamps are second-resolution (whatsapp.ts:362),
    // so subtract 1s to avoid excluding a same-second sibling message on the
    // next capture. getMessagesSince re-sorts ascending, so the last element
    // is the max timestamp.
    const maxCapturedTs = messages[messages.length - 1].timestamp;
    const newWatermark = new Date(
      new Date(maxCapturedTs).getTime() - 1_000,
    ).toISOString();
    setAutoCompressWatermark(chatJid, newWatermark);
    logger.info(
      { group: group.name, path: savedPath },
      'Auto-compress: session saved',
    );
  }
}
