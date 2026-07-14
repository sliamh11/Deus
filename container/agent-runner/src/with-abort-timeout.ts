/**
 * Shared fetch-timeout primitive. Callback-based (not `Promise<Response>`) so
 * the timer stays armed for the caller's WHOLE operation, including
 * response-body parsing — a naive helper that clears its timer as soon as
 * `fetch()` resolves would leave a slow `res.json()` unbounded (see
 * with-abort-timeout.test.ts for the regression this avoids).
 */
export async function withAbortTimeout<T>(
  timeoutMs: number,
  operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await operation(controller.signal);
  } finally {
    clearTimeout(timer);
  }
}
