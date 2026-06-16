import path from 'path';

import { logger } from './logger.js';
import { writeSessionLogAndIndex } from './memory-session-log.js';
import { resolveVaultPath } from './solutions/index.js';

/**
 * Shared session-log consolidation envelope for the two surfaces that persist a
 * conversation to the vault (auto-compress.ts; webui-consolidation.ts, LIA-295).
 * The core owns only the structural tail they shared byte-for-byte: vault
 * resolution, the `Session-Logs/<date>/<stem>.md` path, the
 * `---\n<fm>\n---\n\n<body>\n` envelope, and dispatch to writeSessionLogAndIndex
 * (the seam to the unchanged `memory_indexer.py --add` heart). Each surface
 * keeps its own format, empty-guard, kill-switch, and in-flight-key lifecycle.
 *
 * Throw-transparency: NO try/catch here — a synchronous write failure propagates
 * unchanged, so auto-compress still rejects (its orchestrator catches it) and
 * webui still catches it in its own try/catch to release its in-flight key.
 */
export interface ConsolidationSpec {
  /** Log date `YYYY-MM-DD`. The surface owns the clock read. */
  dateStr: string;
  /** File name without extension, e.g. `auto-whatsapp_main-1000` or `webui-<key>`. */
  fileStem: string;
  /** Pre-rendered YAML frontmatter block — NO `---` delimiters. */
  frontmatter: string;
  /** Pre-rendered markdown body. */
  body: string;
  /** fireAndForget label for the detached indexer spawn. */
  spawnLabel: string;
  /**
   * Optional callback fired when the attempt concludes: either synchronously on
   * a no-vault skip (below), or — via writeSessionLogAndIndex — when the
   * detached indexer child settles. webui uses it to release its in-flight
   * guard, so a skip MUST still fire it or the guard would stick. It may fire
   * more than once (the indexer settles on both 'close' and 'error'); callers
   * must keep it idempotent (webui's is a `Set.delete`).
   */
  onSettle?: () => void;
}

/**
 * Persist a pre-rendered session log to the vault and fire-and-forget index it.
 * Returns the written path, or null when skipped (no vault configured).
 */
export function consolidateSessionLog(spec: ConsolidationSpec): string | null {
  const vaultPath = resolveVaultPath();
  if (!vaultPath) {
    logger.debug('Consolidation skipped: no vault configured');
    // Release the caller's in-flight guard even though nothing was written —
    // the spawn (which would otherwise fire onSettle) never launches.
    spec.onSettle?.();
    return null;
  }

  const savedPath = path.join(
    vaultPath,
    'Session-Logs',
    spec.dateStr,
    `${spec.fileStem}.md`,
  );
  const content = `---\n${spec.frontmatter}\n---\n\n${spec.body}\n`;

  writeSessionLogAndIndex(savedPath, content, spec.spawnLabel, spec.onSettle);
  return savedPath;
}
