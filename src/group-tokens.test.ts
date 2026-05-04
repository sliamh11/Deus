import { describe, it, expect, beforeEach } from 'vitest';
import {
  getOrCreateGroupToken,
  validateGroupToken,
  _clearTokens,
} from './group-tokens.js';

describe('group-tokens', () => {
  beforeEach(() => _clearTokens());

  it('generates a 64-char hex token', () => {
    const token = getOrCreateGroupToken('group-a');
    expect(token).toMatch(/^[0-9a-f]{64}$/);
  });

  it('returns the same token for the same folder', () => {
    const t1 = getOrCreateGroupToken('group-a');
    const t2 = getOrCreateGroupToken('group-a');
    expect(t1).toBe(t2);
  });

  it('returns different tokens for different folders', () => {
    const t1 = getOrCreateGroupToken('group-a');
    const t2 = getOrCreateGroupToken('group-b');
    expect(t1).not.toBe(t2);
  });

  it('uses _anonymous key when folder is undefined', () => {
    const t1 = getOrCreateGroupToken();
    const t2 = getOrCreateGroupToken(undefined);
    expect(t1).toBe(t2);
    expect(t1).toMatch(/^[0-9a-f]{64}$/);
  });

  it('validates a known token and returns its folder', () => {
    const token = getOrCreateGroupToken('my-group');
    expect(validateGroupToken(token)).toBe('my-group');
  });

  it('rejects an unknown token', () => {
    expect(validateGroupToken('not-a-real-token')).toBeNull();
  });

  it('rejects empty string', () => {
    expect(validateGroupToken('')).toBeNull();
  });
});
