/**
 * Deus error taxonomy — see `docs/decisions/error-discipline.md`.
 *
 * Four disjoint error classes. Choose the one that answers "what should the
 * caller do?":
 *
 *   RetryableError → retry with backoff (transient: network, rate-limit, lock contention)
 *   UserError      → surface message to user, do not log at error level (bad input, auth)
 *   FatalError     → log + shut down this boundary (unrecoverable: corrupt state, missing config)
 *   DeusError      → base class; throw directly only when none of the three fits
 *
 * Every class preserves `cause` (ES2022 Error.cause) so the original stack is never lost.
 */

export interface ErrorContext {
  readonly [key: string]: unknown;
}

export interface DeusErrorOptions {
  /** Wrapped underlying error — preserves stack chain. */
  readonly cause?: unknown;
  /** Structured fields attached to the error for logging. Avoid secrets. */
  readonly context?: ErrorContext;
}

/**
 * Base class for every error raised by Deus code.
 * Prefer a concrete subclass (RetryableError / UserError / FatalError) when possible.
 */
export class DeusError extends Error {
  readonly context: ErrorContext;

  constructor(message: string, options: DeusErrorOptions = {}) {
    super(
      message,
      options.cause !== undefined ? { cause: options.cause } : undefined,
    );
    this.name = new.target.name;
    this.context = options.context ?? {};
    // Preserve prototype across transpilation targets (`instanceof` works).
    Object.setPrototypeOf(this, new.target.prototype);
  }

  /**
   * Serialize to a log-friendly shape. Flattens cause chain into an array.
   * Does not include the stack by default — add it at the log sink if needed.
   */
  toJSON(): Record<string, unknown> {
    return {
      name: this.name,
      message: this.message,
      context: this.context,
      cause: serializeCause(this.cause),
    };
  }
}

/**
 * Transient failure — the caller should retry with backoff.
 * Examples: HTTP 5xx, ECONNRESET, SQLite BUSY, provider rate limit.
 */
export class RetryableError extends DeusError {}

/**
 * Non-recoverable — the caller should give up on this operation and
 * surface the failure. Examples: invalid config, corrupt DB, missing required secret.
 */
export class FatalError extends DeusError {}

/**
 * Caused by user input or action. Not a bug in Deus.
 * Log at `warn`/`info`, not `error`. Message is safe to show to the user.
 */
export class UserError extends DeusError {}

function serializeCause(cause: unknown): unknown {
  if (cause === undefined) return undefined;
  if (cause instanceof DeusError) return cause.toJSON();
  if (cause instanceof Error) {
    return { name: cause.name, message: cause.message };
  }
  return cause;
}

/**
 * Type guard — true if `err` is any Deus error class.
 * Use this at log/metric sinks to switch on taxonomy.
 */
export function isDeusError(err: unknown): err is DeusError {
  return err instanceof DeusError;
}
