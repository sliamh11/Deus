import { describe, it, expect } from 'vitest';
import { estimateTokens, sumTokens } from './token-counter.js';

describe('estimateTokens', () => {
  it('returns 0 for empty string', () => {
    expect(estimateTokens('')).toBe(0);
  });

  it('returns floor(length / 4)', () => {
    expect(estimateTokens('abcd')).toBe(1);
    expect(estimateTokens('abc')).toBe(0); // floor(3/4) = 0
    expect(estimateTokens('abcde')).toBe(1); // floor(5/4) = 1
  });

  it('handles longer strings', () => {
    expect(estimateTokens('a'.repeat(100))).toBe(25);
  });

  it('handles unicode characters', () => {
    // len('שלום') = 4 chars → 1 token
    expect(estimateTokens('שלום')).toBe(1);
  });
});

describe('sumTokens', () => {
  it('returns 0 with no arguments', () => {
    expect(sumTokens()).toBe(0);
  });

  it('sums a single part', () => {
    expect(sumTokens('abcd')).toBe(1);
  });

  it('sums multiple parts', () => {
    // 'aaaa' = 1, 'bbbbbbbb' = 2
    expect(sumTokens('aaaa', 'bbbbbbbb')).toBe(3);
  });

  it('handles empty parts', () => {
    expect(sumTokens('abcd', '', 'efgh')).toBe(2);
  });
});
