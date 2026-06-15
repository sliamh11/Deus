import { describe, it, expect, afterEach } from 'vitest';
import { envPositiveInt } from './env-utils.js';

describe('envPositiveInt (LIA-293)', () => {
  const NAME = 'DEUS_TEST_ENV_POSITIVE_INT';
  const FB = 12345;

  afterEach(() => {
    delete process.env[NAME];
  });

  it('returns the fallback when unset', () => {
    expect(envPositiveInt(NAME, FB)).toBe(FB);
  });

  it('returns a valid positive number', () => {
    process.env[NAME] = '500';
    expect(envPositiveInt(NAME, FB)).toBe(500);
  });

  // The trap this guards: Number('') === 0 and Number('abc') === NaN, either of
  // which would otherwise flow through as a bogus timeout/limit.
  it.each(['', 'abc', '0', '-5', 'NaN'])(
    'falls back on malformed/non-positive value %j',
    (bad) => {
      process.env[NAME] = bad;
      expect(envPositiveInt(NAME, FB)).toBe(FB);
    },
  );
});
