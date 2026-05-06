import type { SubagentTask, SubagentResult } from './types.js';

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
  parts.push('End your response with exactly one of these status markers:');
  parts.push('- [STATUS:DONE] if you completed the task successfully');
  parts.push(
    '- [STATUS:DONE_WITH_CONCERNS:<concern1>;<concern2>] if completed but with concerns',
  );
  parts.push('- [STATUS:BLOCKED:<reason>] if you cannot complete the task');

  return parts.join('\n');
}
