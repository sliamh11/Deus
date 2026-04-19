import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DeusError, RetryableError } from '../errors/index.js';

const logCalls: Array<{ level: string; ctx: unknown; msg: string }> = [];
vi.mock('../logger.js', () => ({
  logger: {
    fatal: (ctx: unknown, msg: string) =>
      logCalls.push({ level: 'fatal', ctx, msg }),
    error: (ctx: unknown, msg: string) =>
      logCalls.push({ level: 'error', ctx, msg }),
    warn: (ctx: unknown, msg: string) =>
      logCalls.push({ level: 'warn', ctx, msg }),
    info: (ctx: unknown, msg: string) =>
      logCalls.push({ level: 'info', ctx, msg }),
  },
}));

const { fireAndForget, withTimeout, allSettledOrThrow } =
  await import('./index.js');

beforeEach(() => {
  logCalls.length = 0;
});
afterEach(() => {
  vi.useRealTimers();
});

describe('fireAndForget', () => {
  it('swallows success silently', async () => {
    fireAndForget(Promise.resolve(42), { name: 'ok' });
    await new Promise((r) => setTimeout(r, 5));
    expect(logCalls).toEqual([]);
  });

  it('routes promise rejection to default logger', async () => {
    fireAndForget(Promise.reject(new Error('boom')), { name: 'work' });
    await new Promise((r) => setTimeout(r, 5));
    expect(logCalls).toHaveLength(1);
    expect(logCalls[0].level).toBe('error');
    expect(logCalls[0].msg).toBe('fireAndForget(work) failed');
    expect(logCalls[0].ctx).toMatchObject({ task: 'work' });
  });

  it('routes rejection to custom onError', async () => {
    const onError = vi.fn();
    fireAndForget(Promise.reject(new Error('boom')), { name: 'work', onError });
    await new Promise((r) => setTimeout(r, 5));
    expect(onError).toHaveBeenCalledOnce();
    expect(logCalls).toEqual([]);
  });

  it('catches synchronous throws from a thunk', async () => {
    const onError = vi.fn();
    fireAndForget(
      () => {
        throw new Error('sync');
      },
      { name: 'work', onError },
    );
    await new Promise((r) => setTimeout(r, 5));
    expect(onError).toHaveBeenCalledOnce();
    expect((onError.mock.calls[0][0] as Error).message).toBe('sync');
  });

  it('handles thunk returning a non-promise', async () => {
    fireAndForget(() => 42, { name: 'ok' });
    await new Promise((r) => setTimeout(r, 5));
    expect(logCalls).toEqual([]);
  });

  it('does not throw if onError itself throws', async () => {
    const bad = () => {
      throw new Error('handler broke');
    };
    expect(() =>
      fireAndForget(Promise.reject(new Error('x')), {
        name: 'w',
        onError: bad,
      }),
    ).not.toThrow();
    await new Promise((r) => setTimeout(r, 5));
  });
});

describe('withTimeout', () => {
  it('resolves when inner completes in time', async () => {
    const value = await withTimeout(Promise.resolve('ok'), 100, {
      name: 'fast',
    });
    expect(value).toBe('ok');
  });

  it('rejects with RetryableError after timeout', async () => {
    const slow = new Promise((resolve) =>
      setTimeout(() => resolve('late'), 100),
    );
    await expect(withTimeout(slow, 10, { name: 'slow' })).rejects.toThrow(
      RetryableError,
    );
  });

  it('timeout error carries name and timeoutMs in context', async () => {
    const slow = new Promise((resolve) => setTimeout(() => resolve('x'), 100));
    try {
      await withTimeout(slow, 5, { name: 'auth.refresh' });
      expect.fail('should have thrown');
    } catch (err) {
      expect(err).toBeInstanceOf(RetryableError);
      expect((err as RetryableError).context).toMatchObject({
        task: 'auth.refresh',
        timeoutMs: 5,
      });
    }
  });

  it('propagates inner rejection before timeout fires', async () => {
    const fails = Promise.reject(new Error('inner'));
    await expect(withTimeout(fails, 1000, { name: 't' })).rejects.toThrow(
      'inner',
    );
  });

  it('honors custom message', async () => {
    const slow = new Promise((resolve) => setTimeout(() => resolve('x'), 100));
    await expect(
      withTimeout(slow, 5, { name: 't', message: 'custom!' }),
    ).rejects.toThrow('custom!');
  });
});

describe('allSettledOrThrow', () => {
  it('returns values when all succeed', async () => {
    const result = await allSettledOrThrow(
      [Promise.resolve(1), Promise.resolve(2), Promise.resolve(3)],
      { name: 'fanout' },
    );
    expect(result).toEqual([1, 2, 3]);
  });

  it("default 'any' policy throws DeusError on first failure", async () => {
    await expect(
      allSettledOrThrow(
        [
          Promise.resolve(1),
          Promise.reject(new Error('x')),
          Promise.resolve(3),
        ],
        { name: 'fanout' },
      ),
    ).rejects.toThrow(DeusError);
  });

  it("'all' policy tolerates partial failure", async () => {
    const result = await allSettledOrThrow(
      [Promise.resolve(1), Promise.reject(new Error('x')), Promise.resolve(3)],
      { name: 'fanout', throwIf: 'all' },
    );
    expect(result).toEqual([1, undefined, 3]);
  });

  it("'all' policy throws when every promise rejects", async () => {
    await expect(
      allSettledOrThrow(
        [Promise.reject(new Error('a')), Promise.reject(new Error('b'))],
        { name: 'fanout', throwIf: 'all' },
      ),
    ).rejects.toThrow(DeusError);
  });

  it('custom predicate gates the throw', async () => {
    // Throw only if majority rejected.
    const majority = (failures: number, total: number) => failures * 2 > total;
    const result = await allSettledOrThrow(
      [Promise.resolve(1), Promise.reject(new Error('x')), Promise.resolve(3)],
      { name: 'fanout', throwIf: majority },
    );
    expect(result).toEqual([1, undefined, 3]);
  });

  it('aggregate error carries per-slot causes', async () => {
    try {
      await allSettledOrThrow(
        [
          Promise.resolve(1),
          Promise.reject(new Error('boom')),
          Promise.resolve(3),
        ],
        { name: 'fanout' },
      );
      expect.fail('should throw');
    } catch (err) {
      expect(err).toBeInstanceOf(DeusError);
      const ctx = (err as DeusError).context as {
        failures: number;
        total: number;
        causes: Array<{ index: number; reason: { message: string } } | null>;
      };
      expect(ctx.failures).toBe(1);
      expect(ctx.total).toBe(3);
      expect(ctx.causes[0]).toBeNull();
      expect(ctx.causes[1]).toMatchObject({
        index: 1,
        reason: { message: 'boom' },
      });
      expect(ctx.causes[2]).toBeNull();
    }
  });

  it('handles empty input', async () => {
    const result = await allSettledOrThrow([], { name: 'empty' });
    expect(result).toEqual([]);
  });
});
