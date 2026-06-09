/**
 * Odysseus web channel — an OpenAI-compatible `/v1/chat/completions` SSE endpoint
 * that lets Odysseus's Chat mode (base_url → here) drive the existing Deus
 * container agent, so hooks / wardens / memory / persona all keep firing.
 *
 * Design (see plan path-(a)): the endpoint does NOT run the agent on the host.
 * It resolves the main/control group and enqueues a serialized GroupQueue task —
 * mirroring src/task-scheduler.ts's "single agent turn as a task" — whose
 * RuntimeEventSink writes SSE instead of channel.sendMessage. Container isolation
 * is preserved; the turn shares the main session (continuity) and serializes
 * against WhatsApp turns on the same jid.
 *
 * Security: localhost-only bind, bearer-token (constant-time) auth on every
 * route, fail-closed startup, 64 KB body cap, method gate, rate limit + SSE cap,
 * audit log with header/secret scrubbing. The bearer token is the SOLE control —
 * 127.0.0.1 only excludes remote-network attackers; any local process under the
 * same OS user can reach the port. A token-holder gets full control-group agent
 * power (the accepted cost of "share main session").
 */
import { createServer, Server, IncomingMessage, ServerResponse } from 'http';
import crypto from 'crypto';

import { RuntimeRegistry } from './agent-runtimes/registry.js';
import {
  AgentRuntimeId,
  RunContext,
  RuntimeEventSink,
  RuntimeSession,
  defaultSession,
} from './agent-runtimes/types.js';
import {
  INJECTION_SCANNER_CONFIG,
  ODYSSEUS_HTTP_ENABLED,
  ODYSSEUS_HTTP_PORT,
} from './config.js';
import { writeGroupsSnapshot, writeTasksSnapshot } from './container-runner.js';
import { getAllTasks } from './db.js';
import { readEnvFile } from './env.js';
import { GroupQueue } from './group-queue.js';
import { scanForInjection } from './guardrails/injection-scanner.js';
import { logger } from './logger.js';
import { getAvailableGroups } from './router-state.js';
import { RegisteredGroup } from './types.js';

const ODYSSEUS_BIND_HOST = '127.0.0.1'; // localhost only — never 0.0.0.0
const MIN_TOKEN_LEN = 32;
const MAX_BODY_BYTES = 64 * 1024;
const KEEPALIVE_MS = 20_000; // < Odysseus' ~300s time-to-first-token limit
const ABSOLUTE_TURN_MS = 10 * 60_000; // hard total-duration cap (DoS bound; truncates long turns)
const TASK_CLOSE_DELAY_MS = 10_000; // mirror task-scheduler: wind the container down promptly
const MAX_CONCURRENT_SSE = 5;

/* ── Rate limiter (mirrors credential-proxy.ts:69-113) ─────────────────── */
const RATE_LIMIT_MAX = 5;
const RATE_LIMIT_WINDOW_MS = 60_000;
const rateBuckets = new Map<string, number[]>();
const rateLimitCleanupInterval = setInterval(() => {
  const now = Date.now();
  for (const [key, ts] of rateBuckets) {
    const kept = ts.filter((t) => now - t < RATE_LIMIT_WINDOW_MS);
    if (kept.length === 0) rateBuckets.delete(key);
    else rateBuckets.set(key, kept);
  }
}, RATE_LIMIT_WINDOW_MS);
rateLimitCleanupInterval.unref();

let activeSse = 0;
/** main jid → true while a turn is queued/running (one in-flight per jid). */
const inFlight = new Set<string>();

function isRateLimited(key: string): boolean {
  const now = Date.now();
  const ts = (rateBuckets.get(key) ?? []).filter(
    (t) => now - t < RATE_LIMIT_WINDOW_MS,
  );
  if (ts.length >= RATE_LIMIT_MAX) {
    rateBuckets.set(key, ts);
    return true;
  }
  ts.push(now);
  rateBuckets.set(key, ts);
  return false;
}

/** @internal exposed for testing only */
export function _resetServerStateForTest(): void {
  rateBuckets.clear();
  inFlight.clear();
  activeSse = 0;
}

export interface OdysseusServerDeps {
  queue: GroupQueue;
  registry: RuntimeRegistry;
  registeredGroups: () => Record<string, RegisteredGroup>;
  getSession: (
    groupFolder: string,
    backend: AgentRuntimeId,
  ) => RuntimeSession | undefined;
  /** Persists to both router state and the DB (caller wires both). */
  setSession: (groupFolder: string, sessionRef: RuntimeSession) => void;
}

/**
 * Validate the configured token. Exported for unit tests so the fail-closed
 * predicate can be asserted without triggering process.exit.
 */
export function validateOdysseusToken(token: string | undefined): {
  ok: boolean;
  reason?: string;
} {
  if (!token) return { ok: false, reason: 'unset/empty' };
  if (token.length < MIN_TOKEN_LEN)
    return { ok: false, reason: `shorter than ${MIN_TOKEN_LEN} chars` };
  return { ok: true };
}

function timingSafeEqualStr(a: string, b: string): boolean {
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  // Length check first — timingSafeEqual throws on length mismatch. NB: this
  // leaks the token *length* via fast-path timing on length-mismatched guesses;
  // acceptable given the localhost-only bind and the MIN_TOKEN_LEN floor.
  if (ab.length !== bb.length) return false;
  return crypto.timingSafeEqual(ab, bb);
}

function extractBearer(req: IncomingMessage): string | null {
  const h = req.headers['authorization'];
  if (typeof h !== 'string') return null;
  const m = h.match(/^Bearer\s+(.+)$/i);
  return m ? m[1] : null;
}

function writeJson(res: ServerResponse, status: number, body: unknown): void {
  if (!res.writable) return;
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

function writeSse(res: ServerResponse, frame: Record<string, unknown>): void {
  if (!res.writable) return;
  res.write(`data: ${JSON.stringify(frame)}\n\n`);
}

const MODELS_RESPONSE = {
  object: 'list',
  data: [{ id: 'deus', object: 'model', created: 0, owned_by: 'deus' }],
};

function chunkFrame(
  id: string,
  delta: Record<string, unknown>,
  finish: string | null,
): Record<string, unknown> {
  return {
    id: `chatcmpl-${id}`,
    object: 'chat.completion.chunk',
    created: Math.floor(Date.now() / 1000),
    model: 'deus',
    choices: [{ index: 0, delta, finish_reason: finish }],
  };
}

function completionFrame(id: string, content: string): Record<string, unknown> {
  return {
    id: `chatcmpl-${id}`,
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model: 'deus',
    choices: [
      {
        index: 0,
        message: { role: 'assistant', content },
        finish_reason: 'stop',
      },
    ],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  };
}

/** Extract the last user message from an OpenAI chat body. */
export function extractPrompt(body: unknown): string {
  // `body` is already-parsed JSON of unknown shape (user-supplied); each access
  // is structurally cast and immediately guarded below, so no type guard.
  const messages = (body as { messages?: unknown })?.messages;
  if (!Array.isArray(messages)) return '';
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m?.role !== 'user') continue;
    if (typeof m.content === 'string') return m.content;
    if (Array.isArray(m.content)) {
      // OpenAI multi-part content: concatenate text parts.
      return m.content
        .map((p: unknown) =>
          typeof (p as { text?: unknown })?.text === 'string'
            ? (p as { text: string }).text
            : '',
        )
        .join('');
    }
    return '';
  }
  return '';
}

/**
 * Build the configured (not-yet-listening) HTTP server. Exported so tests drive
 * the handler directly on an ephemeral port without the enabled-gate.
 */
export function createOdysseusServer(
  deps: OdysseusServerDeps,
  token: string,
): Server {
  return createServer((req, res) => {
    // Sub-tick TOCTOU guard: the socket can die between a res.writable check
    // and the synchronous write; an unhandled 'error' would crash the host.
    res.on('error', (err) =>
      logger.warn({ err }, 'Odysseus SSE response error (ignored)'),
    );
    // A client reset mid-body emits 'error' on the request stream; unhandled it
    // would propagate and crash the host.
    req.on('error', (err) =>
      logger.warn({ err }, 'Odysseus request error (body read, ignored)'),
    );

    const url = (req.url || '').split('?')[0];
    const method = req.method || 'GET';
    const remoteAddr = req.socket.remoteAddress || 'unknown';

    const isChat = url === '/v1/chat/completions';
    const isModels = url === '/v1/models';

    // Method gate BEFORE auth (closes OPTIONS/HEAD presence-leak).
    if (!isChat && !isModels) {
      writeJson(res, 404, { error: 'not found' });
      return;
    }
    if (isChat && method !== 'POST') {
      writeJson(res, 405, { error: 'method not allowed' });
      return;
    }
    if (isModels && method !== 'GET') {
      writeJson(res, 405, { error: 'method not allowed' });
      return;
    }

    // Auth — all routes, constant-time.
    const bearer = extractBearer(req);
    if (!bearer || !timingSafeEqualStr(bearer, token)) {
      logger.warn({ remoteAddr, url, status: 401 }, 'Odysseus auth rejected');
      writeJson(res, 401, { error: 'unauthorized' });
      return;
    }

    if (isRateLimited(remoteAddr)) {
      writeJson(res, 429, { error: 'rate limit exceeded' });
      return;
    }

    if (isModels) {
      writeJson(res, 200, MODELS_RESPONSE);
      return;
    }

    // ── /v1/chat/completions ──
    const chunks: Buffer[] = [];
    let bodySize = 0;
    let tooLarge = false;
    req.on('data', (c: Buffer) => {
      if (tooLarge) return;
      bodySize += c.length;
      if (bodySize > MAX_BODY_BYTES) {
        tooLarge = true;
        writeJson(res, 413, { error: 'payload too large' });
        req.destroy();
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      if (tooLarge) return;
      let body: unknown;
      try {
        body = JSON.parse(Buffer.concat(chunks).toString('utf-8'));
      } catch {
        writeJson(res, 400, { error: 'invalid JSON body' });
        return;
      }
      handleChatCompletion(deps, body, res, remoteAddr);
    });
  });
}

/**
 * Start the Odysseus `/v1` server. Resolves to the Server, or `undefined` when
 * disabled. Fail-closed: calls process.exit(1) when enabled without a valid token.
 */
export function startOdysseusServer(
  deps: OdysseusServerDeps,
): Promise<Server | undefined> {
  if (!ODYSSEUS_HTTP_ENABLED) {
    logger.info('Odysseus /v1 server disabled (ODYSSEUS_HTTP_ENABLED not set)');
    return Promise.resolve(undefined);
  }

  // Token is a secret → read via readEnvFile (not config.ts), with env fallback.
  const token =
    process.env.ODYSSEUS_HTTP_TOKEN ||
    readEnvFile(['ODYSSEUS_HTTP_TOKEN']).ODYSSEUS_HTTP_TOKEN ||
    '';
  const valid = validateOdysseusToken(token);
  if (!valid.ok) {
    logger.error(
      { reason: valid.reason },
      'FATAL: ODYSSEUS_HTTP_ENABLED=1 but ODYSSEUS_HTTP_TOKEN is ' +
        `${valid.reason}. Refusing to start (never run open). ` +
        'Generate one with: openssl rand -hex 32',
    );
    process.exit(1);
  }

  return new Promise((resolve, reject) => {
    const server = createOdysseusServer(deps, token);
    server.on('close', () => clearInterval(rateLimitCleanupInterval));
    server.on('error', (err: NodeJS.ErrnoException) => reject(err));
    server.listen(ODYSSEUS_HTTP_PORT, ODYSSEUS_BIND_HOST, () => {
      logger.info(
        { port: ODYSSEUS_HTTP_PORT, host: ODYSSEUS_BIND_HOST },
        'Odysseus /v1 server started',
      );
      resolve(server);
    });
  });
}

function handleChatCompletion(
  deps: OdysseusServerDeps,
  body: unknown,
  res: ServerResponse,
  remoteAddr: string,
): void {
  const prompt = extractPrompt(body);
  if (!prompt.trim()) {
    writeJson(res, 400, { error: 'no user message in request' });
    return;
  }
  // body is validated JSON; structural cast, value checked inline. Default true.
  const stream = (body as { stream?: unknown })?.stream !== false;

  // Resolve the control group SERVER-SIDE — no request field influences this.
  const groups = deps.registeredGroups();
  const entry = Object.entries(groups).find(
    ([, g]) => g.isControlGroup === true,
  );
  if (!entry) {
    writeJson(res, 503, { error: 'no control group registered' });
    return;
  }
  const [mainJid, mainGroup] = entry;

  // If the queue is shutting down, enqueueTask silently drops the task and the
  // cleanup() in its callback never runs — refuse early so we never leak the
  // in-flight slot / activeSse counter or hang the SSE response.
  if (deps.queue.isShuttingDown()) {
    writeJson(res, 503, { error: 'server shutting down' });
    return;
  }

  // One in-flight Odysseus turn per main jid (don't starve the shared slot).
  if (inFlight.has(mainJid)) {
    writeJson(res, 429, { error: 'a turn is already in progress' });
    return;
  }
  if (stream && activeSse >= MAX_CONCURRENT_SSE) {
    writeJson(res, 503, { error: 'too many concurrent streams' });
    return;
  }

  // Injection scan (defense-in-depth; fails open by design).
  const scan = scanForInjection(prompt, INJECTION_SCANNER_CONFIG);
  if (scan.blocked) {
    logger.warn(
      { remoteAddr, score: scan.score },
      'Odysseus prompt blocked by injection scanner',
    );
    writeJson(res, 400, { error: 'request blocked' });
    return;
  }

  const turnNonce = crypto.randomBytes(8).toString('hex');
  // Audit log — no token, no Authorization header, no raw prompt content.
  logger.info(
    {
      event: 'odysseus_turn',
      remoteAddr,
      turnNonce,
      promptLen: prompt.length,
      stream,
    },
    'Odysseus turn accepted',
  );

  inFlight.add(mainJid);
  let sseCounted = false;

  // ── Lifecycle state ──
  let finalized = false;
  let firstTokenSeen = false;
  let taskActive = false; // true only while OUR task owns the active container
  const buffered: string[] = [];
  let keepalive: ReturnType<typeof setInterval> | null = null;
  let absTimer: ReturnType<typeof setTimeout> | null = null;
  let closeTimer: ReturnType<typeof setTimeout> | null = null;

  const clearTimers = () => {
    if (keepalive) {
      clearInterval(keepalive);
      keepalive = null;
    }
    if (absTimer) {
      clearTimeout(absTimer);
      absTimer = null;
    }
  };

  // Wind the container down promptly — but ONLY ever close OUR own active
  // container. Gating on taskActive at FIRE time prevents a queued/aborted
  // Odysseus turn from writing _close to WhatsApp's container on the shared jid.
  const scheduleClose = () => {
    if (closeTimer) return;
    const t = setTimeout(() => {
      closeTimer = null;
      if (taskActive) deps.queue.closeStdin(mainJid);
    }, TASK_CLOSE_DELAY_MS);
    t.unref(); // don't block a SIGTERM exit for the 10s wind-down delay
    closeTimer = t;
  };

  const cleanup = () => {
    inFlight.delete(mainJid);
    if (sseCounted) {
      activeSse = Math.max(0, activeSse - 1);
      sseCounted = false;
    }
  };

  // finalize() is SSE-only + run-once. It performs NO GroupQueue windown
  // (notifyIdle/closeStdin) — those happen solely inside the active turn's
  // eventSink, where taskActive is guaranteed true, so we never disrupt a
  // WhatsApp turn sharing this jid. Slot release still happens: every success
  // turn emits turn_complete (→ scheduleClose), error turns self-exit, and the
  // task's finally + runTurn's hard timeout bound the worst case.
  const finalize = (errMsg?: string) => {
    if (finalized) return;
    finalized = true;
    clearTimers();
    if (!res.writable) return; // server-ended OR client-aborted (destroyed)
    if (stream) {
      if (errMsg)
        writeSse(
          res,
          chunkFrame(turnNonce, { content: `\n[error] ${errMsg}` }, null),
        );
      writeSse(res, chunkFrame(turnNonce, {}, 'stop'));
      res.write('data: [DONE]\n\n');
      res.end();
    } else {
      const content = buffered.join('');
      if (!content && errMsg) writeJson(res, 502, { error: errMsg });
      else writeJson(res, 200, completionFrame(turnNonce, content));
    }
  };

  // Client abort → free SSE accounting + terminate. No queue windown here
  // (res.writable is already false); the running task closes its own container.
  res.on('close', () => finalize());

  if (stream) {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    });
    // Early role delta — satisfies the time-to-first-token window immediately.
    writeSse(res, chunkFrame(turnNonce, { role: 'assistant' }, null));
    activeSse++;
    sseCounted = true;
    keepalive = setInterval(() => {
      if (res.writable && !firstTokenSeen) res.write(': ping\n\n');
    }, KEEPALIVE_MS);
    keepalive.unref();
  }
  absTimer = setTimeout(() => {
    if (taskActive) scheduleClose();
    finalize('turn exceeded maximum duration');
  }, ABSOLUTE_TURN_MS);
  absTimer.unref();

  // Resolve backend + shared session (continuity rides the main group's session).
  const backend = deps.registry.resolve(mainGroup);
  const backendName = backend.name();
  const existing = deps.getSession(mainGroup.folder, backendName);
  const sessionRef =
    existing && existing.backend === backendName
      ? existing
      : defaultSession('', backendName);

  // Snapshots for the agent (parity with runAgent — current tasks/groups).
  // These are a sync SQLite read + two sync fs writes. They run on the request
  // path, but Odysseus turns are human-paced (one in-flight per jid), so the
  // blocking is negligible here — same work the scheduler does per task.
  try {
    writeTasksSnapshot(
      mainGroup.folder,
      true,
      getAllTasks().map((t) => ({
        id: t.id,
        groupFolder: t.group_folder,
        prompt: t.prompt,
        schedule_type: t.schedule_type,
        schedule_value: t.schedule_value,
        status: t.status,
        next_run: t.next_run,
      })),
    );
    writeGroupsSnapshot(
      mainGroup.folder,
      true,
      getAvailableGroups(groups),
      new Set(Object.keys(groups)),
    );
  } catch (err) {
    logger.warn({ err }, 'Odysseus snapshot write failed (non-fatal)');
  }

  const runContext: RunContext = {
    prompt,
    groupFolder: mainGroup.folder,
    chatJid: mainJid,
    isControlGroup: true,
  };

  const sink: RuntimeEventSink = async (event) => {
    if (event.type === 'session') {
      deps.setSession(mainGroup.folder, event.sessionRef);
    } else if (event.type === 'output_text') {
      firstTokenSeen = true;
      if (stream && res.writable)
        writeSse(res, chunkFrame(turnNonce, { content: event.text }, null));
      else if (!stream) buffered.push(event.text);
      scheduleClose();
    } else if (event.type === 'turn_complete') {
      deps.queue.notifyIdle(mainJid);
      scheduleClose();
      finalize();
    } else if (event.type === 'error') {
      finalize(event.error);
    }
  };

  // Enqueue as a serialized GroupQueue task on the main jid — runs mutually
  // exclusive with WhatsApp turns on the shared session.
  deps.queue.enqueueTask(mainJid, turnNonce, async () => {
    taskActive = true;
    try {
      const result = await backend.runTurn(runContext, sessionRef, sink);
      if (result.status === 'error') finalize(result.error || 'unknown error');
      else if (result.sessionRef)
        deps.setSession(mainGroup.folder, result.sessionRef);
      finalize();
    } catch (err) {
      finalize(err instanceof Error ? err.message : String(err));
    } finally {
      taskActive = false;
      finalize();
      cleanup();
    }
  });
}
