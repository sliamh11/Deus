import { describe, it, expect, vi, afterEach } from 'vitest';
import { createRateLimiter } from './rate-limiter.js';

describe('createRateLimiter', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('allows requests within the window up to max, then blocks', () => {
    const limiter = createRateLimiter(3, 60_000);
    const now = 1_000_000;
    expect(limiter.isRateLimited('a', now)).toBe(false);
    expect(limiter.isRateLimited('a', now)).toBe(false);
    expect(limiter.isRateLimited('a', now)).toBe(false);
    // 4th request within the same window is blocked.
    expect(limiter.isRateLimited('a', now)).toBe(true);
  });

  it('allows a request once earlier timestamps fall outside the window', () => {
    const limiter = createRateLimiter(2, 60_000);
    const t0 = 1_000_000;
    expect(limiter.isRateLimited('a', t0)).toBe(false);
    expect(limiter.isRateLimited('a', t0)).toBe(false);
    expect(limiter.isRateLimited('a', t0)).toBe(true);
    // Advance past the window — the two earlier timestamps are now expired.
    const t1 = t0 + 60_001;
    expect(limiter.isRateLimited('a', t1)).toBe(false);
  });

  it('tracks separate keys independently', () => {
    const limiter = createRateLimiter(1, 60_000);
    const now = 1_000_000;
    expect(limiter.isRateLimited('a', now)).toBe(false);
    expect(limiter.isRateLimited('a', now)).toBe(true);
    // A different key has its own bucket.
    expect(limiter.isRateLimited('b', now)).toBe(false);
  });

  it('defaults `now` to Date.now() when omitted', () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const limiter = createRateLimiter(1, 60_000);
    expect(limiter.isRateLimited('a')).toBe(false);
    expect(limiter.isRateLimited('a')).toBe(true);
  });

  it('cleanupInterval: true creates an interval that dispose() clears', () => {
    vi.useFakeTimers();
    const setIntervalSpy = vi.spyOn(global, 'setInterval');
    const clearIntervalSpy = vi.spyOn(global, 'clearInterval');

    const limiter = createRateLimiter(5, 60_000, { cleanupInterval: true });
    expect(setIntervalSpy).toHaveBeenCalledTimes(1);
    expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 60_000);

    limiter.dispose();
    expect(clearIntervalSpy).toHaveBeenCalledTimes(1);
  });

  it('cleanupInterval: false (default) creates no interval', () => {
    const setIntervalSpy = vi.spyOn(global, 'setInterval');
    const limiter = createRateLimiter(5, 60_000);
    expect(setIntervalSpy).not.toHaveBeenCalled();
    // dispose() is still safe to call (no-op) even though no interval exists.
    expect(() => limiter.dispose()).not.toThrow();
  });

  it('the cleanup interval prunes expired entries across buckets', () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
    const limiter = createRateLimiter(1, 60_000, { cleanupInterval: true });
    expect(limiter.isRateLimited('a', Date.now())).toBe(false);
    expect(limiter.isRateLimited('a', Date.now())).toBe(true);

    // Advance past the window and let the prune interval fire.
    vi.setSystemTime(1_000_000 + 60_001);
    vi.advanceTimersByTime(60_000);

    // The bucket was pruned, so a fresh request at the current time succeeds.
    expect(limiter.isRateLimited('a', Date.now())).toBe(false);
    limiter.dispose();
  });

  it('resetForTest() clears all bucket state', () => {
    const limiter = createRateLimiter(1, 60_000);
    const now = 1_000_000;
    expect(limiter.isRateLimited('a', now)).toBe(false);
    expect(limiter.isRateLimited('a', now)).toBe(true);
    limiter.resetForTest();
    expect(limiter.isRateLimited('a', now)).toBe(false);
  });

  it('dispose() is idempotent — safe to call multiple times', () => {
    vi.useFakeTimers();
    const limiter = createRateLimiter(5, 60_000, { cleanupInterval: true });
    expect(() => {
      limiter.dispose();
      limiter.dispose();
      limiter.dispose();
    }).not.toThrow();
  });
});
