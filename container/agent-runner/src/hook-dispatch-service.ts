/**
 * HookDispatchService — standalone HTTP server on :3002 that receives
 * pre/post tool-use events and fans them out to registered observer callbacks.
 *
 * Enabled: HOOK_DISPATCH_ENABLED=true (cold-start safety gate).
 * Port:    HOOK_DISPATCH_PORT (default 3002).
 *
 * Contract:
 *   POST /hooks/:event  — receives serialised hook payload
 *   Returns: aggregated SDK-conformant hook response or {}
 *
 * Fan-out uses Promise.allSettled so a throwing observer cannot block others.
 * additionalContext strings from all non-empty responses are concatenated.
 */

import crypto from 'crypto';
import http from 'http';

export type ObserverCallback = (
  event: string,
  payload: unknown,
) => Promise<Record<string, unknown>>;

/**
 * Constant-time check of the `x-deus-proxy-token` header against
 * `DEUS_PROXY_TOKEN`. Defense-in-depth behind the loopback bind (an in-container
 * attacker can read the token from its own env; the real value is cross-container
 * if the bind were ever widened). When DEUS_PROXY_TOKEN is unset, the check is
 * a no-op (accept) so tests and local runs without a token still work.
 */
function isAuthorized(header: string | string[] | undefined): boolean {
  const expected = process.env.DEUS_PROXY_TOKEN;
  if (!expected) return true; // no token configured → nothing to enforce
  const provided = Array.isArray(header) ? header[0] : header;
  if (typeof provided !== 'string') return false;
  const a = Buffer.from(provided);
  const b = Buffer.from(expected);
  // timingSafeEqual throws on length mismatch — guard it, and the length check
  // is itself a (non-secret) early-out since the token length is not sensitive.
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

export class HookDispatchService {
  private readonly observers = new Map<string, ObserverCallback[]>();
  private server: http.Server | null = null;

  /**
   * Register an observer callback for a named hook event.
   * Multiple observers can be registered per event; all receive every payload.
   */
  registerObserver(event: string, cb: ObserverCallback): void {
    const list = this.observers.get(event) ?? [];
    list.push(cb);
    this.observers.set(event, list);
  }

  /**
   * Fan out a payload to all registered observers for an event.
   * Uses Promise.allSettled — throwing observers are warned and skipped.
   * Aggregation: concatenates additionalContext strings; last-writer-wins
   * for other top-level keys.
   */
  async fanOut(
    event: string,
    payload: unknown,
  ): Promise<Record<string, unknown>> {
    const callbacks = this.observers.get(event) ?? [];
    if (callbacks.length === 0) return {};

    const results = await Promise.allSettled(
      callbacks.map((cb) => cb(event, payload)),
    );

    const contexts: string[] = [];
    let merged: Record<string, unknown> = {};

    for (const result of results) {
      if (result.status === 'rejected') {
        console.warn('[HookDispatchService] observer threw:', result.reason);
        continue;
      }
      const val = result.value;
      if (!val || typeof val !== 'object') continue;

      const hookOut = (val as Record<string, unknown>).hookSpecificOutput as
        | Record<string, unknown>
        | undefined;
      if (hookOut?.additionalContext) {
        contexts.push(String(hookOut.additionalContext));
      }

      // Merge top-level fields (later observers win on scalar collisions)
      Object.assign(merged, val);
    }

    if (contexts.length > 0) {
      const existingHookOut =
        (merged.hookSpecificOutput as Record<string, unknown>) ?? {};
      merged = {
        ...merged,
        hookSpecificOutput: {
          ...existingHookOut,
          additionalContext: contexts.join('\n'),
        },
      };
    }

    return merged;
  }

  /** Start the HTTP server and begin accepting connections. */
  start(port: number): Promise<void> {
    return new Promise((resolve, reject) => {
      const server = http.createServer((req, res) => {
        void this._handleRequest(req, res);
      });
      this.server = server;
      server.on('error', reject);
      // Bind LOOPBACK only. The service is co-located in the same container as
      // the agent and must never be reachable from other containers on the
      // Docker bridge. Without the explicit host, `listen(port, cb)` binds
      // 0.0.0.0 (all interfaces) — contradicting every docstring that claims
      // loopback and exposing this no-auth decision endpoint cross-container
      // (LIA-199 threat-model). The host arg makes the bind match the design.
      server.listen(port, '127.0.0.1', () => resolve());
    });
  }

  private async _handleRequest(
    req: http.IncomingMessage,
    res: http.ServerResponse,
  ): Promise<void> {
    const match = req.method === 'POST' && req.url?.match(/^\/hooks\/(\w+)$/);
    if (!match) {
      res.writeHead(404, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: 'not found' }));
      return;
    }

    // Defense-in-depth: reject callers without the proxy token (no-op when
    // DEUS_PROXY_TOKEN is unset). Loopback bind is the primary control; this
    // backstops it (LIA-199 threat-model).
    if (!isAuthorized(req.headers['x-deus-proxy-token'])) {
      res.writeHead(401, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: 'unauthorized' }));
      return;
    }

    const event = match[1];
    const chunks: Buffer[] = [];

    await new Promise<void>((resolve) => {
      req.on('data', (chunk: Buffer) => chunks.push(chunk));
      req.on('end', resolve);
    });

    let payload: unknown;
    try {
      payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    } catch {
      res.writeHead(400, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: 'invalid json' }));
      return;
    }

    try {
      const result = await this.fanOut(event, payload);
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify(result));
    } catch (err) {
      console.warn('[HookDispatchService] fanOut error', {
        event,
        err: err instanceof Error ? err.message : String(err),
      });
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end('{}');
    }
  }

  /** Gracefully stop the server. */
  stop(): Promise<void> {
    return new Promise((resolve) => {
      if (!this.server) {
        resolve();
        return;
      }
      this.server.close(() => resolve());
      this.server = null;
    });
  }
}
