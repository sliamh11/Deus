import { describe, expect, it } from 'vitest';
import { buildPrompt, SOFT_TURN_BUDGET } from './prompt-templates.js';
import type { SubagentTask } from './types.js';

// Mirrors STATUS_MARKER_RE in orchestrator.ts:32-33 (not exported). If the
// orchestrator contract changes shape, the budget text's example marker must
// change with it — this assertion is the drift alarm.
const STATUS_MARKER_RE =
  /\[STATUS:(DONE_WITH_CONCERNS:[^\]]*|DONE|BLOCKED:[^\]]*)\]/;

function task(): SubagentTask {
  return {
    id: 't1',
    role: 'researcher',
    goal: 'find things',
    backstory: '',
    prompt: 'Find the things.',
    mode: 'read',
  };
}

describe('buildPrompt soft turn budget (LIA-380)', () => {
  it('includes the soft budget block with the configured turn count', () => {
    const prompt = buildPrompt(task());
    expect(prompt).toContain(`soft budget of ~${SOFT_TURN_BUDGET} turns`);
    expect(prompt).toContain('do not grind past the budget');
  });

  it('places the budget block after the task prompt and before the status contract', () => {
    const prompt = buildPrompt(task());
    const promptIdx = prompt.indexOf('Find the things.');
    const budgetIdx = prompt.indexOf('soft budget');
    const statusIdx = prompt.indexOf('End your response with exactly one');
    expect(promptIdx).toBeGreaterThanOrEqual(0);
    expect(budgetIdx).toBeGreaterThan(promptIdx);
    expect(statusIdx).toBeGreaterThan(budgetIdx);
  });

  it('budget text cites a DONE_WITH_CONCERNS example that satisfies the orchestrator marker regex', () => {
    const prompt = buildPrompt(task());
    const budgetLine = prompt
      .split('\n')
      .find((l) => l.includes('soft budget'));
    expect(budgetLine).toBeDefined();
    const match = budgetLine!.match(STATUS_MARKER_RE);
    expect(match).not.toBeNull();
    expect(match![1].startsWith('DONE_WITH_CONCERNS:')).toBe(true);
  });

  it('keeps the existing status-marker instructions intact', () => {
    const prompt = buildPrompt(task());
    expect(prompt).toContain('[STATUS:DONE]');
    expect(prompt).toContain(
      '[STATUS:DONE_WITH_CONCERNS:<concern1>;<concern2>]',
    );
    expect(prompt).toContain('[STATUS:BLOCKED:<reason>]');
  });
});
