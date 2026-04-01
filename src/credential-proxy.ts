/**
 * Credential proxy for container isolation.
 * Containers connect here instead of directly to the Anthropic API.
 * The proxy injects real credentials so containers never see them.
 *
 * Two auth modes:
 *   API key:  Proxy injects x-api-key on every request.
 *   OAuth:    Container CLI exchanges its placeholder token for a temp
 *             API key via /api/oauth/claude_cli/create_api_key.
 *             Proxy injects real OAuth token on that exchange request;
 *             subsequent requests carry the temp key which is valid as-is.
 *
 * OAuth token resolution order (per-request, with 5-min cache):
 *   1. CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_AUTH_TOKEN from env file (explicit override)
 *   2. ~/.claude/.credentials.json (auto-refreshed by Claude Code CLI)
 */
import { readFileSync } from 'fs';
import { createServer, Server } from 'http';
import { request as httpsRequest } from 'https';
import { request as httpRequest, RequestOptions } from 'http';
import os from 'os';
import path from 'path';
import { readEnvFile } from './env.js';
import { logger } from './logger.js';

export type AuthMode = 'api-key' | 'oauth';

export interface ProxyConfig {
  authMode: AuthMode;
}

// ---------------------------------------------------------------------------
// Dynamic OAuth token — read from ~/.claude/.credentials.json with a 5-min TTL.
// The env-file value always takes priority when set.
// ---------------------------------------------------------------------------
const CREDENTIALS_PATH = path.join(
  process.env.HOME || os.homedir(),
  '.claude',
  '.credentials.json',
);
const CACHE_TTL_MS = 5 * 60 * 1000;
const EARLY_EXPIRE_WINDOW_MS = 5 * 60 * 1000;

interface CredentialsCache {
  token: string;
  fetchedAt: number;
  tokenExpiresAt: number;
}

let credentialsCache: CredentialsCache | null = null;

/** @internal exposed for testing only */
export function _resetCredentialsCacheForTest(): void {
  credentialsCache = null;
}

function readCredentialsFile(): { token: string; expiresAt: number } | undefined {
  try {
    const raw = readFileSync(CREDENTIALS_PATH, 'utf-8');
    const parsed = JSON.parse(raw) as {
      claudeAiOauth?: { accessToken?: string; expiresAt?: number };
    };
    const oauth = parsed?.claudeAiOauth;
    if (!oauth?.accessToken) return undefined;
    return { token: oauth.accessToken, expiresAt: oauth.expiresAt ?? Infinity };
  } catch {
    return undefined;
  }
}

function getDynamicOAuthToken(): string | undefined {
  const now = Date.now();
  if (credentialsCache) {
    const cacheAge = now - credentialsCache.fetchedAt;
    const aboutToExpire =
      credentialsCache.tokenExpiresAt !== Infinity &&
      credentialsCache.tokenExpiresAt < now + EARLY_EXPIRE_WINDOW_MS;
    if (cacheAge < CACHE_TTL_MS && !aboutToExpire) return credentialsCache.token;
  }
  const creds = readCredentialsFile();
  if (!creds) return undefined;
  credentialsCache = { token: creds.token, fetchedAt: now, tokenExpiresAt: creds.expiresAt };
  return creds.token;
}

export function startCredentialProxy(
  port: number,
  host = '127.0.0.1',
): Promise<Server> {
  const secrets = readEnvFile([
    'ANTHROPIC_API_KEY',
    'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_BASE_URL',
  ]);

  const authMode: AuthMode = secrets.ANTHROPIC_API_KEY ? 'api-key' : 'oauth';
  // env-file value takes priority; otherwise resolved dynamically per-request
  const envOauthToken =
    secrets.CLAUDE_CODE_OAUTH_TOKEN || secrets.ANTHROPIC_AUTH_TOKEN;

  const upstreamUrl = new URL(
    secrets.ANTHROPIC_BASE_URL || 'https://api.anthropic.com',
  );
  const isHttps = upstreamUrl.protocol === 'https:';
  const makeRequest = isHttps ? httpsRequest : httpRequest;

  return new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      const chunks: Buffer[] = [];
      req.on('data', (c) => chunks.push(c));
      req.on('end', () => {
        const body = Buffer.concat(chunks);
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

        if (authMode === 'api-key') {
          // API key mode: inject x-api-key on every request
          delete headers['x-api-key'];
          headers['x-api-key'] = secrets.ANTHROPIC_API_KEY;
        } else {
          // OAuth mode: replace placeholder Bearer token with the real one
          // only when the container actually sends an Authorization header
          // (exchange request + auth probes). Post-exchange requests use
          // x-api-key only, so they pass through without token injection.
          if (headers['authorization']) {
            delete headers['authorization'];
            const token = envOauthToken || getDynamicOAuthToken();
            if (token) {
              headers['authorization'] = `Bearer ${token}`;
            }
          }
        }

        const upstream = makeRequest(
          {
            hostname: upstreamUrl.hostname,
            port: upstreamUrl.port || (isHttps ? 443 : 80),
            path: req.url,
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
  const secrets = readEnvFile(['ANTHROPIC_API_KEY']);
  return secrets.ANTHROPIC_API_KEY ? 'api-key' : 'oauth';
}
