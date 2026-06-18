// The single public-facing HTTP server. Every inbound public request passes
// through here and ONLY here — this is the centralization chokepoint. Handlers
// (webhook channel, migrated Linear, etc.) register a path prefix + their own
// verify(); the gateway owns the cross-cutting controls (body cap, rate limit,
// IP allowlist, scrubbed audit) applied BEFORE any handler runs.
//
// Built on Node's built-in `http` to match every other server in this repo
// (credential-proxy, tool-proxy, odysseus) — no framework dependency added.

import {
  createServer,
  type Server,
  type IncomingMessage,
  type ServerResponse,
} from 'http';
import { logger } from '../logger.js';
import type { VerifyResult } from './hmac.js';

/**
 * A registered ingress route. The gateway dispatches to the first handler whose
 * `pathPrefix` matches (Strategy registry; O(routes), routes < 10 so a linear
 * `Array.find` is correct and a prefix tree would be premature).
 */
export interface IngressHandler {
  pathPrefix: string;
  /** Reject (403) before `handle` runs — signature/replay/etc. Sync or async. */
  verify(
    req: IncomingMessage,
    bodyRaw: Buffer,
  ): VerifyResult | Promise<VerifyResult>;
  /** Owns the response. Only called when `verify` returned ok. */
  handle(
    req: IncomingMessage,
    res: ServerResponse,
    bodyRaw: Buffer,
  ): Promise<void>;
}

export interface GatewayConfig {
  host: string;
  port: number;
  maxBodyBytes: number;
  rateLimitMax: number;
  rateLimitWindowMs: number;
  /** Exact peer IPs allowed. Empty = allow all. Coarse best-effort filter on the
   *  unspoofable socket peer (HMAC is the real auth). Behind the tunnel the peer
   *  is loopback — use ngrok edge IP policy for real-client filtering. */
  ipAllowlist: string[];
}

export interface GatewayDeps {
  /** Mutable, shared. Channels push their routes AFTER the server is listening. */
  handlers: IngressHandler[];
  config: GatewayConfig;
}

function writeStatus(res: ServerResponse, status: number, msg: string): void {
  if (!res.writable) return;
  res.writeHead(status, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ ok: status < 400, message: msg }));
}

/**
 * The unspoofable network peer address — used for ALL security decisions
 * (allowlist, rate limit). X-Forwarded-For is client-controllable and must
 * never gate access; behind the tunnel the peer is the tunnel's loopback, so
 * real-client IP filtering belongs at the ngrok edge, not here.
 */
function peerIp(req: IncomingMessage): string {
  return req.socket.remoteAddress ?? '';
}

/** Client-claimed forwarded IP — informational only (audit), never trusted. */
function forwardedFor(req: IncomingMessage): string | undefined {
  const xff = req.headers['x-forwarded-for'];
  if (typeof xff === 'string' && xff.length > 0)
    return xff.split(',')[0]!.trim();
  return undefined;
}

/** Sliding-window per-key rate limiter (same shape as odysseus-server.ts:83-95). */
function makeRateLimiter(max: number, windowMs: number) {
  const buckets = new Map<string, number[]>();
  return function isRateLimited(key: string, now: number): boolean {
    const ts = (buckets.get(key) ?? []).filter((t) => now - t < windowMs);
    if (ts.length >= max) {
      buckets.set(key, ts);
      return true;
    }
    ts.push(now);
    buckets.set(key, ts);
    return false;
  };
}

/** Build (but do not start) the gateway server. Exposed for tests. */
export function createIngressGateway(deps: GatewayDeps): Server {
  const { handlers, config } = deps;
  const allow = new Set(config.ipAllowlist);
  const isRateLimited = makeRateLimiter(
    config.rateLimitMax,
    config.rateLimitWindowMs,
  );

  return createServer((req, res) => {
    const started = Date.now();
    const ip = peerIp(req);
    const path = new URL(req.url ?? '/', 'http://localhost').pathname;

    // Scrubbed audit on completion — never logs the body or auth headers.
    // `ip` is the trusted peer; `fwdFor` is the client-claimed (untrusted) hop.
    res.on('finish', () => {
      logger.info(
        {
          ingress: true,
          method: req.method,
          path,
          ip,
          fwdFor: forwardedFor(req),
          status: res.statusCode,
          ms: Date.now() - started,
        },
        'ingress request',
      );
    });

    // 1) IP allowlist (optional, coarse; checks the unspoofable peer, not XFF).
    if (allow.size > 0 && !allow.has(ip)) {
      writeStatus(res, 403, 'forbidden');
      return;
    }

    // 2) Rate limit per peer IP.
    if (isRateLimited(ip, started)) {
      writeStatus(res, 429, 'rate limited');
      return;
    }

    // 3) Reserved health endpoint (no handler needed).
    if (req.method === 'GET' && path === '/health') {
      writeStatus(res, 200, 'ok');
      return;
    }

    // 4) Route lookup (Strategy registry; live read of the shared array).
    const handler = handlers.find((h) => path.startsWith(h.pathPrefix));
    if (!handler) {
      writeStatus(res, 404, 'no route');
      return;
    }

    // 5) Accumulate body with a hard cap (413 + drain on overflow).
    const chunks: Buffer[] = [];
    let size = 0;
    let overflowed = false;
    req.on('data', (c: Buffer) => {
      if (overflowed) return;
      size += c.length;
      if (size > config.maxBodyBytes) {
        overflowed = true;
        writeStatus(res, 413, 'payload too large');
        req.resume(); // drain so the socket doesn't hang
        return;
      }
      chunks.push(c);
    });
    req.on('end', () => {
      if (overflowed) return;
      const body = Buffer.concat(chunks);
      void (async () => {
        try {
          const v = await handler.verify(req, body);
          if (!v.ok) {
            writeStatus(res, 403, v.reason ?? 'verification failed');
            return;
          }
          await handler.handle(req, res, body);
          if (!res.writableEnded) res.end();
        } catch (err) {
          logger.error({ err, path }, 'ingress handler error');
          if (!res.headersSent) writeStatus(res, 500, 'internal error');
        }
      })();
    });
    req.on('error', () => {
      if (!res.headersSent) writeStatus(res, 400, 'bad request');
    });
  });
}

/** Start the gateway; resolves once listening. Rejects (fail-closed) on bind error. */
export function startIngressGateway(deps: GatewayDeps): Promise<Server> {
  return new Promise((resolve, reject) => {
    const server = createIngressGateway(deps);
    server.once('error', reject);
    server.listen(deps.config.port, deps.config.host, () => {
      server.off('error', reject);
      logger.info(
        { host: deps.config.host, port: deps.config.port },
        'ingress-gateway: server started',
      );
      resolve(server);
    });
  });
}
