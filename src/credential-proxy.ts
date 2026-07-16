/**
 * Credential proxy for container isolation.
 * Containers connect here instead of directly to API providers.
 * The proxy injects real credentials so containers never see them.
 *
 * Auth is delegated to AuthProvider implementations (see auth-providers/).
 *
 * Path-prefix routing:
 *   /anthropic/*  → Anthropic provider (prefix stripped)
 *   /openai/*     → OpenAI provider (prefix stripped, if registered)
 *   /gemini/*     → Gemini provider (prefix stripped, if registered)
 *   /*            → Anthropic provider (backward compatibility)
 *
 * OAuth token resolution order (per-request, with 5-min cache):
 *   1. CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_AUTH_TOKEN from env file (explicit override)
 *   2. ~/.claude/.credentials.json (auto-refreshed by Claude Code CLI)
 */
import { createServer, Server } from 'http';
import { request as httpsRequest } from 'https';
import { request as httpRequest, RequestOptions } from 'http';
import { execFile } from 'child_process';
import path from 'path';
import { DEUS_PROXY_AUTH_ENABLED } from './config.js';
import { envPositiveInt } from './env-utils.js';
import { validateGroupToken } from './group-tokens.js';
import { readEnvFile } from './env.js';
import { logger } from './logger.js';
import {
  AuthProviderRegistry,
  AnthropicAuthProvider,
  CREDENTIALS_PATH,
  triggerProactiveOAuthRefresh,
  _resetCredentialsCacheForTest as _resetAnthropicCache,
  ensureDefaultProviders,
} from './auth-providers/index.js';
import type { AuthProvider } from './auth-providers/types.js';

export type AuthMode = 'api-key' | 'oauth';

/**
 * How often the always-on host pokes the OAuth token-read/refresh path even
 * with no incoming container traffic. Chosen to equal the early-expire window
 * (EARLY_EXPIRE_WINDOW_MS = 30 min): interval ≤ window guarantees a tick lands
 * inside the refresh window before the token expires, leaving no blind spot.
 * It also matches the launchd job's cadence (StartInterval 1800). A tick on a
 * comfortably-valid token is just a single file read (getDynamicOAuthToken
 * fast-paths it); the keychain/refresh only fire inside the window.
 */
const PROACTIVE_REFRESH_INTERVAL_MS = 30 * 60 * 1000;

export interface ProxyConfig {
  authMode: AuthMode;
}

/** @internal exposed for testing only */
export function _resetCredentialsCacheForTest(): void {
  _resetAnthropicCache();
}

/* ── Memory bridge constants ───────────────────────────────────────── */

const PYTHON_BIN = process.env.DEUS_PYTHON ?? 'python3';
const MEMORY_QUERY_SCRIPT = path.join(
  process.cwd(),
  'scripts',
  'memory_query.py',
);
const MEMORY_QUERY_TIMEOUT_MS = 4_000;

// LIA-354: server-side recall bounds for the container bridge — never read
// from the request body (containers must not be able to lift their own cap).
// 8192 mirrors the MCP server's _MAX_CONTEXT_CHARS (memory_mcp_server.py,
// LIA-344 sizing). Env-read is lazy (per request) so overrides are testable
// without module re-import; the cost is negligible next to the execFile.
const MEMORY_QUERY_DEFAULT_MAX_CHARS = 8192;
// Exact vault-relative paths — coupled to the index files living at the vault
// ROOT. A vault reorg that moves them silently un-blocks them (no error).
const MEMORY_QUERY_DEFAULT_EXCLUDE = 'CLAUDE.md,INFRA.md';

/** Extra memory_query.py args enforcing the bridge cap + index-file blocklist (LIA-354). */
function bridgeRecallBoundArgs(): string[] {
  // LIA-354: config plumbing with a validated fallback, not a feature gate.
  const rawCap = Number(process.env.DEUS_BRIDGE_RECALL_MAX_CHARS);
  const cap =
    Number.isInteger(rawCap) && rawCap > 0
      ? rawCap
      : MEMORY_QUERY_DEFAULT_MAX_CHARS;
  // LIA-354: per-surface blocklist override, same config-plumbing shape.
  const exclude = (
    process.env.DEUS_BRIDGE_RECALL_EXCLUDE ?? MEMORY_QUERY_DEFAULT_EXCLUDE
  ).trim();
  const args = ['--max-context-chars', String(cap)];
  if (exclude) args.push('--exclude-paths', exclude);
  return args;
}

/* ── Rate limiter (in-process, keyed per authenticated group) ──────── */

// 20/min per group: the /memory/query bucket is keyed on the authenticated
// groupFolder (LIA-244), so this is a per-group budget — roughly a chatty
// conversation's few requests per turn across a burst of turns. The old global
// 5/min was shared across every group/container, so a single active chat
// silently lost memory context (the container maps a 429 to empty context with
// no telemetry).
const RATE_LIMIT_MAX = 20;
const RATE_LIMIT_WINDOW_MS = 60_000;

interface RateBucket {
  timestamps: number[];
}

const rateBuckets = new Map<string, RateBucket>();

/** Prune expired entries periodically to prevent unbounded growth. */
const rateLimitCleanupInterval = setInterval(() => {
  const now = Date.now();
  for (const [key, bucket] of rateBuckets) {
    bucket.timestamps = bucket.timestamps.filter(
      (t) => now - t < RATE_LIMIT_WINDOW_MS,
    );
    if (bucket.timestamps.length === 0) rateBuckets.delete(key);
  }
}, RATE_LIMIT_WINDOW_MS);

// Prevent the cleanup timer from keeping Node alive after tests/shutdown
rateLimitCleanupInterval.unref();

/** @internal exposed for testing only */
export function _resetRateLimiterForTest(): void {
  rateBuckets.clear();
}

function isRateLimited(sourceKey: string): boolean {
  const now = Date.now();
  let bucket = rateBuckets.get(sourceKey);
  if (!bucket) {
    bucket = { timestamps: [] };
    rateBuckets.set(sourceKey, bucket);
  }
  // Prune expired timestamps for this source
  bucket.timestamps = bucket.timestamps.filter(
    (t) => now - t < RATE_LIMIT_WINDOW_MS,
  );
  if (bucket.timestamps.length >= RATE_LIMIT_MAX) return true;
  bucket.timestamps.push(now);
  return false;
}

/**
 * Resolve provider and path from a request URL.
 *
 * Path-prefix routing:
 *   /anthropic/v1/messages → provider='anthropic', path='/v1/messages'
 *   /openai/v1/chat        → provider='openai',    path='/v1/chat'
 *   /v1/messages            → provider='anthropic', path='/v1/messages' (default)
 *
 * @internal exported for testing
 */
export function resolveProviderRoute(
  url: string,
  registry: AuthProviderRegistry,
): { provider: AuthProvider; path: string } {
  // Check for provider prefix: /<provider-name>/rest/of/path
  const prefixMatch = url.match(/^\/([a-z]+)(\/.*)?$/);
  if (prefixMatch) {
    const prefix = prefixMatch[1];
    const rest = prefixMatch[2] || '/';
    // Only treat as a provider prefix if it's actually registered
    if (registry.listProviders().includes(prefix)) {
      return { provider: registry.get(prefix), path: rest };
    }
  }

  // Default: route to Anthropic for backward compatibility
  return { provider: registry.get('anthropic'), path: url || '/' };
}

export function startCredentialProxy(
  port: number,
  host = '127.0.0.1',
): Promise<Server> {
  // Lazily register built-in providers (deferred to avoid breaking test mocks)
  ensureDefaultProviders();
  const registry = AuthProviderRegistry.default();

  // Get the Anthropic provider for logging the auth mode + deciding whether the
  // proactive refresh timer is worth running (only for refreshable OAuth creds).
  let authMode: AuthMode = 'oauth';
  let usesRefreshableOAuth = false;
  try {
    const anthropic = registry.get('anthropic');
    if (anthropic instanceof AnthropicAuthProvider) {
      authMode = anthropic.getAuthMode();
      usesRefreshableOAuth = anthropic.usesRefreshableOAuth();
    }
  } catch {
    // No Anthropic provider registered — unusual but not fatal
  }

  // Proactive OAuth refresh timer. The per-request refresh in the Anthropic
  // provider only fires when a container makes a request; an idle host (no
  // overnight traffic) never triggers it, so the token expires and the next
  // morning's request 401s (issue #625). This in-process timer pokes the same
  // token-read/refresh path on an interval so refresh happens proactively.
  //
  // Cross-platform defense-in-depth: the launchd job (scheduleOAuthRefresh) is
  // macOS-only and can fail to load silently, leaving Linux/Windows hosts with
  // no proactive refresh at all. This timer lives in the always-on host process
  // and covers every platform. Overlap with a request-triggered refresh or the
  // launchd CLI is already handled (in-process `refreshInFlight` flag + the
  // CLI's file lock; credential writes are atomic).
  let proactiveRefreshTimer: ReturnType<typeof setInterval> | undefined;
  if (usesRefreshableOAuth) {
    proactiveRefreshTimer = setInterval(
      triggerProactiveOAuthRefresh,
      PROACTIVE_REFRESH_INTERVAL_MS,
    );
    // Don't keep the Node process alive on the timer alone (tests/shutdown).
    proactiveRefreshTimer.unref();
    logger.info(
      { intervalMs: PROACTIVE_REFRESH_INTERVAL_MS },
      'Credential proxy: proactive OAuth refresh timer started',
    );
  }

  // Cap the buffered request body to bound host memory (LIA-236). Generous for
  // multimodal base64 image payloads; the proxy binds 127.0.0.1 so this is a
  // robustness bound, not an attack surface.
  const maxBodyBytes = envPositiveInt(
    'DEUS_PROXY_MAX_BODY_BYTES',
    32 * 1024 * 1024,
  );
  // Inactivity ceiling for a black-holed upstream connection (LIA-236). Socket
  // inactivity timeout — a live SSE stream resets it on every chunk, so only a
  // genuinely stalled/dead connection trips it.
  const upstreamTimeoutMs = envPositiveInt(
    'DEUS_PROXY_UPSTREAM_TIMEOUT_MS',
    600_000,
  );

  return new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      let bodySize = 0;
      let bodyTooLarge = false;
      // A client that disconnects mid-upload emits 'error' on the request
      // stream; an unhandled stream 'error' is a fatal uncaught exception, so
      // swallow it here (LIA-236).
      req.on('error', (err) => {
        logger.debug(
          { err, url: req.url },
          'credential-proxy: request stream error',
        );
      });
      req.on('data', (c) => {
        if (bodyTooLarge) return;
        bodySize += c.length;
        if (bodySize > maxBodyBytes) {
          // Stop storing the body (bounds host memory) and reject. Respond with
          // Connection: close so Node discards the rest of the upload on close —
          // cleaner than req.destroy(), which would RST the shared socket and
          // can truncate this very response before the client reads it.
          bodyTooLarge = true;
          logger.warn(
            { url: req.url, maxBodyBytes },
            'credential-proxy: request body exceeded size limit',
          );
          res.writeHead(413, { Connection: 'close' });
          res.end('Payload Too Large');
          return;
        }
        chunks.push(c);
      });
      req.on('end', () => {
        if (bodyTooLarge) return; // already responded 413 above
        const body = Buffer.concat(chunks);

        // Also keys the /memory/query rate limiter per authenticated group;
        // null when auth is off (falls back to the socket address). (LIA-244)
        let groupFolder: string | null = null;
        if (DEUS_PROXY_AUTH_ENABLED) {
          const token = req.headers['x-deus-proxy-token'] as string | undefined;
          groupFolder = token ? validateGroupToken(token) : null;
          if (!groupFolder) {
            logger.error(
              { statusCode: 401, url: req.url, hasToken: !!token },
              'Credential proxy rejected unauthenticated request',
            );
            res.writeHead(401);
            res.end('Unauthorized');
            return;
          }
          logger.debug(
            { url: req.url, group: groupFolder },
            'Proxy request authenticated',
          );
        }

        // Strip proxy-internal headers before forwarding upstream
        delete req.headers['x-deus-proxy-token'];
        delete req.headers['x-deus-group'];

        /* ── Memory bridge route: POST /memory/query ───────────── */
        if (req.method === 'POST' && req.url === '/memory/query') {
          // Rate-limit per AUTHENTICATED group, not the client-supplied
          // x-deus-source header — that header is the constant 'container-claude'
          // for every Claude container, so it collapsed all groups into one shared
          // global bucket, and it is spoofable. Fall back to the socket address
          // when proxy auth is disabled. (LIA-244)
          const rateKey = groupFolder ?? req.socket.remoteAddress ?? 'unknown';

          if (isRateLimited(rateKey)) {
            logger.warn(
              { group: rateKey },
              'Memory bridge rate limit exceeded',
            );
            res.writeHead(429, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Rate limit exceeded' }));
            return;
          }

          let parsed: { query?: unknown; k?: unknown; source?: unknown };
          try {
            parsed = JSON.parse(body.toString('utf-8'));
          } catch {
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ error: 'Invalid JSON body' }));
            return;
          }

          if (typeof parsed.query !== 'string' || parsed.query.length === 0) {
            res.writeHead(400, { 'Content-Type': 'application/json' });
            res.end(
              JSON.stringify({ error: 'Missing or empty "query" field' }),
            );
            return;
          }

          const queryArg = parsed.query;
          const kArg = typeof parsed.k === 'number' ? String(parsed.k) : '3';
          const sourceArg =
            typeof parsed.source === 'string' ? parsed.source : 'bridge';

          const args = [
            MEMORY_QUERY_SCRIPT,
            queryArg,
            '--json',
            '--source',
            sourceArg,
            '-k',
            kArg,
            ...bridgeRecallBoundArgs(),
          ];

          execFile(
            PYTHON_BIN,
            args,
            { timeout: MEMORY_QUERY_TIMEOUT_MS },
            (err, stdout, _stderr) => {
              const errAny = err as NodeJS.ErrnoException & {
                killed?: boolean;
                signal?: string;
              };
              if (
                errAny &&
                (errAny.killed ||
                  errAny.signal === 'SIGTERM' ||
                  errAny.code === 'ETIMEDOUT')
              ) {
                logger.warn('Memory query timed out');
                res.writeHead(504, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ error: 'Memory query timed out' }));
                return;
              }
              if (err) {
                logger.error({ err }, 'Memory query failed');
                res.writeHead(500, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ error: 'Memory query failed' }));
                return;
              }

              try {
                const result = JSON.parse(stdout);
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify(result));
              } catch {
                logger.error({ stdout }, 'Memory query returned invalid JSON');
                res.writeHead(500, { 'Content-Type': 'application/json' });
                res.end(
                  JSON.stringify({
                    error: 'Memory query returned invalid output',
                  }),
                );
              }
            },
          );
          return;
        }

        // Resolve which provider handles this request
        let provider: AuthProvider;
        let upstreamPath: string;
        try {
          const route = resolveProviderRoute(req.url || '/', registry);
          provider = route.provider;
          upstreamPath = route.path;
        } catch (err) {
          logger.error(
            { err, url: req.url },
            'No provider available for request',
          );
          res.writeHead(502);
          res.end('No provider available');
          return;
        }

        const upstreamUrl = new URL(provider.getUpstreamUrl());
        const isHttps = upstreamUrl.protocol === 'https:';
        const makeRequest = isHttps ? httpsRequest : httpRequest;

        const headers: Record<string, string | number | string[] | undefined> =
          {
            ...(req.headers as Record<string, string>),
            host: upstreamUrl.host,
            'content-length': body.length,
          };

        // Strip hop-by-hop headers that must not be forwarded by proxies
        delete headers['connection'];
        delete headers['keep-alive'];
        delete headers['transfer-encoding'];

        // Delegate auth injection to the provider
        provider.injectAuth(
          headers as Record<string, string | string[] | undefined>,
        );

        const upstream = makeRequest(
          {
            hostname: upstreamUrl.hostname,
            port: upstreamUrl.port || (isHttps ? 443 : 80),
            path: upstreamPath,
            method: req.method,
            headers,
          } as RequestOptions,
          (upRes) => {
            if (upRes.statusCode === 401) {
              logger.error(
                { statusCode: 401, provider: provider.name, url: req.url },
                'Credential proxy received 401 from upstream — auth failure',
              );
            }
            res.writeHead(upRes.statusCode!, upRes.headers);
            upRes.pipe(res);
          },
        );

        upstream.on('error', (err) => {
          logger.error(
            { err, url: req.url },
            'Credential proxy upstream error',
          );
          if (!res.headersSent) {
            res.writeHead(502);
            res.end('Bad Gateway');
          }
        });

        // Bound a black-holed upstream: socket inactivity timeout → destroy,
        // which surfaces through the error handler above as a 502 (LIA-236).
        upstream.setTimeout(upstreamTimeoutMs, () => {
          upstream.destroy(new Error('upstream timeout'));
        });

        // Client aborted mid-response (e.g. SDK gave up on a long SSE stream):
        // pipe() does not propagate the destination close to the source, so
        // destroy the upstream to free its socket. writableEnded is false on an
        // abort, true on a normal completion (LIA-236).
        res.on('close', () => {
          if (!res.writableEnded) upstream.destroy();
        });

        upstream.write(body);
        upstream.end();
      });
    });

    let retries = 0;
    const maxRetries = 10;
    const retryDelay = 2000;

    const tryListen = () => {
      server.listen(port, host, () => {
        // Register the shutdown cleanup only AFTER a successful bind. Doing it
        // earlier means the EADDRINUSE retry's server.close() (below) fires the
        // 'close' listener and clears proactiveRefreshTimer, so the proxy then
        // binds on a later attempt with proactive OAuth refresh permanently
        // dead — the exact "next-morning 401" class the timer prevents (LIA-363).
        server.on('close', () => {
          clearInterval(rateLimitCleanupInterval);
          if (proactiveRefreshTimer) clearInterval(proactiveRefreshTimer);
        });
        logger.info(
          { port, host, authMode, credentialsPath: CREDENTIALS_PATH },
          'Credential proxy started',
        );
        resolve(server);
      });
    };

    server.on('error', (err: NodeJS.ErrnoException) => {
      if (err.code === 'EADDRINUSE' && retries < maxRetries) {
        retries++;
        logger.warn(
          { port, attempt: retries, maxRetries },
          'Port in use, retrying...',
        );
        server.close();
        setTimeout(tryListen, retryDelay);
      } else {
        reject(err);
      }
    });

    tryListen();
  });
}

/** Detect which auth mode the host is configured for. */
export function detectAuthMode(): AuthMode {
  try {
    const registry = AuthProviderRegistry.default();
    const anthropic = registry.get('anthropic');
    if (anthropic instanceof AnthropicAuthProvider) {
      return anthropic.getAuthMode();
    }
  } catch {
    // Fallback to direct env check if registry not available
  }
  // Fallback: read env directly (same logic as before)
  const secrets = readEnvFile(['ANTHROPIC_API_KEY']);
  return secrets.ANTHROPIC_API_KEY ? 'api-key' : 'oauth';
}
