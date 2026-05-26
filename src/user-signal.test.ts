import { describe, it, expect } from 'vitest';
import { detectUserSignal } from './user-signal.js';

const MAX_SIGNAL_LENGTH = 200;

describe('detectUserSignal', () => {
  // Positive signals
  it('detects "perfect" as positive', () => {
    expect(detectUserSignal('perfect')).toBe('positive');
  });

  it('detects "exactly" as positive', () => {
    expect(detectUserSignal('exactly what I needed')).toBe('positive');
  });

  it('detects "great job" as positive', () => {
    expect(detectUserSignal('great job!')).toBe('positive');
  });

  it('detects "love it" as positive', () => {
    expect(detectUserSignal('love it')).toBe('positive');
  });

  it('detects "nailed it" as positive', () => {
    expect(detectUserSignal('you nailed it')).toBe('positive');
  });

  it('detects "spot on" as positive', () => {
    expect(detectUserSignal('spot on!')).toBe('positive');
  });

  // Negative signals
  it('detects "wrong" as negative', () => {
    expect(detectUserSignal('wrong')).toBe('negative');
  });

  it('detects "try again" as negative', () => {
    expect(detectUserSignal('try again please')).toBe('negative');
  });

  it('detects "not what i wanted" as negative', () => {
    expect(detectUserSignal('not what i wanted')).toBe('negative');
  });

  it('detects "completely wrong" as negative', () => {
    expect(detectUserSignal('completely wrong')).toBe('negative');
  });

  it('returns null for neutral message', () => {
    expect(detectUserSignal('Can you help me with my homework?')).toBeNull();
  });

  it('returns null when message exceeds MAX_SIGNAL_LENGTH', () => {
    const longMessage = 'perfect ' + 'x'.repeat(200);
    expect(longMessage.length).toBeGreaterThan(MAX_SIGNAL_LENGTH);
    expect(detectUserSignal(longMessage)).toBeNull();
  });

  it('returns signal when message is exactly at MAX_SIGNAL_LENGTH', () => {
    const msg = 'wrong' + ' '.repeat(MAX_SIGNAL_LENGTH - 5);
    expect(msg.length).toBe(MAX_SIGNAL_LENGTH);
    expect(detectUserSignal(msg)).toBe('negative');
  });

  it('is case-insensitive for positive keywords', () => {
    expect(detectUserSignal('PERFECT')).toBe('positive');
    expect(detectUserSignal('Great Job')).toBe('positive');
  });

  it('is case-insensitive for negative keywords', () => {
    expect(detectUserSignal('WRONG')).toBe('negative');
  });

  it('returns null for empty string', () => {
    expect(detectUserSignal('')).toBeNull();
  });
});
