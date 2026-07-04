import { describe, it, expect } from 'vitest';
import { SUBAGENT_NUDGE, subagentNudgeAppend } from './subagent-nudge.js';

describe('subagentNudgeAppend', () => {
  it('appends the nudge in engineering context (enabled + project + full profile)', () => {
    expect(
      subagentNudgeAppend({
        enabled: true,
        hasProject: true,
        toolProfile: 'full',
      }),
    ).toBe(SUBAGENT_NUDGE);
  });

  it('is silent when the kill-switch is off', () => {
    expect(
      subagentNudgeAppend({
        enabled: false,
        hasProject: true,
        toolProfile: 'full',
      }),
    ).toBe('');
  });

  it('is silent on plain chat (no project mounted)', () => {
    expect(
      subagentNudgeAppend({
        enabled: true,
        hasProject: false,
        toolProfile: 'full',
      }),
    ).toBe('');
  });

  it('is silent for the webhook profile (no Task tool available)', () => {
    expect(
      subagentNudgeAppend({
        enabled: true,
        hasProject: true,
        toolProfile: 'webhook',
      }),
    ).toBe('');
  });

  it('nudge text references the Task tool and carries the negative-scope clause', () => {
    expect(SUBAGENT_NUDGE).toContain('Task');
    expect(SUBAGENT_NUDGE).toContain('Do NOT spawn');
  });

  it('nudge text instructs a soft budget in each dispatch prompt (LIA-380)', () => {
    expect(SUBAGENT_NUDGE).toContain('soft budget');
    expect(SUBAGENT_NUDGE).toContain(
      'return partial findings plus what remains',
    );
  });
});
