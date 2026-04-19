/**
 * Async-boundary helpers — see docs/decisions/error-discipline.md (PR #4 of 10)
 *
 * Three primitives for the three async patterns that keep tripping up Deus:
 *
 *   fireAndForget  — "I intend to ignore this, but log failures with owner"
 *   withTimeout    — "I intend to wait, but not forever"
 *   allSettledOrThrow — "I intend to run these in parallel, fail-policy is X"
 *
 * Why these three: TrueCourse's 16 HIGH floating-promise findings decompose
 * into exactly these patterns. Having named primitives makes the correct
 * pattern the default — the linter (PR #5) can then flag raw floating
 * promises as "use fireAndForget or .then().catch() explicitly".
 */

import { DeusError, RetryableError, isDeusError } from '../errors/index.js';
import { logger } from '../logger.js';

// ── fireAndForget ─────────────────────────────────────────────────────────

export interface FireAndForgetOptions {
  /**
   * Owner of this background task — included in every failure log.
   * Example: `'telegram.reconnect'`, `'gcal.sync'`.
   */
  readonly name: string;

  /**
   * Custom failure handler. Default: logs at `error` level with the owner.
   * Return nothing; errors from the handler itself are swallowed (they
   * can't safely propagate from a fire-and-forget boundary).
   */
  readonly onError?: (err: unknown) => void;
}

/**
 * Mark a promise as intentionally unawaited. Failures are routed to
 * `onError` (default: `logger.error`) so they never vanish.
 *
 * Accepts either a promise or a thunk — the thunk form catches *synchronous*
 * throws inside the factory as well, which a bare promise can't.
 *
 *   fireAndForget(doWork(), { name: 'work' });
 *   fireAndForget(() => doWork(), { name: 'work' }); // safer: catches sync throws
 */
export function fireAndForget(
  work: Promise<unknown> | (() => Promise<unknown> | unknown),
  options: FireAndForgetOptions,
): void {
  const { name, onError = defaultFireAndForgetHandler(name) } = options;

  let promise: Promise<unknown>;
  try {
    promise = typeof work === 'function' ? Promise.resolve(work()) : work;
  } catch (err) {
    // Synchronous throw from the thunk.
    safeCallOnError(onError, err);
    return;
  }

  promise.catch((err: unknown) => {
    safeCallOnError(onError, err);
  });
}

function defaultFireAndForgetHandler(name: string): (err: unknown) => void {
  return (err) =>
    logger.error(
      { err: serializeForLog(err), task: name },
      `fireAndForget(${name}) failed`,
    );
}

function safeCallOnError(handler: (err: unknown) => void, err: unknown): void {
  try {
    handler(err);
  } catch {
    // Onerror itself threw. Nothing safe to do at this boundary — the caller
    // opted into fire-and-forget. Log via pino if possible, else drop.
    try {
      logger.error(
        { err: serializeForLog(err) },
        'fireAndForget onError handler threw',
      );
    } catch {
      /* last-ditch: drop */
    }
  }
}

// ── withTimeout ───────────────────────────────────────────────────────────

export interface WithTimeoutOptions {
  /** Label for the timeout error — e.g. `'oauth.refresh'`. */
  readonly name: string;
  /** Optional message override. Default: `<name> timed out after <ms>ms`. */
  readonly message?: string;
}

/**
 * Race a promise against a deadline. Throws `RetryableError` on timeout
 * (timeouts are almost always transient — retry is the sane default).
 *
 * The underlying promise keeps running after timeout (there's no way to
 * abort a generic Promise). Pass an AbortSignal at the source if you need
 * to cancel the real work.
 */
export function withTimeout<T>(
  promise: Promise<T>,
  ms: number,
  options: WithTimeoutOptions,
): Promise<T> {
  const { name, message = `${name} timed out after ${ms}ms` } = options;

  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      reject(
        new RetryableError(message, { context: { task: name, timeoutMs: ms } }),
      );
    }, ms);

    promise.then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (err: unknown) => {
        clearTimeout(timer);
        reject(err);
      },
    );
  });
}

// ── allSettledOrThrow ─────────────────────────────────────────────────────

export type AllSettledThrowPolicy =
  | 'any'
  | 'all'
  | ((failures: number, total: number) => boolean);

export interface AllSettledOrThrowOptions {
  /** Label for aggregate failure — e.g. `'channel.fanout'`. */
  readonly name: string;
  /**
   * When to throw:
   *   `'any'` — if any promise rejects (like Promise.all but waits for all to settle).
   *   `'all'` — only if every promise rejects (strictest partial-success).
   *   `(failures, total) => boolean` — custom quorum.
   * Default: `'any'`.
   */
  readonly throwIf?: AllSettledThrowPolicy;
}

/**
 * Run promises in parallel; decide failure by policy rather than
 * short-circuit-on-first-reject (Promise.all) or silent-swallow
 * (Promise.allSettled).
 *
 * Returns a dense array of values in input order; failed slots are
 * replaced with `undefined` when the policy allows partial success.
 * The aggregate error carries a `context.causes` list with per-slot
 * reasons for debuggability.
 */
export async function allSettledOrThrow<T>(
  promises: ReadonlyArray<Promise<T>>,
  options: AllSettledOrThrowOptions,
): Promise<Array<T | undefined>> {
  const { name, throwIf = 'any' } = options;

  const settled = await Promise.allSettled(promises);
  const failures = settled.filter((s) => s.status === 'rejected').length;

  if (shouldThrow(throwIf, failures, settled.length)) {
    const causes = settled.map((s, i) =>
      s.status === 'rejected'
        ? { index: i, reason: summarizeCause(s.reason) }
        : null,
    );
    throw new DeusError(`${name}: ${failures}/${settled.length} rejected`, {
      context: { task: name, failures, total: settled.length, causes },
    });
  }

  return settled.map((s) => (s.status === 'fulfilled' ? s.value : undefined));
}

function shouldThrow(
  policy: AllSettledThrowPolicy,
  failures: number,
  total: number,
): boolean {
  if (failures === 0) return false;
  if (policy === 'any') return true;
  if (policy === 'all') return failures === total;
  return policy(failures, total);
}

// ── helpers ───────────────────────────────────────────────────────────────

function serializeForLog(err: unknown): unknown {
  if (isDeusError(err)) return err.toJSON();
  if (err instanceof Error)
    return { name: err.name, message: err.message, stack: err.stack };
  return err;
}

function summarizeCause(err: unknown): unknown {
  if (isDeusError(err)) return { name: err.name, message: err.message };
  if (err instanceof Error) return { name: err.name, message: err.message };
  return err;
}
