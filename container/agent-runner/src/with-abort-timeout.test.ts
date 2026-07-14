import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { withAbortTimeout } from './with-abort-timeout.js';

describe('withAbortTimeout', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('resolves with the operation value on the happy path and clears the timer exactly once', async () => {
    const clearTimeoutSpy = vi.spyOn(global, 'clearTimeout');

    const result = await withAbortTimeout(1000, async () => 'ok');

    expect(result).toBe('ok');
    expect(clearTimeoutSpy).toHaveBeenCalledTimes(1);
  });

  it('keeps the timeout armed for the WHOLE operation, not just until "fetch" resolves — the exact regression this helper exists to prevent', async () => {
    // Models the real call sites: `fetch()` resolves quickly (headers
    // received), then a further async step (`res.json()`) is still pending
    // when the timeout fires. A naive `fetchWithTimeout(): Promise<Response>`
    // that clears its timer as soon as the fetch-equivalent step resolves
    // would leave this second phase unbounded — this test fails against that
    // design (the signal would never abort) and passes against the
    // callback-based design, which keeps the whole operation inside the
    // try/finally.
    let capturedSignal: AbortSignal | undefined;
    let resolveJsonPhase: (() => void) | undefined;

    const operation = vi.fn(async (signal: AbortSignal) => {
      capturedSignal = signal;
      await Promise.resolve(); // "fetch() resolved" — headers received
      await new Promise<void>((resolve) => {
        resolveJsonPhase = resolve; // "res.json() still pending"
      });
      return 'late-value';
    });

    const promise = withAbortTimeout(1000, operation);
    // The operation never observes the abort itself in this test (that's the
    // real call sites' job via fetch(..., {signal})) — swallow so the
    // eventual resolution below doesn't produce an unhandled rejection.
    promise.catch(() => {});

    // Let the operation run up to the point where it's blocked on the
    // "res.json()" phase — i.e. past the point a naive design would already
    // have cleared its timer.
    await vi.advanceTimersByTimeAsync(0);
    expect(capturedSignal).toBeDefined();
    expect(capturedSignal!.aborted).toBe(false);

    // Advance past the timeout while the operation's promise is STILL
    // unresolved.
    await vi.advanceTimersByTimeAsync(1000);

    // The signal must now be aborted — proving the timer stayed live through
    // the second async phase instead of being cleared the moment "fetch"
    // resolved.
    expect(capturedSignal!.aborted).toBe(true);

    resolveJsonPhase?.();
    await expect(promise).resolves.toBe('late-value');
  });

  it('surfaces the abort as a rejection when the operation observes the aborted signal — matching how fetch(..., {signal}) behaves at the real call sites', async () => {
    const clearTimeoutSpy = vi.spyOn(global, 'clearTimeout');

    const operation = (signal: AbortSignal) =>
      new Promise<never>((_resolve, reject) => {
        signal.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'));
        });
      });

    const promise = withAbortTimeout(1000, operation);
    const assertion = expect(promise).rejects.toThrow('Aborted');

    await vi.advanceTimersByTimeAsync(1000);

    await assertion;
    expect(clearTimeoutSpy).toHaveBeenCalledTimes(1);
  });

  it('clears the timer exactly once on a non-abort failure path (operation rejects on its own)', async () => {
    const clearTimeoutSpy = vi.spyOn(global, 'clearTimeout');

    await expect(
      withAbortTimeout(1000, async () => {
        throw new Error('boom');
      }),
    ).rejects.toThrow('boom');

    expect(clearTimeoutSpy).toHaveBeenCalledTimes(1);

    // No dangling timer left behind: advancing past timeoutMs after the
    // operation already settled must not throw or abort anything further.
    await vi.advanceTimersByTimeAsync(1000);
  });
});
