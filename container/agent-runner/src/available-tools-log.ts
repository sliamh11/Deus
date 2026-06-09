/**
 * Available-tools manifest capture for evolution tool observability (LIA-154).
 * Mirrors tool-call-log.ts. Writes the OFFERED tool set for this dispatch to a
 * per-interaction file the host reads back (readAvailableTools) into the
 * `available_tools` column — the "menu" ground truth that unblocks LIA-151.
 * Observability only (no live-scoring consumer); best-effort, never throws.
 * Opt-out via DEUS_AVAILABLE_TOOLS_LOG=0.
 */

import fs from 'fs';
import path from 'path';

import { safeInteractionId } from './safe-interaction-id.js';

const LOG_DIR = '/workspace/group/logs/available-tools';

/**
 * Write the offered tool manifest for this dispatch. Best-effort: a missing
 * interaction id (no host join key) or any fs error is swallowed — capture must
 * never affect the dispatch. Written once per dispatch (not per tool call), so
 * a plain overwrite is correct.
 */
export function writeAvailableTools(
  interactionId: string | undefined,
  tools: string[],
): void {
  try {
    if (!interactionId) return; // no join key → host can't read it back
    const logPath = path.join(
      LOG_DIR,
      `${safeInteractionId(interactionId)}.json`,
    );
    fs.mkdirSync(LOG_DIR, { recursive: true });
    fs.writeFileSync(logPath, JSON.stringify(tools));
  } catch {
    // Capture must never crash a dispatch.
  }
}
