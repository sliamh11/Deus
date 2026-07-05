/**
 * Read-discipline nudge (LIA-379).
 *
 * Read is the dominant tool-result cost: 81.6% of tool-result bytes in the 20
 * largest subagent transcripts (mean 12,086 B/call), and every byte is re-read
 * on each subsequent turn of the session. This appends a short, static
 * instruction steering the model toward grep-then-slice (offset/limit) reads by
 * default, reserving whole-file reads for tasks that genuinely need them.
 * Advisory only — no truncation, no forced tool behavior change. Pure module,
 * mirroring `subagent-nudge.ts`.
 */

/**
 * The instruction appended to the system prompt in engineering context.
 * Own cost: ~150 tokens, once per engineering-context turn in the cached
 * system-prompt prefix — vs the mean 12KB (~3k-token) full Reads it deters.
 */
export const READ_DISCIPLINE_NUDGE = `## Read discipline

Reading a file re-bills its full byte size on every later turn of the session.
Before a Read, prefer this order:
  1. When searching for something specific, Grep for the symbol/string first,
     then Read only the surrounding lines with offset/limit.
  2. If you already read a file this session and it hasn't changed, don't
     re-read it — refer back to what you saw.
  3. Read a file in full only when the task genuinely needs the whole thing:
     a small config, a file you must edit end-to-end, or a review that
     requires complete context.
This restates "Context hygiene" — full Reads out of habit are the single
largest token cost in agent sessions; a slice usually does.`;

export interface ReadDisciplineNudgeOpts {
  /** Runtime kill-switch (DEUS_READ_DISCIPLINE_NUDGE !== '0'); read at the call site. */
  enabled: boolean;
  /**
   * Engineering context — an external project is mounted at /workspace/project.
   * Unlike subagent-nudge, NOT gated on toolProfile: Read exists on both the
   * 'full' and 'webhook' profiles, and the measured problem is file-reading
   * volume, not the Task tool.
   */
  hasProject: boolean;
}

/**
 * Returns the nudge text when it should be appended, else an empty string.
 *
 * Appended only when BOTH hold:
 *  - `enabled`    — the kill-switch is on (default)
 *  - `hasProject` — engineering context (plain chat has no project files worth
 *                   disciplining reads over, so it pays no tokens)
 */
export function readDisciplineNudgeAppend(
  opts: ReadDisciplineNudgeOpts,
): string {
  const { enabled, hasProject } = opts;
  if (!enabled || !hasProject) return '';
  return READ_DISCIPLINE_NUDGE;
}
