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
