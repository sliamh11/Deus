import { describe, it, expect } from 'vitest';

import { reconnectDelayMs, ReconnectController } from './reconnect-backoff.js';

/**
 * Deterministic fake clock so backoff timing is asserted without real delays.
 * `clearBehavesAsNoop` simulates the race where a timer has already elapsed and
 * clearTimeout can no longer stop it — used to exercise the epoch-cancellation guard.
 */
class FakeClock {
  private cbs: Array<{ id: number; cb: () => void; due: number }> = [];
  private now = 0;
  private nextId = 1;
  constructor(private readonly clearBehavesAsNoop = false) {}

  setTimeoutFn = (
    cb: () => void,
    ms: number,
  ): ReturnType<typeof setTimeout> => {
    const id = this.nextId++;
    this.cbs.push({ id, cb, due: this.now + ms });
    return id as unknown as ReturnType<typeof setTimeout>;
  };

  clearTimeoutFn = (handle: ReturnType<typeof setTimeout>): void => {
    if (this.clearBehavesAsNoop) return;
    const id = handle as unknown as number;
    this.cbs = this.cbs.filter((c) => c.id !== id);
  };

  /** Advance time, firing every callback whose deadline has passed (in order). */
  advance(ms: number): void {
    this.now += ms;
    // Loop so callbacks that schedule further timers within the window still fire.
    for (;;) {
      const due = this.cbs
        .filter((c) => c.due <= this.now)
        .sort((a, b) => a.due - b.due);
      if (due.length === 0) break;
      this.cbs = this.cbs.filter((c) => c.due > this.now);
      for (const c of due) c.cb();
    }
  }

  get pendingCount(): number {
    return this.cbs.length;
  }
}

const opts = (clock: FakeClock, jitter: () => number = () => 1) => ({
  baseMs: 1000,
  capMs: 60000,
  stableMs: 30000,
  jitter,
  setTimeoutFn: clock.setTimeoutFn,
  clearTimeoutFn: clock.clearTimeoutFn,
});

describe('reconnectDelayMs', () => {
  it('attempt 0 with full jitter is the base delay', () => {
    expect(reconnectDelayMs(0, { jitter: () => 1 })).toBe(1000);
  });

  it('jitter floor is 50% of the exponential value', () => {
    expect(reconnectDelayMs(0, { jitter: () => 0 })).toBe(500);
  });

  it('grows exponentially with the attempt', () => {
    const d1 = reconnectDelayMs(1, { jitter: () => 1 });
    const d3 = reconnectDelayMs(3, { jitter: () => 1 });
    expect(d1).toBe(2000);
    expect(d3).toBe(8000);
    expect(d3).toBeGreaterThan(d1);
  });

  it('caps at capMs no matter how large the attempt', () => {
    expect(reconnectDelayMs(100, { jitter: () => 1 })).toBe(60000);
  });

  it('stays within [50%, 100%] of the uncapped exponential', () => {
    for (const attempt of [0, 1, 2, 5]) {
      const exp = Math.min(60000, 1000 * 2 ** attempt);
      expect(
        reconnectDelayMs(attempt, { jitter: () => 0.5 }),
      ).toBeGreaterThanOrEqual(Math.round(exp * 0.5));
      expect(
        reconnectDelayMs(attempt, { jitter: () => 1 }),
      ).toBeLessThanOrEqual(exp);
    }
  });

  it('treats negative attempts as 0', () => {
    expect(reconnectDelayMs(-5, { jitter: () => 1 })).toBe(1000);
  });
});

describe('ReconnectController — single-flight (the storm regression)', () => {
  it('100 rapid schedules arm exactly one reconnect', () => {
    const clock = new FakeClock();
    const c = new ReconnectController(opts(clock));
    let calls = 0;
    const action = () => {
      calls += 1;
    };

    const first = c.schedule(action);
    let nullReturns = 0;
    for (let i = 0; i < 99; i++) {
      if (c.schedule(action) === null) nullReturns += 1;
    }

    expect(first).toBe(1000); // first schedule returns the delay
    expect(nullReturns).toBe(99); // every redundant call is suppressed
    expect(clock.pendingCount).toBe(1); // only one timer armed

    clock.advance(1000);
    expect(calls).toBe(1); // exactly one reconnect, not 100
  });
});

describe('ReconnectController — backoff growth', () => {
  it('each successive reconnect waits longer, up to the cap', () => {
    const clock = new FakeClock();
    const c = new ReconnectController(opts(clock));
    const noop = () => {};

    const delays: number[] = [];
    for (let i = 0; i < 8; i++) {
      const d = c.schedule(noop);
      expect(d).not.toBeNull();
      delays.push(d as number);
      clock.advance(d as number); // let it fire so the next schedule is allowed
    }

    expect(delays.slice(0, 4)).toEqual([1000, 2000, 4000, 8000]);
    expect(delays[delays.length - 1]).toBe(60000); // capped
    for (let i = 1; i < delays.length; i++) {
      expect(delays[i]).toBeGreaterThanOrEqual(delays[i - 1]);
    }
  });
});

describe('ReconnectController — stability resets backoff only after a stable window', () => {
  it('resets backoff after the connection survives stableMs', () => {
    const clock = new FakeClock();
    const c = new ReconnectController(opts(clock));
    const noop = () => {};

    c.schedule(noop);
    clock.advance(1000);
    c.schedule(noop);
    clock.advance(2000); // attempts now advanced
    expect(c.attempts).toBe(2);

    c.markConnected();
    clock.advance(30000); // survives the stability window
    expect(c.attempts).toBe(0);

    expect(c.schedule(noop)).toBe(1000); // back to base delay
  });

  it('a flap shorter than stableMs does NOT reset backoff', () => {
    const clock = new FakeClock();
    const c = new ReconnectController(opts(clock));
    const noop = () => {};

    c.schedule(noop);
    clock.advance(1000);
    c.schedule(noop);
    clock.advance(2000);
    expect(c.attempts).toBe(2);

    c.markConnected();
    clock.advance(5000); // open only 5s …
    c.markDisconnected(); // … then drops — not stable
    expect(c.attempts).toBe(2); // backoff preserved

    expect(c.schedule(noop)).toBe(4000); // continues from attempt 2
  });
});

describe('ReconnectController — reset()', () => {
  it('cancels the pending reconnect and returns the next delay to base', () => {
    const clock = new FakeClock();
    const c = new ReconnectController(opts(clock));
    let calls = 0;

    c.schedule(() => {
      calls += 1;
    });
    expect(clock.pendingCount).toBe(1);

    c.reset();
    expect(clock.pendingCount).toBe(0); // pending timer cleared
    clock.advance(60000);
    expect(calls).toBe(0); // never fired

    expect(c.schedule(() => {})).toBe(1000); // backoff reset to base
  });

  it('epoch guard: a timer that fires after reset() does not run its action', () => {
    // clearTimeout is a no-op here → the timer WILL fire despite reset(),
    // exercising the generation/epoch invalidation guard.
    const clock = new FakeClock(true);
    const c = new ReconnectController(opts(clock));
    let calls = 0;

    c.schedule(() => {
      calls += 1;
    });
    c.reset(); // increments epoch; clearTimeout no-ops so the timer remains armed
    clock.advance(1000); // stale timer fires …

    expect(calls).toBe(0); // … but the epoch guard suppresses the action
  });
});
