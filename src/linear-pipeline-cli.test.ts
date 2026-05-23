import { describe, it, expect } from 'vitest';
import { parseDuration } from './linear-pipeline-cli.js';

describe('parseDuration', () => {
  it('parses minutes', () => {
    const result = parseDuration('30m');
    expect(result).toBeTruthy();
    const diff = Date.now() - new Date(result!).getTime();
    expect(diff).toBeGreaterThan(29 * 60_000);
    expect(diff).toBeLessThan(31 * 60_000);
  });

  it('parses hours', () => {
    const result = parseDuration('24h');
    expect(result).toBeTruthy();
    const diff = Date.now() - new Date(result!).getTime();
    expect(diff).toBeGreaterThan(23.9 * 3_600_000);
    expect(diff).toBeLessThan(24.1 * 3_600_000);
  });

  it('parses days', () => {
    const result = parseDuration('7d');
    expect(result).toBeTruthy();
    const diff = Date.now() - new Date(result!).getTime();
    expect(diff).toBeGreaterThan(6.9 * 86_400_000);
    expect(diff).toBeLessThan(7.1 * 86_400_000);
  });

  it('returns null for invalid input', () => {
    expect(parseDuration('abc')).toBeNull();
    expect(parseDuration('24')).toBeNull();
    expect(parseDuration('h')).toBeNull();
    expect(parseDuration('')).toBeNull();
  });
});
