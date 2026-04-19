import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { FatalError, RetryableError } from './errors/index.js';

// Mock logger before importing bootstrap so it captures calls.
const logCalls: Array<{ level: string; ctx: unknown; msg: string }> = [];
vi.mock('./logger.js', () => ({
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

const { bootstrap, __resetBootstrapForTesting } =
  await import('./bootstrap.js');

// Silence actual process.exit during tests — jsdom/node would kill the runner.
let exitSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  logCalls.length = 0;
  __resetBootstrapForTesting();
  exitSpy = vi.spyOn(process, 'exit').mockImplementation(((code?: number) => {
    throw new Error(`process.exit(${code})`);
  }) as never);
});

afterEach(() => {
  exitSpy.mockRestore();
  __resetBootstrapForTesting();
});

async function waitForLog(
  predicate: () => boolean,
  timeoutMs = 500,
): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((r) => setTimeout(r, 5));
  }
  throw new Error(`timeout waiting for log; saw: ${JSON.stringify(logCalls)}`);
}

describe('bootstrap', () => {
  it('runs mainFn to completion without exiting on success', async () => {
    const main = vi.fn(async () => {});
    bootstrap(main, { name: 'test' });
    await new Promise((r) => setTimeout(r, 20));
    expect(main).toHaveBeenCalledOnce();
    expect(exitSpy).not.toHaveBeenCalled();
  });

  it('logs at error level and exits when mainFn rejects with plain Error', async () => {
    const main = async () => {
      throw new Error('boom');
    };
    bootstrap(main, { name: 'entry-a' });
    await waitForLog(() => logCalls.length > 0);
    expect(logCalls[0].level).toBe('error');
    expect(logCalls[0].msg).toBe('entry-a main() failed');
    expect(logCalls[0].ctx).toMatchObject({ entry: 'entry-a' });
    expect(exitSpy).toHaveBeenCalledWith(1);
  });

  it('logs at fatal level when mainFn rejects with FatalError', async () => {
    const main = async () => {
      throw new FatalError('corrupt db');
    };
    bootstrap(main, { name: 'entry-b' });
    await waitForLog(() => logCalls.length > 0);
    expect(logCalls[0].level).toBe('fatal');
    expect(logCalls[0].msg).toBe('entry-b main() failed');
  });

  it('respects custom exitCode', async () => {
    const main = async () => {
      throw new Error('x');
    };
    bootstrap(main, { name: 'entry-c', exitCode: 42 });
    await waitForLog(() => logCalls.length > 0);
    expect(exitSpy).toHaveBeenCalledWith(42);
  });

  it('treats non-FatalError Deus errors (e.g. RetryableError) at error level', async () => {
    const main = async () => {
      throw new RetryableError('rate limited');
    };
    bootstrap(main, { name: 'entry-d' });
    await waitForLog(() => logCalls.length > 0);
    expect(logCalls[0].level).toBe('error');
  });

  it('installs global uncaughtException handler that logs + exits', async () => {
    bootstrap(async () => {}, { name: 'entry-e' });
    await new Promise((r) => setTimeout(r, 20));

    const listeners = process.listeners('uncaughtException');
    expect(listeners.length).toBeGreaterThanOrEqual(1);

    const err = new FatalError('synthetic');
    expect(() =>
      (listeners[listeners.length - 1] as (e: Error) => void)(err),
    ).toThrow(/process\.exit/);
    expect(
      logCalls.some(
        (c) => c.level === 'fatal' && c.msg === 'uncaughtException',
      ),
    ).toBe(true);
  });

  it('installs unhandledRejection handler that logs; no exit by default', async () => {
    bootstrap(async () => {}, { name: 'entry-f' });
    await new Promise((r) => setTimeout(r, 20));

    const listeners = process.listeners('unhandledRejection');
    expect(listeners.length).toBeGreaterThanOrEqual(1);

    (listeners[listeners.length - 1] as (r: unknown) => void)(new Error('rej'));
    expect(
      logCalls.some(
        (c) => c.level === 'error' && c.msg === 'unhandledRejection',
      ),
    ).toBe(true);
    expect(exitSpy).not.toHaveBeenCalled();
  });

  it('exitOnUnhandledRejection=true terminates the process', async () => {
    bootstrap(async () => {}, {
      name: 'entry-g',
      exitOnUnhandledRejection: true,
    });
    await new Promise((r) => setTimeout(r, 20));

    const listeners = process.listeners('unhandledRejection');
    expect(() =>
      (listeners[listeners.length - 1] as (r: unknown) => void)('oops'),
    ).toThrow(/process\.exit/);
  });

  it('installs global handlers only once even across multiple bootstrap() calls', async () => {
    const before = process.listeners('uncaughtException').length;
    bootstrap(async () => {}, { name: 'e1' });
    bootstrap(async () => {}, { name: 'e2' });
    bootstrap(async () => {}, { name: 'e3' });
    await new Promise((r) => setTimeout(r, 20));
    const after = process.listeners('uncaughtException').length;
    expect(after - before).toBe(1);
  });
});
