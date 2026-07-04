import { describe, expect, it } from 'vitest';
import {
  READ_DISCIPLINE_NUDGE,
  readDisciplineNudgeAppend,
} from './read-discipline-nudge.js';

describe('readDisciplineNudgeAppend', () => {
  it('is silent when the kill-switch is off', () => {
    expect(
      readDisciplineNudgeAppend({ enabled: false, hasProject: true }),
    ).toBe('');
  });

  it('is silent on plain chat (no project mounted)', () => {
    expect(
      readDisciplineNudgeAppend({ enabled: true, hasProject: false }),
    ).toBe('');
  });

  it('appends the nudge in engineering context', () => {
    expect(readDisciplineNudgeAppend({ enabled: true, hasProject: true })).toBe(
      READ_DISCIPLINE_NUDGE,
    );
  });

  it('nudge text keeps its load-bearing guidance', () => {
    expect(READ_DISCIPLINE_NUDGE).toContain('offset/limit');
    expect(READ_DISCIPLINE_NUDGE).toContain('Grep');
    expect(READ_DISCIPLINE_NUDGE).toContain('Read a file in full only when');
  });
});
