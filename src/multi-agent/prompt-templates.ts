import type { SubagentTask, SubagentResult } from './types.js';

/**
 * Soft turn budget stated in every subagent prompt (LIA-380). Advisory only —
 * nothing enforces it (SDK maxTurns deliberately unwired; a hard cut mid-task
 * is a quality-regression risk). Budget-aware prompting cut agent cost ~31%
 * at comparable accuracy in published benchmarks (arXiv:2511.17006); the
 * local baseline motivating the 15-turn default is in the LIA-380 PR body.
 */
export const SOFT_TURN_BUDGET = 15;

export function buildPrompt(
  task: SubagentTask,
  priorOutputs?: Map<string, SubagentResult>,
): string {
  const parts: string[] = [];

  parts.push(`You are a ${task.role}.`);
  parts.push(`Your goal: ${task.goal}`);
  if (task.backstory) {
    parts.push(`Background: ${task.backstory}`);
  }
  parts.push('');

  // Inject context from prior tasks
  if (task.contextFrom && priorOutputs) {
    for (const depId of task.contextFrom) {
      const dep = priorOutputs.get(depId);
      if (dep && dep.status !== 'BLOCKED') {
        parts.push(`--- Context from ${depId} ---`);
        parts.push(dep.output);
        if (dep.concerns?.length) {
          parts.push(`Concerns from ${depId}: ${dep.concerns.join('; ')}`);
        }
        parts.push('---');
        parts.push('');
      }
    }
  }

  parts.push(task.prompt);
  parts.push('');
  // Budget frames the response; the status contract below stays last. The
  // DONE_WITH_CONCERNS example must match orchestrator.ts STATUS_MARKER_RE —
  // a bare [STATUS:DONE_WITH_CONCERNS] fails the regex and the orchestrator
  // discards the summary with a synthetic "no marker" concern.
  parts.push(
    `Work within a soft budget of ~${SOFT_TURN_BUDGET} turns. If the task needs more, stop and report [STATUS:DONE_WITH_CONCERNS:budget exceeded;<what's done>;<what remains>] — do not grind past the budget.`,
  );
  parts.push('');
  parts.push('End your response with exactly one of these status markers:');
  parts.push('- [STATUS:DONE] if you completed the task successfully');
  parts.push(
    '- [STATUS:DONE_WITH_CONCERNS:<concern1>;<concern2>] if completed but with concerns',
  );
  parts.push('- [STATUS:BLOCKED:<reason>] if you cannot complete the task');

  return parts.join('\n');
}
