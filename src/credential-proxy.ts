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
import { validateGroupToken } from './group-tokens.js';
import { readEnvFile } from './env.js';
import { logger } from './logger.js';
import {
  AuthProviderRegistry,
  AnthropicAuthProvider,
  _resetCredentialsCacheForTest as _resetAnthropicCache,
  ensureDefaultProviders,
} from './auth-providers/index.js';
import type { AuthProvider } from './auth-providers/types.js';

export type AuthMode = 'api-key' | 'oauth';

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

/* ── Rate limiter (in-process, per-source) ─────────────────────────── */

const RATE_LIMIT_MAX = 5;
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

  // Get the Anthropic provider for logging the auth mode
  let authMode: AuthMode = 'oauth';
  try {
    const anthropic = registry.get('anthropic');
    if (anthropic instanceof AnthropicAuthProvider) {
      authMode = anthropic.getAuthMode();
    }
  } catch {
    // No Anthropic provider registered — unusual but not fatal
  }

  return new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => {
        const body = Buffer.concat(chunks);

        if (DEUS_PROXY_AUTH_ENABLED) {
          const token = req.headers['x-deus-proxy-token'] as string | undefined;
          const groupFolder = token ? validateGroupToken(token) : null;
          if (!groupFolder) {
            logger.warn(
              { url: req.url, hasToken: !!token },
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
          const sourceKey =
            (req.headers['x-deus-source'] as string) ||
            req.socket.remoteAddress ||
            'unknown';

          if (isRateLimited(sourceKey)) {
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

        upstream.write(body);
        upstream.end();
      });
    });

    server.on('close', () => {
      clearInterval(rateLimitCleanupInterval);
    });

    let retries = 0;
    const maxRetries = 10;
    const retryDelay = 2000;

    const tryListen = () => {
      server.listen(port, host, () => {
        logger.info({ port, host, authMode }, 'Credential proxy started');
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
