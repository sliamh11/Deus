import { describe, it, expect } from 'vitest';

import { detectUserSignal } from '../user-signal.js';

describe('detectUserSignal', () => {
  it('detects positive keywords', () => {
    expect(detectUserSignal('perfect')).toBe('positive');
    expect(detectUserSignal('exactly')).toBe('positive');
    expect(detectUserSignal('great job')).toBe('positive');
    expect(detectUserSignal('love it')).toBe('positive');
    expect(detectUserSignal('that works')).toBe('positive');
    expect(detectUserSignal('looks good')).toBe('positive');
    expect(detectUserSignal('lgtm')).toBe('positive');
    expect(detectUserSignal('thanks')).toBe('positive');
    expect(detectUserSignal('thank you')).toBe('positive');
  });

  it('detects negative keywords', () => {
    expect(detectUserSignal('wrong')).toBe('negative');
    expect(detectUserSignal('try again')).toBe('negative');
    expect(detectUserSignal('not what i asked')).toBe('negative');
    expect(detectUserSignal('redo')).toBe('negative');
    expect(detectUserSignal('start over')).toBe('negative');
    expect(detectUserSignal('redo this')).toBe('negative');
  });

  it('returns null for messages exceeding MAX_SIGNAL_LENGTH (200)', () => {
    const longMessage = 'perfect ' + 'x'.repeat(200);
    expect(detectUserSignal(longMessage)).toBeNull();
  });

  it('detects signals up to MAX_SIGNAL_LENGTH boundary', () => {
    // 200 chars exactly should still work
    const atBoundary = 'perfect' + ' '.repeat(193);
    expect(atBoundary.length).toBe(200);
    expect(detectUserSignal(atBoundary)).toBe('positive');

    // 201 chars should return null
    const overBoundary = 'perfect' + ' '.repeat(194);
    expect(overBoundary.length).toBe(201);
    expect(detectUserSignal(overBoundary)).toBeNull();
  });

  it('returns null for normal prompts without signal keywords', () => {
    expect(detectUserSignal('hello world')).toBeNull();
    expect(detectUserSignal('what is the weather?')).toBeNull();
  });

  it('is case-insensitive', () => {
    expect(detectUserSignal('PERFECT')).toBe('positive');
    expect(detectUserSignal('WRONG')).toBe('negative');
    expect(detectUserSignal('LGTM')).toBe('positive');
  });

  it('negative takes priority over positive when both present', () => {
    expect(detectUserSignal('perfect but wrong')).toBe('negative');
  });
});
