/**
 * Layer-A subagent fan-out nudge (LIA-343).
 *
 * Modern Opus under-reaches for parallel subagents by default — it holds the
 * Task tools but won't delegate unless told WHEN it's worth it. This appends a
 * short, static instruction to the agent's system prompt in engineering context
 * (see `subagentNudgeAppend`) telling it when to fan out and when not to. The
 * model owns both the fan-out decision and the synthesis in one context — no
 * host planner or coordination. Pure module, mirroring `computeTeamsNeeded`.
 */

/** The instruction appended to the system prompt in engineering context. */
export const SUBAGENT_NUDGE = `## Parallel subagents (Task tool)

You have Task/TaskOutput/TaskStop. Use them when a request fans out across
INDEPENDENT items that can run at the same time:
  - comparing or evaluating multiple candidates
  - reading or auditing several files/sources whose findings don't depend on each other
  - gathering from multiple distinct sources before you synthesize

Rule of thumb: worth spawning at ~3+ independent items; for 1-2, inline tools are
faster. Cap concurrent subagents at ~5-6 — more just serialize and add overhead.
When you fan out, give each subagent ONLY the context it needs and one clear
deliverable, run them in parallel in a single turn, then YOU read all results and
write one coherent answer. Synthesis is your job, not theirs. State an explicit
soft budget in each subagent's prompt ("about N tool calls / M turns; if you hit
it, return partial findings plus what remains") scaled to the deliverable — an
unbounded dispatch tends to keep exploring well past diminishing returns.

Do NOT spawn a subagent for:
  - a single-file read, a single tool call, or a quick lookup
  - sequential/dependent steps where each needs the previous one's result
  - synthesis-heavy work needing one consistent voice (a plan, an essay, a refactor)
  - work from inside a subagent — only the top-level agent fans out
For those, work directly — a subagent adds latency and cost with no benefit.`;

export interface SubagentNudgeOpts {
  /** Runtime kill-switch (DEUS_SUBAGENT_NUDGE !== '0'); read at the call site. */
  enabled: boolean;
  /** Engineering context — an external project is mounted at /workspace/project. */
  hasProject: boolean;
  /** Tool profile: 'webhook' runs exclude the Task tool entirely (LIA-315). */
  toolProfile: 'full' | 'webhook';
}

/**
 * Returns the nudge text when it should be appended, else an empty string.
 *
 * Appended only when ALL hold:
 *  - `enabled`            — the kill-switch is on (default)
 *  - `hasProject`         — engineering context (plain chat has no project, so it
 *                           pays no tokens and its behavior is unchanged)
 *  - `toolProfile==='full'` — the webhook profile has no Task tool, so nudging it
 *                           toward Task would be incoherent
 */
export function subagentNudgeAppend(opts: SubagentNudgeOpts): string {
  const { enabled, hasProject, toolProfile } = opts;
  if (!enabled || !hasProject || toolProfile !== 'full') return '';
  return SUBAGENT_NUDGE;
}
