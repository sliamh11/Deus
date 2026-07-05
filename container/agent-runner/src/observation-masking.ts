/**
 * Observation masking for Deus-owned message arrays (LIA-378, slice 1).
 *
 * Tool results dominate agent-loop context (measured: 53.5% of final-context
 * tokens in the largest dispatches, re-billed ~47x over a dispatch); masking
 * stale ones matches summarization solve-rates at ~half the cost
 * (arXiv:2508.21433). This module applies ONLY where Deus owns the message
 * history — today the llama-cpp backend. The Claude path has no owned array
 * (see the LIA-378 spike); the openai backend keeps history server-side.
 *
 * Honesty note: masked llama-cpp content is NOT recoverable (the messages
 * array is in-memory only; the LIA-374 cold store covers host transcripts,
 * not container loops). The safety case is: flag OFF by default, keep-window,
 * minBytes floor, and a placeholder that tells the model to re-run the tool.
 *
 * Turn counting matches the adjacent compactMessages() (llama-cpp-backend.ts):
 * a turn is one `role === 'user'` message; tool results attach to the turn
 * they follow. The two windows can never disagree about what "N turns" means.
 */

const DEFAULT_KEEP_TURNS = 3;
const DEFAULT_MIN_BYTES = 500;

/** Replacement text for a masked tool result — no retention claim. */
export const MASKED_PLACEHOLDER =
  '[tool result masked to save context — re-run the tool if its output is needed again]';

/** Kill-switch: OFF by default (dark ship). LIA-378 */
export function maskingEnabled(): boolean {
  return process.env.DEUS_OBSERVATION_MASKING === '1'; // LIA-378
}

export function maskKeepTurns(): number {
  const parsed = Number.parseInt(
    process.env.DEUS_MASK_KEEP_TURNS || '', // LIA-378
    10,
  );
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_KEEP_TURNS;
}

export function maskMinBytes(): number {
  const parsed = Number.parseInt(
    process.env.DEUS_MASK_MIN_BYTES || '', // LIA-378
    10,
  );
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_MIN_BYTES;
}

interface MaskableMessage {
  role: string;
  content?: string | Array<Record<string, unknown>> | null;
}

export interface MaskOpts {
  keepRecentTurns: number;
  minBytes: number;
}

/**
 * Replace stale tool-result contents with MASKED_PLACEHOLDER, in place.
 *
 * A tool message is masked only when ALL hold: it precedes the last
 * `keepRecentTurns` user messages; its content is a string (non-string =
 * fail-open skip); it is at least `minBytes` bytes; and it isn't already the
 * placeholder (idempotent). Never touches non-tool messages.
 */
export function maskStaleToolResults(
  messages: MaskableMessage[],
  opts: MaskOpts,
): { masked: number } {
  const userIndices: number[] = [];
  for (let i = 0; i < messages.length; i += 1) {
    if (messages[i]?.role === 'user') userIndices.push(i);
  }
  if (userIndices.length <= opts.keepRecentTurns) return { masked: 0 };

  // Everything before the first kept user turn is stale.
  const cutoff = userIndices[userIndices.length - opts.keepRecentTurns];
  let masked = 0;
  for (let i = 0; i < cutoff; i += 1) {
    const msg = messages[i];
    if (msg?.role !== 'tool') continue;
    const content = msg.content;
    if (typeof content !== 'string') continue; // fail-open: arrays/null untouched
    if (content === MASKED_PLACEHOLDER) continue; // idempotent
    if (Buffer.byteLength(content, 'utf8') < opts.minBytes) continue;
    msg.content = MASKED_PLACEHOLDER;
    masked += 1;
  }
  return { masked };
}
