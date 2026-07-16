import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import http from 'http';
import type { AddressInfo } from 'net';
import type { Server } from 'http';

// ── Mock heavy / side-effecting modules (same approach as odysseus-server.test.ts) ──
vi.mock('./config.js', () => ({
  ODYSSEUS_HTTP_ENABLED: true,
  ODYSSEUS_HTTP_PORT: 0,
  INJECTION_SCANNER_CONFIG: { enabled: false, threshold: 0.7, logOnly: true },
  PROJECT_ROOT: '/tmp/deus-test',
}));
vi.mock('./container-runner.js', () => ({
  writeTasksSnapshot: vi.fn(),
  writeGroupsSnapshot: vi.fn(),
}));
vi.mock('./db.js', () => ({ getAllTasks: vi.fn(() => []) }));
vi.mock('./router-state.js', () => ({ getAvailableGroups: vi.fn(() => []) }));
vi.mock('./env.js', () => ({ readEnvFile: vi.fn(() => ({})) }));
vi.mock('./guardrails/injection-scanner.js', () => ({
  scanForInjection: () => ({
    blocked: false,
    triggered: false,
    score: 0,
    matches: [] as string[],
  }),
}));
vi.mock('./logger.js', () => ({
  logger: { info: vi.fn(), error: vi.fn(), debug: vi.fn(), warn: vi.fn() },
}));

import {
  createOdysseusServer,
  stopOdysseusServer,
  _resetServerStateForTest,
  type OdysseusServerDeps,
} from './odysseus-server.js';
import {
  RuntimeActivityBroadcaster,
  withRuntimeActivityBroadcast,
  type RuntimeActivityEnvelope,
  type RuntimeActivitySource,
} from './agent-runtimes/activity-broadcaster.js';
import type {
  AgentRuntime,
  RuntimeCapabilities,
  RuntimeEvent,
} from './agent-runtimes/types.js';

const TOKEN = 'a'.repeat(48); // valid (>= MIN_TOKEN_LEN=32)
const authHeaders = { Authorization: `Bearer ${TOKEN}` };
const SOURCE: RuntimeActivitySource = {
  backend: 'deus-native',
  groupFolder: 'group-x',
  chatJid: 'x@deus.local',
};

function makeDeps(
  broadcaster?: RuntimeActivityBroadcaster,
): OdysseusServerDeps {
  return {
    // /v1/activity never touches queue/registry/registeredGroups — minimal
    // stubs that would throw loudly if the route regressed into using them.
    queue: {
      enqueueTask: () => {
        throw new Error('activity route must never enqueue a GroupQueue task');
      },
      closeStdin: () => {},
      notifyIdle: () => {},
      isShuttingDown: () => false,
    } as unknown as OdysseusServerDeps['queue'],
    registry: {
      resolve: () => {
        throw new Error('activity route must never resolve a backend');
      },
    } as unknown as OdysseusServerDeps['registry'],
    registeredGroups: () => ({}),
    activityBroadcaster: broadcaster ?? new RuntimeActivityBroadcaster(),
  };
}

// ── SSE frame parsing ────────────────────────────────────────────────────────
interface ParsedFrame {
  id?: string;
  event?: string;
  data?: string;
  retry?: string;
  comment?: string;
  raw: string;
}

function parseFrame(raw: string): ParsedFrame {
  const frame: ParsedFrame = { raw };
  for (const line of raw.split('\n')) {
    if (line === '') continue;
    if (line.startsWith(': ')) {
      frame.comment = line.slice(2);
      continue;
    }
    const sep = line.indexOf(': ');
    if (sep === -1) continue;
    const key = line.slice(0, sep);
    const value = line.slice(sep + 2);
    if (key === 'id') frame.id = value;
    else if (key === 'event') frame.event = value;
    else if (key === 'data') frame.data = value;
    else if (key === 'retry') frame.retry = value;
  }
  return frame;
}

/**
 * A real `http.request()`-backed `/v1/activity` client that parses incoming
 * SSE frames incrementally (buffered by the `\n\n` delimiter) and exposes
 * them one at a time via `next()` — so a test can assert on frames as they
 * arrive without ever waiting for the (permanent) connection to end.
 */
class ActivityClient {
  req: http.ClientRequest;
  statusCode?: number;
  headers?: http.IncomingHttpHeaders;
  closed = false;
  opened: Promise<void>;
  closedPromise: Promise<void>;

  private buffer = '';
  private frameQueue: ParsedFrame[] = [];
  private waiters: Array<(f: ParsedFrame) => void> = [];
  private openResolve!: () => void;
  private closeResolve!: () => void;

  constructor(opts: {
    port: number;
    path?: string;
    headers?: Record<string, string>;
  }) {
    this.opened = new Promise((resolve) => {
      this.openResolve = resolve;
    });
    this.closedPromise = new Promise((resolve) => {
      this.closeResolve = resolve;
    });
    this.req = http.request(
      {
        method: 'GET',
        path: opts.path ?? '/v1/activity',
        hostname: '127.0.0.1',
        port: opts.port,
        headers: { ...authHeaders, ...opts.headers },
      },
      (res) => {
        this.statusCode = res.statusCode;
        this.headers = res.headers;
        this.openResolve();
        res.on('data', (chunk: Buffer) => this.onData(chunk.toString('utf-8')));
        res.on('close', () => {
          this.closed = true;
          this.closeResolve();
        });
        res.on('error', () => {});
      },
    );
    this.req.on('error', () => {}); // swallow ECONNRESET from destroy()
    this.req.end();
  }

  private onData(chunk: string): void {
    this.buffer += chunk;
    let idx: number;
    while ((idx = this.buffer.indexOf('\n\n')) !== -1) {
      const rawFrame = this.buffer.slice(0, idx);
      this.buffer = this.buffer.slice(idx + 2);
      const parsed = parseFrame(rawFrame);
      const waiter = this.waiters.shift();
      if (waiter) waiter(parsed);
      else this.frameQueue.push(parsed);
    }
  }

  /** Resolves with the next complete SSE frame (data, comment/ping, or the
   * connection-open `retry:` directive — always the first frame on a 200
   * connection), in arrival order. Callers that don't care about the retry
   * directive should discard it with one `next()` call right after `opened`. */
  next(): Promise<ParsedFrame> {
    const queued = this.frameQueue.shift();
    if (queued) return Promise.resolve(queued);
    return new Promise((resolve) => this.waiters.push(resolve));
  }

  destroy(): void {
    this.req.destroy();
  }
}

/** Discards the connection-open `retry: 15000` directive — always the first
 * frame on a 200 connection — so a test can go straight to real data/ping
 * frames via `next()` afterward. */
async function discardRetry(c: ActivityClient): Promise<void> {
  const frame = await c.next();
  if (frame.retry === undefined) {
    throw new Error(
      'discardRetry(): expected the connection-open retry: directive as the first frame',
    );
  }
}

async function waitForSubscriberCount(
  broadcaster: RuntimeActivityBroadcaster,
  expected: number,
): Promise<void> {
  for (let i = 0; i < 100; i++) {
    if (broadcaster.subscriberCount() === expected) return;
    await new Promise((r) => setTimeout(r, 10));
  }
  throw new Error(
    `broadcaster.subscriberCount() never reached ${expected} ` +
      `(still ${broadcaster.subscriberCount()})`,
  );
}

function statusRequest(options: {
  method?: string;
  path?: string;
  headers?: Record<string, string>;
  port: number;
}): Promise<{
  statusCode: number;
  body: string;
  headers: http.IncomingHttpHeaders;
}> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        method: options.method ?? 'GET',
        path: options.path ?? '/v1/activity',
        hostname: '127.0.0.1',
        port: options.port,
        headers: options.headers,
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () =>
          resolve({
            statusCode: res.statusCode!,
            body: Buffer.concat(chunks).toString(),
            headers: res.headers,
          }),
        );
      },
    );
    req.on('error', reject);
    req.end();
  });
}

let server: Server;
let port: number;
let currentBroadcaster: RuntimeActivityBroadcaster | undefined;

function listen(deps: OdysseusServerDeps): Promise<void> {
  currentBroadcaster = deps.activityBroadcaster;
  server = createOdysseusServer(deps, TOKEN);
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      port = (server.address() as AddressInfo).port;
      resolve();
    });
  });
}

function client(opts?: { path?: string; headers?: Record<string, string> }) {
  return new ActivityClient({ port, path: opts?.path, headers: opts?.headers });
}

beforeEach(() => {
  _resetServerStateForTest();
});

afterEach(async () => {
  // Stop the server / route subscriptions FIRST, then close the process-wide
  // broadcaster last — matching production ownership order (PLAN.md §5).
  if (server?.listening) {
    await stopOdysseusServer(server);
  }
  currentBroadcaster?.close();
  currentBroadcaster = undefined;
});

// ── Case 1: endpoint / auth / method contract ───────────────────────────────
describe('GET /v1/activity — endpoint, auth, and method contract', () => {
  it('rejects a missing bearer with 401', async () => {
    await listen(makeDeps());
    const r = await statusRequest({ port });
    expect(r.statusCode).toBe(401);
  });

  it('rejects an invalid bearer with 401', async () => {
    await listen(makeDeps());
    const r = await statusRequest({
      port,
      headers: { Authorization: 'Bearer ' + 'z'.repeat(48) },
    });
    expect(r.statusCode).toBe(401);
  });

  it('rejects an authenticated non-GET with 405', async () => {
    await listen(makeDeps());
    const r = await statusRequest({
      port,
      method: 'POST',
      headers: authHeaders,
    });
    expect(r.statusCode).toBe(405);
  });

  it('never accepts a query-string token as an auth bypass', async () => {
    await listen(makeDeps());
    const r = await statusRequest({
      port,
      path: `/v1/activity?token=${TOKEN}`,
    });
    expect(r.statusCode).toBe(401);
  });

  it('an authenticated GET is admitted with SSE headers and the 15s retry directive, using the same bearer path as every other route', async () => {
    await listen(makeDeps());
    const c = client();
    await c.opened;
    expect(c.statusCode).toBe(200);
    expect(c.headers?.['content-type']).toContain('text/event-stream');
    expect(c.headers?.['cache-control']).toContain('no-cache');
    expect(c.headers?.['connection']).toContain('keep-alive');

    const first = await c.next();
    expect(first.retry).toBe('15000');

    c.destroy();
  });
});

// ── Case 3: multiple events on one connection ───────────────────────────────
describe('multiple runtime events on one connection', () => {
  it('delivers events in publication order, resolving as each arrives (not waiting for connection end)', async () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster));
    const c = client();
    await c.opened;
    await waitForSubscriberCount(broadcaster, 1);
    await discardRetry(c);

    broadcaster.publish('run-1', SOURCE, { type: 'activity', text: 'a' });
    broadcaster.publish('run-1', SOURCE, {
      type: 'tool_call',
      name: 'web_search',
      arguments: {},
    });
    broadcaster.publish('run-1', SOURCE, { type: 'turn_complete' });

    const frames = [await c.next(), await c.next(), await c.next()];
    expect(frames.map((f) => f.event)).toEqual([
      'activity',
      'tool_call',
      'turn_complete',
    ]);
    // The test resolved via 3 delivered frames — the connection is still open.
    expect(c.closed).toBe(false);

    c.destroy();
  });
});

// ── Case 4: multi-client fan-out ─────────────────────────────────────────────
describe('multi-client fan-out', () => {
  it('delivers byte-equivalent envelopes, in order, to every connected client; an aborted client stops receiving and is unsubscribed', async () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster));
    const a = client();
    const b = client();
    await Promise.all([a.opened, b.opened]);
    await waitForSubscriberCount(broadcaster, 2);
    await Promise.all([discardRetry(a), discardRetry(b)]);

    broadcaster.publish('run-1', SOURCE, { type: 'activity', text: 'first' });
    broadcaster.publish('run-1', SOURCE, { type: 'activity', text: 'second' });

    const [a1, b1] = await Promise.all([a.next(), b.next()]);
    const [a2, b2] = await Promise.all([a.next(), b.next()]);
    expect(a1.data).toBe(b1.data);
    expect(a2.data).toBe(b2.data);
    expect(JSON.parse(a1.data!).payload.text).toBe('first');
    expect(JSON.parse(a2.data!).payload.text).toBe('second');

    a.destroy();
    await waitForSubscriberCount(broadcaster, 1);

    broadcaster.publish('run-1', SOURCE, { type: 'activity', text: 'third' });
    const b3 = await b.next();
    expect(JSON.parse(b3.data!).payload.text).toBe('third');

    b.destroy();
  });
});

// ── Case 6: client abort cleanup ─────────────────────────────────────────────
describe('client abort cleanup', () => {
  it('destroying the connection unsubscribes, clears the keepalive interval, and a later publish causes no write/error', async () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster));
    const clearIntervalSpy = vi.spyOn(global, 'clearInterval');

    const c = client();
    await c.opened;
    await waitForSubscriberCount(broadcaster, 1);

    const callsBefore = clearIntervalSpy.mock.calls.length;
    c.destroy();
    await waitForSubscriberCount(broadcaster, 0);
    expect(clearIntervalSpy.mock.calls.length).toBeGreaterThan(callsBefore);

    expect(() =>
      broadcaster.publish('run-1', SOURCE, { type: 'turn_complete' }),
    ).not.toThrow();

    clearIntervalSpy.mockRestore();
  });

  it('reconnecting and disconnecting six times sequentially never returns 503 — the separate admission counter is released each time (distinct from the 429 rate limiter)', async () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster));

    for (let i = 0; i < 6; i++) {
      const c = client();
      await c.opened;
      // A leaked activeActivitySse slot would eventually manifest as 503;
      // the 6th same-address attempt may legitimately hit the SEPARATE 429
      // rate limiter instead (RATE_LIMIT_MAX=5/60s) — that is not the bug
      // under test here.
      expect(c.statusCode).not.toBe(503);
      if (c.statusCode === 200) {
        c.destroy();
        await waitForSubscriberCount(broadcaster, 0);
      } else {
        c.destroy();
      }
    }
  });
});

// ── Case 7: server shutdown cleanup ──────────────────────────────────────────
describe('server shutdown cleanup (stopOdysseusServer)', () => {
  it('draining two live connections: both observe close, broadcaster has zero route subscribers, keepalives cleared, close callback completes — the broadcaster itself stays open', async () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster));
    const clearIntervalSpy = vi.spyOn(global, 'clearInterval');

    const a = client();
    const b = client();
    await Promise.all([a.opened, b.opened]);
    await waitForSubscriberCount(broadcaster, 2);

    const callsBefore = clearIntervalSpy.mock.calls.length;
    await stopOdysseusServer(server);

    await Promise.all([a.closedPromise, b.closedPromise]);
    expect(broadcaster.subscriberCount()).toBe(0);
    expect(clearIntervalSpy.mock.calls.length).toBeGreaterThan(callsBefore);
    expect(server.listening).toBe(false);

    // The broadcaster itself was never closed by stopOdysseusServer() — still
    // open, still delivers to a fresh subscriber.
    const recorded: RuntimeActivityEnvelope[] = [];
    broadcaster.subscribe((e) => recorded.push(e));
    broadcaster.publish('run-1', SOURCE, { type: 'turn_complete' });
    expect(recorded).toHaveLength(1);

    clearIntervalSpy.mockRestore();
  });

  it('is idempotent — a second call resolves without re-invoking server.close() or throwing', async () => {
    await listen(makeDeps());
    await stopOdysseusServer(server);
    await expect(stopOdysseusServer(server)).resolves.toBeUndefined();
  });
});

// ── Case 8 (route part): shared-broadcaster shutdown ordering ───────────────
describe('cross-cutting lifecycle: the shared broadcaster survives stopOdysseusServer()', () => {
  const CAPS: RuntimeCapabilities = {
    shell: false,
    filesystem: false,
    web: true,
    multimodal: false,
    handoffs: false,
    persistent_sessions: true,
    tool_streaming: false,
  };

  it('after stopOdysseusServer() resolves but before the process-wide close(), a decorated non-SSE (channel/task/Linear-shaped) turn still publishes; after close() it becomes a documented safe no-op without changing the downstream sink/RunResult', async () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster));

    // Stands in for whatever else the process-wide composition root
    // subscribes to the SAME broadcaster (not an SSE client).
    const recorded: RuntimeActivityEnvelope[] = [];
    broadcaster.subscribe((e) => recorded.push(e));

    const sseClient = client();
    await sseClient.opened;
    await waitForSubscriberCount(broadcaster, 2); // recorder + SSE route

    await stopOdysseusServer(server);
    await waitForSubscriberCount(broadcaster, 1); // only the recorder remains

    const fakeRuntime: AgentRuntime = {
      name: () => 'deus-native',
      capabilities: () => CAPS,
      startOrResume: async () => ({ backend: 'deus-native', session_id: 's' }),
      close: async () => {},
      runTurn: async (_ctx, _s, sink) => {
        await sink({ type: 'turn_complete' });
        return { status: 'success', result: 'ok' };
      },
    };
    const decorated = withRuntimeActivityBroadcast(fakeRuntime, broadcaster);

    // A channel/task/Linear-shaped RunContext — NOT an SSE client.
    const downstream: RuntimeEvent[] = [];
    const result = await decorated.runTurn(
      {
        prompt: 'scheduled task prompt',
        groupFolder: 'group-x',
        chatJid: 'task@deus.local',
        isControlGroup: false,
        isScheduledTask: true,
      },
      { backend: 'deus-native', session_id: '' },
      (e) => {
        downstream.push(e);
      },
    );

    expect(result).toEqual({ status: 'success', result: 'ok' });
    expect(downstream).toEqual([{ type: 'turn_complete' }]);
    expect(recorded.map((e) => e.type)).toEqual(['turn_complete']);

    // NOW simulate the process-wide tail-of-shutdown close().
    broadcaster.close();
    const downstream2: RuntimeEvent[] = [];
    const result2 = await decorated.runTurn(
      {
        prompt: 'late task',
        groupFolder: 'group-x',
        chatJid: 'task@deus.local',
        isControlGroup: false,
      },
      { backend: 'deus-native', session_id: '' },
      (e) => {
        downstream2.push(e);
      },
    );
    expect(result2).toEqual({ status: 'success', result: 'ok' });
    expect(downstream2).toEqual([{ type: 'turn_complete' }]); // downstream sink unaffected
    expect(recorded).toHaveLength(1); // no new envelope reached the broadcaster

    sseClient.destroy();
  });
});

// ── Case 9: fire-and-forward reconnection ───────────────────────────────────
describe('reconnection — fire-and-forward only, no replay', () => {
  it('a reconnect does not receive events published while disconnected; Last-Event-ID is accepted but ignored', async () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster));

    const a = client();
    await a.opened;
    await waitForSubscriberCount(broadcaster, 1);
    await discardRetry(a);
    broadcaster.publish('run-1', SOURCE, { type: 'activity', text: 'N' });
    const frameN = await a.next();
    const envelopeN = JSON.parse(frameN.data!) as RuntimeActivityEnvelope;
    expect(envelopeN.sequence).toBe(1);

    a.destroy();
    await waitForSubscriberCount(broadcaster, 0);

    // Published while NO client is connected — must never be replayed.
    broadcaster.publish('run-1', SOURCE, {
      type: 'activity',
      text: 'missed N+1',
    });

    const b = client({ headers: { 'Last-Event-ID': envelopeN.id } });
    await b.opened;
    expect(b.statusCode).toBe(200); // header accepted, not rejected
    await waitForSubscriberCount(broadcaster, 1);
    await discardRetry(b);

    broadcaster.publish('run-1', SOURCE, { type: 'activity', text: 'N+2' });
    const frameFirst = await b.next();
    const envelopeFirst = JSON.parse(
      frameFirst.data!,
    ) as RuntimeActivityEnvelope;

    // B's FIRST received event is N+2 (sequence 3) — the missed N+1
    // (sequence 2) was never replayed. Same streamId (same process epoch),
    // so the client can detect the gap via the sequence jump (1 → 3).
    expect(envelopeFirst.sequence).toBe(3);
    expect(envelopeFirst.streamId).toBe(envelopeN.streamId);

    b.destroy();
  });

  it('a fresh broadcaster (simulating a host restart) has a different streamId and its own sequence restarting at 1', async () => {
    const broadcaster1 = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster1));
    const c1 = client();
    await c1.opened;
    await waitForSubscriberCount(broadcaster1, 1);
    await discardRetry(c1);
    broadcaster1.publish('run-1', SOURCE, { type: 'turn_complete' });
    const envelope1 = JSON.parse(
      (await c1.next()).data!,
    ) as RuntimeActivityEnvelope;
    expect(envelope1.sequence).toBe(1);
    c1.destroy();
    await stopOdysseusServer(server);

    const broadcaster2 = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster2));
    const c2 = client();
    await c2.opened;
    await waitForSubscriberCount(broadcaster2, 1);
    await discardRetry(c2);
    broadcaster2.publish('run-1', SOURCE, { type: 'turn_complete' });
    const envelope2 = JSON.parse(
      (await c2.next()).data!,
    ) as RuntimeActivityEnvelope;

    expect(envelope2.sequence).toBe(1); // sequence restarts on the new instance
    expect(envelope2.streamId).not.toBe(envelope1.streamId); // new ordering epoch

    c2.destroy();
  });
});

// ── Case 10: keepalive behavior ──────────────────────────────────────────────
describe('keepalive', () => {
  function collectIntervalHandlers(): {
    restore: () => void;
    calls: Array<{ delay: number; fire: () => void }>;
  } {
    const calls: Array<{ delay: number; fire: () => void }> = [];
    const spy = vi
      .spyOn(global, 'setInterval')
      .mockImplementation(
        (
          handler: (...handlerArgs: unknown[]) => void,
          timeout?: number,
          ...args: unknown[]
        ) => {
          calls.push({ delay: timeout ?? 0, fire: () => handler(...args) });
          // Inert but shape-compatible handle: real code calls .unref() on it.
          return {
            unref: () => {},
            ref: () => {},
          } as unknown as ReturnType<typeof setInterval>;
        },
      );
    return { restore: () => spy.mockRestore(), calls };
  }

  it('writes ": ping" both before and after a data event (unlike chat\'s firstTokenSeen-gated keepalive), and stops firing after disconnect', async () => {
    const { restore, calls } = collectIntervalHandlers();
    const broadcaster = new RuntimeActivityBroadcaster();
    try {
      await listen(makeDeps(broadcaster));
      const c = client();
      await c.opened;
      await waitForSubscriberCount(broadcaster, 1);
      await discardRetry(c);

      const activityInterval = calls.find((call) => call.delay === 20_000);
      expect(activityInterval).toBeDefined();

      activityInterval!.fire(); // simulate a tick BEFORE any data event
      const ping1 = await c.next();
      expect(ping1.comment).toBe('ping');

      broadcaster.publish('run-1', SOURCE, { type: 'turn_complete' });
      const dataFrame = await c.next();
      expect(dataFrame.event).toBe('turn_complete');

      activityInterval!.fire(); // simulate a tick AFTER a data event — still pings
      const ping2 = await c.next();
      expect(ping2.comment).toBe('ping');

      c.destroy();
      await waitForSubscriberCount(broadcaster, 0);

      // The interval is cleared on disconnect — firing the captured callback
      // again must not throw (defensive) and must not reach a dead socket.
      expect(() => activityInterval!.fire()).not.toThrow();
    } finally {
      restore();
    }
  });
});

// ── Case 11: admission / rate behavior ───────────────────────────────────────
describe('admission and rate-limit interaction', () => {
  it('reconnects consume the existing rate-limit bucket (RATE_LIMIT_MAX=5/60s)', async () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster));

    for (let i = 0; i < 5; i++) {
      const c = client();
      await c.opened;
      expect(c.statusCode).toBe(200);
      c.destroy();
      await waitForSubscriberCount(broadcaster, 0);
    }
    const sixth = client();
    await sixth.opened;
    expect(sixth.statusCode).toBe(429);
    sixth.destroy();
  });

  it('delivered events and keepalive pings do not consume additional rate-limit entries', async () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    await listen(makeDeps(broadcaster));

    const c = client();
    await c.opened; // 1st rate-bucket entry
    await discardRetry(c);
    for (let i = 0; i < 10; i++) {
      broadcaster.publish('run-1', SOURCE, { type: 'activity', text: `e${i}` });
      await c.next();
    }
    c.destroy();
    await waitForSubscriberCount(broadcaster, 0);

    // 4 more fresh connections (2nd..5th rate-bucket entries) — all still
    // admitted, proving the 10 delivered events above consumed none of the
    // rate-limit budget.
    for (let i = 0; i < 4; i++) {
      const c2 = client();
      await c2.opened;
      expect(c2.statusCode).toBe(200);
      c2.destroy();
      await waitForSubscriberCount(broadcaster, 0);
    }
  });

  it('the activity admission cap uses its own counter, separate from the rate limiter (503, not 429, once MAX_CONCURRENT_SSE is reached)', async () => {
    // isolates the 503 cap from the 429 rate limiter (both thresholds are 5)
    // by advancing the faked Date between admissions so each connection's
    // rate-bucket entry ages out of the 60s window before the next one — real
    // timers/sockets are untouched, per PLAN.md §7 case 11's explicit
    // instruction not to weaken or reorder auth/rate limiting to test this.
    vi.useFakeTimers({ toFake: ['Date'] });
    const broadcaster = new RuntimeActivityBroadcaster();
    try {
      await listen(makeDeps(broadcaster));
      const clients: ActivityClient[] = [];
      for (let i = 0; i < 5; i++) {
        const c = client();
        await c.opened;
        expect(c.statusCode).toBe(200);
        clients.push(c);
        vi.setSystemTime(Date.now() + 61_000);
      }

      const sixth = client();
      await sixth.opened;
      expect(sixth.statusCode).toBe(503);

      for (const c of clients) c.destroy();
      sixth.destroy();
    } finally {
      vi.useRealTimers();
    }
  });
});
