/**
 * Daemon-side loopback server for `deus chat` (LIA-428 / G1).
 *
 * Started by the daemon (`src/index.ts`) AFTER the runtime registry,
 * database, credential proxy, and shared group-token state are live. The
 * short-lived `deus chat` client process must never instantiate
 * `DeusNativeRuntime`, a second credential proxy, or a second group-token
 * map — group tokens are process-local and port 3001 is already owned by the
 * daemon's proxy (see docs/decisions/deus-native-cli-chat.md). This server
 * is the ONLY bridge: it listens on 127.0.0.1 with an OS-assigned ephemeral
 * port, requires a fresh high-entropy bearer token on every route
 * (constant-time compare, following src/odysseus-server.ts's pattern), and
 * advertises itself via an atomically-written, user-only (0600) discovery
 * record under CONFIG_DIR.
 *
 * Non-goals (see deus-native-chat.ts's module doc for the full list — do
 * not add these here): no G2 model-selection UX, no G3 plan-mode toggle, no
 * multi-session picker, no reuse of the Odysseus /v1/chat/completions
 * semantics (that endpoint intentionally uses fresh non-persisted sessions
 * and a control group, which conflicts with this ticket's persisted
 * synthetic CLI session).
 *
 * Internal transport detail: a turn response is newline-delimited JSON
 * (one normalized display event per line, then a `{"done":...}` record) so
 * events can be delivered incrementally when the runtime starts emitting
 * them. Protocol JSON is never written to stdout/stderr — the client
 * renders only the display events' text fields.
 */

import crypto from 'crypto';
import fs from 'fs';
import http, {
  type IncomingMessage,
  type Server,
  type ServerResponse,
} from 'http';
import path from 'path';

import type { AgentRuntime, AgentRuntimeId } from '../agent-runtimes/types.js';
import { getSession, setSession } from '../db.js';
import { extractBearer, timingSafeEqualStr } from '../odysseus-server.js';
import {
  createDeusNativeChatController,
  NativeChatBusyError,
  NATIVE_CHAT_PROTOCOL_VERSION,
  type ChatDisplayEvent,
  type NativeChatDiscoveryRecord,
  type NativeChatSessionStore,
} from './deus-native-chat.js';

const DEFAULT_HOST = '127.0.0.1';
/** Request-body cap: a chat prompt, not a file upload. */
const MAX_BODY_BYTES = 256 * 1024;

const TURN_PATH = '/v1/native-chat/turn';
const STATUS_PATH = '/v1/native-chat/status';
const CLOSE_PATH = '/v1/native-chat/close';

export interface NativeChatServerDeps {
  registry: { get(id: AgentRuntimeId): AgentRuntime | undefined };
  sessions: NativeChatSessionStore;
  /** Discovery-record location; the daemon passes nativeChatDiscoveryPath(). */
  discoveryPath: string;
  /** Loopback only. Overridable solely for tests. */
  host?: string;
  /** Optional sink for operational messages. MUST never receive the token. */
  log?: (message: string) => void;
}

export interface NativeChatServerHandle {
  host: string;
  port: number;
  /** Exposed for hermetic tests and the discovery record; never logged. */
  token: string;
  discoveryPath: string;
  server: Server;
  close(): Promise<void>;
}

/** Production adapter: the daemon's backend-scoped SQLite session rows. */
export function createDbSessionStore(): NativeChatSessionStore {
  return {
    get: (groupFolder, backend) => getSession(groupFolder, backend),
    set: (groupFolder, session) => setSession(groupFolder, session),
  };
}

function writeJson(res: ServerResponse, status: number, body: unknown): void {
  if (!res.writable) return;
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify(body));
}

/**
 * Atomic discovery write: temp file (user-only mode) + rename, created only
 * AFTER the server is listening so the record always points at a live port.
 */
function writeDiscoveryRecord(
  filePath: string,
  record: NativeChatDiscoveryRecord,
): void {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  const tmpPath = `${filePath}.${process.pid}.tmp`;
  fs.writeFileSync(tmpPath, JSON.stringify(record), { mode: 0o600 });
  fs.renameSync(tmpPath, filePath);
}

/**
 * Remove the discovery record only if this server still owns it — a
 * successor daemon's record (different token/pid) must survive a stale
 * predecessor's shutdown.
 */
function removeOwnDiscoveryRecord(
  filePath: string,
  record: NativeChatDiscoveryRecord,
): void {
  try {
    const raw = fs.readFileSync(filePath, 'utf8');
    const onDisk = JSON.parse(raw) as Partial<NativeChatDiscoveryRecord>;
    if (onDisk.token === record.token && onDisk.pid === record.pid) {
      fs.unlinkSync(filePath);
    }
  } catch {
    // Missing, unreadable, or foreign record: nothing to clean up.
  }
}

async function readBody(
  req: IncomingMessage,
): Promise<{ ok: true; body: string } | { ok: false; tooLarge: boolean }> {
  return new Promise((resolve) => {
    const chunks: Buffer[] = [];
    let size = 0;
    let done = false;
    req.on('data', (chunk: Buffer) => {
      if (done) return;
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        done = true;
        resolve({ ok: false, tooLarge: true });
        return;
      }
      chunks.push(chunk);
    });
    req.on('end', () => {
      if (done) return;
      done = true;
      resolve({ ok: true, body: Buffer.concat(chunks).toString('utf8') });
    });
    req.on('error', () => {
      if (done) return;
      done = true;
      resolve({ ok: false, tooLarge: false });
    });
  });
}

export async function startNativeChatServer(
  deps: NativeChatServerDeps,
): Promise<NativeChatServerHandle> {
  const host = deps.host ?? DEFAULT_HOST;
  const log = deps.log ?? (() => {});

  // Resolve the runtime EXPLICITLY: `deus chat` is this ticket's native
  // surface and must not follow the channel/global default backend.
  const runtime = deps.registry.get('deus-native');
  if (!runtime) {
    throw new Error(
      'native chat server: the deus-native runtime is not registered',
    );
  }
  const controller = createDeusNativeChatController({
    runtime,
    sessions: deps.sessions,
  });
  await controller.start();

  const token = crypto.randomBytes(32).toString('hex');
  // Serialize turns for the ONE fixed CLI session: a competing prompt is
  // rejected rather than interleaved into the same LangGraph thread.
  let turnInFlight = false;

  async function handleTurn(
    req: IncomingMessage,
    res: ServerResponse,
  ): Promise<void> {
    const read = await readBody(req);
    if (!read.ok) {
      writeJson(
        res,
        read.tooLarge ? 413 : 400,
        read.tooLarge
          ? { error: 'request body too large' }
          : { error: 'request aborted' },
      );
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(read.body);
    } catch {
      writeJson(res, 400, { error: 'malformed JSON body' });
      return;
    }
    if (typeof parsed !== 'object' || parsed === null) {
      writeJson(res, 400, { error: 'malformed request' });
      return;
    }
    const body = parsed as Record<string, unknown>;
    if (body.version !== NATIVE_CHAT_PROTOCOL_VERSION) {
      writeJson(res, 400, { error: 'protocol version mismatch' });
      return;
    }
    if (typeof body.prompt !== 'string' || body.prompt.trim() === '') {
      writeJson(res, 400, { error: 'prompt must be a non-empty string' });
      return;
    }
    if (typeof body.cwd !== 'string' || body.cwd === '') {
      writeJson(res, 400, { error: 'cwd must be a non-empty string' });
      return;
    }

    if (turnInFlight) {
      writeJson(res, 409, { error: 'a chat turn is already in progress' });
      return;
    }
    turnInFlight = true;
    try {
      res.writeHead(200, { 'Content-Type': 'application/x-ndjson' });
      const writeEvent = (event: ChatDisplayEvent) => {
        if (res.writable) res.write(`${JSON.stringify(event)}\n`);
      };
      const outcome = await controller.runTurn(
        body.prompt,
        { cwd: body.cwd, resume: true },
        writeEvent,
      );
      if (res.writable)
        res.end(`${JSON.stringify({ done: true, ok: outcome.ok })}\n`);
    } catch (err) {
      // NativeChatBusyError can only race here if a second request slipped
      // past the flag (controller is the deeper guard); everything else is
      // an unexpected failure surfaced as a sanitized display event.
      const message =
        err instanceof NativeChatBusyError
          ? 'a chat turn is already in progress'
          : 'the chat turn failed unexpectedly';
      if (res.writable) {
        res.write(
          `${JSON.stringify({ kind: 'chat_error', message } satisfies ChatDisplayEvent)}\n`,
        );
        res.end(`${JSON.stringify({ done: true, ok: false })}\n`);
      }
      log(`native chat turn failed: ${String(err)}`);
    } finally {
      turnInFlight = false;
    }
  }

  const server = http.createServer((req, res) => {
    // Socket-death guards (odysseus-server.ts precedent): unhandled 'error'
    // events on either stream would crash the daemon.
    res.on('error', () => {});
    req.on('error', () => {});

    const url = (req.url || '').split('?')[0];
    const method = req.method || 'GET';

    const isTurn = url === TURN_PATH;
    const isStatus = url === STATUS_PATH;
    const isClose = url === CLOSE_PATH;

    // Exact routes only; method gate BEFORE auth (presence-leak closure,
    // same ordering odysseus-server.ts uses).
    if (!isTurn && !isStatus && !isClose) {
      writeJson(res, 404, { error: 'not found' });
      return;
    }
    if ((isTurn || isClose) && method !== 'POST') {
      writeJson(res, 405, { error: 'method not allowed' });
      return;
    }
    if (isStatus && method !== 'GET') {
      writeJson(res, 405, { error: 'method not allowed' });
      return;
    }

    // Auth — all routes, constant-time. The token itself is never logged.
    const bearer = extractBearer(req);
    if (!bearer || !timingSafeEqualStr(bearer, token)) {
      writeJson(res, 401, { error: 'unauthorized' });
      return;
    }

    if (isStatus) {
      writeJson(res, 200, controller.status());
      return;
    }
    if (isClose) {
      // No turnInFlight guard here (unlike handleTurn): a client-side SIGINT
      // can fire this concurrently with an in-progress turn. Safe today only
      // because DeusNativeRuntime.close() is a no-op stub — if it ever
      // becomes non-trivial, this route needs the same in-flight guard.
      controller
        .close()
        .then(() => writeJson(res, 200, { closed: true }))
        .catch((err) => {
          log(`native chat close failed: ${String(err)}`);
          writeJson(res, 500, { error: 'close failed' });
        });
      return;
    }
    void handleTurn(req, res);
  });

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, host, () => {
      server.removeListener('error', reject);
      resolve();
    });
  });

  const address = server.address();
  if (address === null || typeof address === 'string') {
    server.close();
    throw new Error('native chat server: could not determine listen port');
  }
  const record: NativeChatDiscoveryRecord = {
    version: NATIVE_CHAT_PROTOCOL_VERSION,
    pid: process.pid,
    host,
    port: address.port,
    token,
  };
  writeDiscoveryRecord(deps.discoveryPath, record);
  log(`native chat server listening on ${host}:${address.port}`);

  return {
    host,
    port: address.port,
    token,
    discoveryPath: deps.discoveryPath,
    server,
    close: async () => {
      removeOwnDiscoveryRecord(deps.discoveryPath, record);
      await new Promise<void>((resolve) => {
        server.close(() => resolve());
        // Keep-alive client sockets (undici pools them) would otherwise
        // stall close() until their idle timeout.
        server.closeAllConnections();
      });
    },
  };
}
