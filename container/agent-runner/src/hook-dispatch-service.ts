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

import http from 'http';

export type ObserverCallback = (
  event: string,
  payload: unknown,
) => Promise<Record<string, unknown>>;

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
  async fanOut(event: string, payload: unknown): Promise<Record<string, unknown>> {
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
      server.listen(port, () => resolve());
    });
  }

  private async _handleRequest(
    req: http.IncomingMessage,
    res: http.ServerResponse,
  ): Promise<void> {
    const match =
      req.method === 'POST' && req.url?.match(/^\/hooks\/(\w+)$/);
    if (!match) {
      res.writeHead(404, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ error: 'not found' }));
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
      console.warn(
        '[HookDispatchService] fanOut error',
        { event, err: err instanceof Error ? err.message : String(err) },
      );
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
