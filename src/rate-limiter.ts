/**
 * Shared sliding-window rate limiter (dedup of 3 independent implementations:
 * credential-proxy.ts, odysseus-server.ts, ingress/gateway.ts — the first two
 * commented "mirrors credential-proxy.ts" but were never consolidated).
 *
 * `Map<string, number[]>` timestamp-array shape, matching all 3 prior
 * implementations. `opts.cleanupInterval` is opt-in (defaults false) — callers
 * that already run a periodic prune (credential-proxy.ts, odysseus-server.ts)
 * ask for one; gateway.ts relies on inline pruning during `isRateLimited` only,
 * matching its current no-interval behavior exactly.
 */
export function createRateLimiter(
  max: number,
  windowMs: number,
  opts?: { cleanupInterval?: boolean },
): {
  isRateLimited(key: string, now?: number): boolean;
  dispose(): void;
  resetForTest(): void;
} {
  const buckets = new Map<string, number[]>();

  let cleanupTimer: ReturnType<typeof setInterval> | undefined;
  if (opts?.cleanupInterval) {
    cleanupTimer = setInterval(() => {
      const now = Date.now();
      for (const [key, ts] of buckets) {
        const kept = ts.filter((t) => now - t < windowMs);
        if (kept.length === 0) buckets.delete(key);
        else buckets.set(key, kept);
      }
    }, windowMs);
    // Prevent the cleanup timer from keeping Node alive after tests/shutdown.
    cleanupTimer.unref();
  }

  function isRateLimited(key: string, now: number = Date.now()): boolean {
    const ts = (buckets.get(key) ?? []).filter((t) => now - t < windowMs);
    if (ts.length >= max) {
      buckets.set(key, ts);
      return true;
    }
    ts.push(now);
    buckets.set(key, ts);
    return false;
  }

  function dispose(): void {
    if (cleanupTimer) {
      clearInterval(cleanupTimer);
      cleanupTimer = undefined;
    }
  }

  function resetForTest(): void {
    buckets.clear();
  }

  return { isRateLimited, dispose, resetForTest };
}
