/**
 * Reconnect backoff for the WhatsApp socket.
 *
 * Background: a sustained reason-408 ("connection lost") made the old handler
 * reconnect with zero delay — `connectInternal()` resolves as soon as the socket
 * is built, so the 408 arrives later as a `'close'` event and never throws, so
 * the only backoff (in `.catch`) was dead code. The result was a tight reconnect
 * loop (~25/sec; 90k+ reconnects in an hour) that also produced the
 * MaxListenersExceededWarning ("drain listeners on [Socket]") of issue #305.
 *
 * Design: `ReconnectController` is a **single-flight scheduler** (at most one
 * pending reconnect) with **epoch (generation-token) cancellation** so a timer
 * that fires after `reset()` is a no-op, plus an internal **stability timer**
 * that only resets the backoff once a connection has survived `stableMs` — a
 * brief flap does not reset it. All timers/jitter are injectable so the
 * storm-prevention behaviour is unit-testable without a real socket.
 */

export interface BackoffOptions {
  /** First-attempt delay before jitter (default 1s). */
  baseMs?: number;
  /** Maximum delay after exponential growth (default 60s). */
  capMs?: number;
  /** Returns a value in [0,1]; defaults to Math.random. Injected in tests. */
  jitter?: () => number;
}

/**
 * Exponential backoff with full-ish jitter: `min(cap, base * 2**attempt)` scaled
 * to 50–100% of that value. Jitter spreads reconnects so many clients don't
 * retry in lockstep. Negative attempts are clamped to 0.
 */
export function reconnectDelayMs(
  attempt: number,
  opts: BackoffOptions = {},
): number {
  const base = opts.baseMs ?? 1000;
  const cap = opts.capMs ?? 60000;
  const jitter = opts.jitter ?? Math.random;
  const exp = Math.min(cap, base * 2 ** Math.max(0, attempt));
  return Math.round(exp * (0.5 + 0.5 * jitter()));
}

type TimerHandle = ReturnType<typeof setTimeout>;

export interface ReconnectControllerOptions extends BackoffOptions {
  /** A connection must stay open this long before the backoff resets (default 30s). */
  stableMs?: number;
  setTimeoutFn?: (cb: () => void, ms: number) => TimerHandle;
  clearTimeoutFn?: (handle: TimerHandle) => void;
}

export class ReconnectController {
  private attempt = 0;
  private pendingTimer: TimerHandle | null = null;
  private stableTimer: TimerHandle | null = null;
  /** Epoch token; bumped by reset() to invalidate any in-flight pending timer. */
  private generation = 0;

  private readonly baseMs: number;
  private readonly capMs: number;
  private readonly stableMs: number;
  private readonly jitter: () => number;
  private readonly setTimeoutFn: (cb: () => void, ms: number) => TimerHandle;
  private readonly clearTimeoutFn: (handle: TimerHandle) => void;

  constructor(opts: ReconnectControllerOptions = {}) {
    this.baseMs = opts.baseMs ?? 1000;
    this.capMs = opts.capMs ?? 60000;
    this.stableMs = opts.stableMs ?? 30000;
    this.jitter = opts.jitter ?? Math.random;
    this.setTimeoutFn = opts.setTimeoutFn ?? setTimeout;
    this.clearTimeoutFn = opts.clearTimeoutFn ?? clearTimeout;
  }

  /**
   * Schedule one reconnect after the current backoff delay. Single-flight: if a
   * reconnect is already pending, this is a no-op and returns `null`; otherwise
   * it returns the delay it scheduled. `action` must handle its own errors
   * (void-returning, no floating promise).
   */
  schedule(action: () => void): number | null {
    if (this.pendingTimer !== null) return null;
    const delay = reconnectDelayMs(this.attempt, {
      baseMs: this.baseMs,
      capMs: this.capMs,
      jitter: this.jitter,
    });
    this.attempt += 1;
    const gen = this.generation;
    this.pendingTimer = this.setTimeoutFn(() => {
      this.pendingTimer = null;
      if (gen !== this.generation) return; // invalidated by reset()
      action();
    }, delay);
    return delay;
  }

  /**
   * Mark the connection open. Starts a stability window; if the connection is
   * still up after `stableMs`, the backoff resets so the next outage reconnects
   * promptly. A flap that closes before `stableMs` does not reset it.
   */
  markConnected(): void {
    this.clearStable();
    this.stableTimer = this.setTimeoutFn(() => {
      this.stableTimer = null;
      this.attempt = 0;
    }, this.stableMs);
  }

  /** Mark the connection closed: cancel the pending stability window. */
  markDisconnected(): void {
    this.clearStable();
  }

  /**
   * Cancel any pending reconnect and reset the backoff to base. Bumps the epoch
   * so an already-elapsed timer's callback becomes a no-op (it does not await
   * in-flight timers).
   */
  reset(): void {
    this.attempt = 0;
    this.generation += 1;
    this.clearPending();
    this.clearStable();
  }

  /** True while a reconnect is armed (test/inspection helper). */
  get pending(): boolean {
    return this.pendingTimer !== null;
  }

  /** Current backoff attempt count (test/inspection helper). */
  get attempts(): number {
    return this.attempt;
  }

  private clearPending(): void {
    if (this.pendingTimer !== null) {
      this.clearTimeoutFn(this.pendingTimer);
      this.pendingTimer = null;
    }
  }

  private clearStable(): void {
    if (this.stableTimer !== null) {
      this.clearTimeoutFn(this.stableTimer);
      this.stableTimer = null;
    }
  }
}
