import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import http from 'http';
import type { AddressInfo } from 'net';

vi.mock('./config.js', () => ({
  DEUS_PROXY_AUTH_ENABLED: true,
}));

vi.mock('./group-tokens.js', () => ({
  validateGroupToken: (token: string) =>
    token === 'test-proxy-token-abc123' ? 'test-group' : null,
}));

const mockEnv: Record<string, string> = {};
vi.mock('./env.js', () => ({
  readEnvFile: vi.fn(() => ({ ...mockEnv })),
}));

vi.mock('./logger.js', () => ({
  logger: { info: vi.fn(), error: vi.fn(), debug: vi.fn(), warn: vi.fn() },
}));

vi.mock('fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('fs')>();
  return { ...actual, readFileSync: vi.fn(actual.readFileSync) };
});

vi.mock('child_process', () => ({
  execFileSync: vi.fn(),
  execSync: vi.fn(),
}));

import { readFileSync } from 'fs';
import { execFileSync } from 'child_process';
import {
  startCredentialProxy,
  _resetCredentialsCacheForTest,
} from './credential-proxy.js';
import { AuthProviderRegistry } from './auth-providers/types.js';
import * as configModule from './config.js';

const TEST_PROXY_TOKEN = 'test-proxy-token-abc123';

const mockReadFileSync = readFileSync as ReturnType<typeof vi.fn>;
const mockExecFileSync = vi.mocked(execFileSync);

function makeRequest(
  port: number,
  options: http.RequestOptions,
  body = '',
): Promise<{
  statusCode: number;
  body: string;
  headers: http.IncomingHttpHeaders;
}> {
  return new Promise((resolve, reject) => {
    const req = http.request(
      { ...options, hostname: '127.0.0.1', port },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          resolve({
            statusCode: res.statusCode!,
            body: Buffer.concat(chunks).toString(),
            headers: res.headers,
          });
        });
      },
    );
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

describe('credential-proxy', () => {
  let proxyServer: http.Server | undefined;
  let upstreamServer: http.Server | undefined;
  let proxyPort: number;
  let upstreamPort: number;
  let lastUpstreamHeaders: http.IncomingHttpHeaders;

  async function closeServer(server: http.Server | undefined): Promise<void> {
    if (!server) return;
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }

  beforeEach(async () => {
    lastUpstreamHeaders = {};
    // Reset registry so each test gets a fresh AnthropicAuthProvider
    // that reads the current mockEnv at construction time.
    AuthProviderRegistry.reset();
    _resetCredentialsCacheForTest();
    // Default: credentials file missing — existing tests are unaffected
    mockReadFileSync.mockImplementation(() => {
      throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
    });
    // Default: keychain lookup fails — prevents real OS credentials leaking
    // into tests on macOS/Linux/Windows dev machines.
    mockExecFileSync.mockImplementation(() => {
      throw new Error('no keychain (test isolation)');
    });

    const server = http.createServer((req, res) => {
      lastUpstreamHeaders = { ...req.headers };
      res.writeHead(200, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ ok: true }));
    });
    upstreamServer = server;
    await new Promise<void>((resolve) =>
      server.listen(0, '127.0.0.1', resolve),
    );
    upstreamPort = (server.address() as AddressInfo).port;
  });

  afterEach(async () => {
    await closeServer(proxyServer);
    await closeServer(upstreamServer);
    for (const key of Object.keys(mockEnv)) delete mockEnv[key];
    mockReadFileSync.mockReset();
    mockExecFileSync.mockReset();
    _resetCredentialsCacheForTest();
    AuthProviderRegistry.reset();
    Object.defineProperty(configModule, 'DEUS_PROXY_AUTH_ENABLED', {
      value: true,
      writable: true,
    });
  });

  function withProxyToken(options: http.RequestOptions): http.RequestOptions {
    return {
      ...options,
      headers: {
        ...options.headers,
        'x-deus-proxy-token': TEST_PROXY_TOKEN,
      },
    };
  }

  async function startProxy(env: Record<string, string>): Promise<number> {
    Object.assign(mockEnv, {
      ANTHROPIC_BASE_URL: `http://127.0.0.1:${upstreamPort}`,
      ...env,
    });
    proxyServer = await startCredentialProxy(0);
    return (proxyServer.address() as AddressInfo).port;
  }

  it('API-key mode injects x-api-key and strips placeholder', async () => {
    proxyPort = await startProxy({ ANTHROPIC_API_KEY: 'sk-ant-real-key' });

    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/v1/messages',
        headers: {
          'content-type': 'application/json',
          'x-api-key': 'placeholder',
        },
      }),
      '{}',
    );

    expect(lastUpstreamHeaders['x-api-key']).toBe('sk-ant-real-key');
  });

  it('routes /openai requests to the OpenAI provider and injects bearer auth', async () => {
    proxyPort = await startProxy({
      OPENAI_API_KEY: 'sk-openai-real-key',
      OPENAI_BASE_URL: `http://127.0.0.1:${upstreamPort}`,
    });

    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/openai/v1/responses',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
          'x-api-key': 'temp-key',
        },
      }),
      '{}',
    );

    expect(lastUpstreamHeaders['authorization']).toBe(
      'Bearer sk-openai-real-key',
    );
    expect(lastUpstreamHeaders['x-api-key']).toBeUndefined();
  });

  it('OAuth mode replaces Authorization when container sends one', async () => {
    proxyPort = await startProxy({
      CLAUDE_CODE_OAUTH_TOKEN: 'real-oauth-token',
    });

    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      }),
      '{}',
    );

    expect(lastUpstreamHeaders['authorization']).toBe(
      'Bearer real-oauth-token',
    );
  });

  it('OAuth mode does not inject Authorization when container omits it', async () => {
    proxyPort = await startProxy({
      CLAUDE_CODE_OAUTH_TOKEN: 'real-oauth-token',
    });

    // Post-exchange: container uses x-api-key only, no Authorization header
    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/v1/messages',
        headers: {
          'content-type': 'application/json',
          'x-api-key': 'temp-key-from-exchange',
        },
      }),
      '{}',
    );

    expect(lastUpstreamHeaders['x-api-key']).toBe('temp-key-from-exchange');
    expect(lastUpstreamHeaders['authorization']).toBeUndefined();
  });

  it('strips hop-by-hop headers', async () => {
    proxyPort = await startProxy({ ANTHROPIC_API_KEY: 'sk-ant-real-key' });

    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/v1/messages',
        headers: {
          'content-type': 'application/json',
          connection: 'keep-alive',
          'keep-alive': 'timeout=5',
          'transfer-encoding': 'chunked',
        },
      }),
      '{}',
    );

    // Proxy strips client hop-by-hop headers. Node's HTTP client may re-add
    // its own Connection header (standard HTTP/1.1 behavior), but the client's
    // custom keep-alive and transfer-encoding must not be forwarded.
    expect(lastUpstreamHeaders['keep-alive']).toBeUndefined();
    expect(lastUpstreamHeaders['transfer-encoding']).toBeUndefined();
  });

  it('returns 502 when upstream is unreachable', async () => {
    Object.assign(mockEnv, {
      ANTHROPIC_API_KEY: 'sk-ant-real-key',
      ANTHROPIC_BASE_URL: 'http://127.0.0.1:59999',
    });
    proxyServer = await startCredentialProxy(0);
    proxyPort = (proxyServer.address() as AddressInfo).port;

    const res = await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/v1/messages',
        headers: { 'content-type': 'application/json' },
      }),
      '{}',
    );

    expect(res.statusCode).toBe(502);
    expect(res.body).toBe('Bad Gateway');
  });

  it('OAuth mode reads token from credentials file when env token absent', async () => {
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'creds-file-token-valid-test',
          expiresAt: Date.now() + 60 * 60 * 1000,
        },
      }),
    );

    proxyPort = await startProxy({});

    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      }),
      '{}',
    );

    expect(lastUpstreamHeaders['authorization']).toBe(
      'Bearer creds-file-token-valid-test',
    );
  });

  it('OAuth mode: env CLAUDE_CODE_OAUTH_TOKEN takes priority over credentials file', async () => {
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'creds-file-token-valid-test',
          expiresAt: Date.now() + 60 * 60 * 1000,
        },
      }),
    );

    proxyPort = await startProxy({ CLAUDE_CODE_OAUTH_TOKEN: 'env-token' });

    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      }),
      '{}',
    );

    expect(lastUpstreamHeaders['authorization']).toBe('Bearer env-token');
  });

  it('OAuth mode: re-reads credentials file when cached token is about to expire', async () => {
    // Constructor validation read (consumed during AnthropicAuthProvider init)
    mockReadFileSync.mockReturnValueOnce(
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'expiring-token-valid-test',
          expiresAt: Date.now() + 2 * 60 * 1000,
        },
      }),
    );
    // First request: token expiring in 2 min (within 30-min early-expire window)
    mockReadFileSync.mockReturnValueOnce(
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'expiring-token-valid-test',
          expiresAt: Date.now() + 2 * 60 * 1000,
        },
      }),
    );
    // Second request: refreshed token
    mockReadFileSync.mockReturnValueOnce(
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'refreshed-token-valid-test',
          expiresAt: Date.now() + 60 * 60 * 1000,
        },
      }),
    );

    proxyPort = await startProxy({});

    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      }),
      '{}',
    );
    expect(lastUpstreamHeaders['authorization']).toBe(
      'Bearer expiring-token-valid-test',
    );

    // Cache is stale (token about to expire) — re-read on next request
    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      }),
      '{}',
    );
    expect(lastUpstreamHeaders['authorization']).toBe(
      'Bearer refreshed-token-valid-test',
    );
  });

  it('OAuth mode replaces Bearer token on session endpoints', async () => {
    proxyPort = await startProxy({
      CLAUDE_CODE_OAUTH_TOKEN: 'real-oauth-token',
    });

    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/v1/sessions',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
          'anthropic-beta': 'ccr-byoc-2025-07-29',
        },
      }),
      '{}',
    );

    expect(lastUpstreamHeaders['authorization']).toBe(
      'Bearer real-oauth-token',
    );
  });

  it('OAuth mode: no crash when credentials file is missing', async () => {
    // mockReadFileSync already throws ENOENT by default

    proxyPort = await startProxy({});

    const res = await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      }),
      '{}',
    );

    expect(res.statusCode).toBe(200);
    expect(lastUpstreamHeaders['authorization']).toBeUndefined();
  });

  it('rejects request without proxy token', async () => {
    proxyPort = await startProxy({ ANTHROPIC_API_KEY: 'sk-ant-real-key' });

    const res = await makeRequest(
      proxyPort,
      {
        method: 'POST',
        path: '/v1/messages',
        headers: { 'content-type': 'application/json' },
      },
      '{}',
    );

    expect(res.statusCode).toBe(401);
    expect(res.body).toBe('Unauthorized');
  });

  it('rejects request with wrong proxy token', async () => {
    proxyPort = await startProxy({ ANTHROPIC_API_KEY: 'sk-ant-real-key' });

    const res = await makeRequest(
      proxyPort,
      {
        method: 'POST',
        path: '/v1/messages',
        headers: {
          'content-type': 'application/json',
          'x-deus-proxy-token': 'wrong-token',
        },
      },
      '{}',
    );

    expect(res.statusCode).toBe(401);
    expect(res.body).toBe('Unauthorized');
  });

  it('does not forward x-deus-proxy-token to upstream', async () => {
    proxyPort = await startProxy({ ANTHROPIC_API_KEY: 'sk-ant-real-key' });

    await makeRequest(
      proxyPort,
      withProxyToken({
        method: 'POST',
        path: '/v1/messages',
        headers: { 'content-type': 'application/json' },
      }),
      '{}',
    );

    expect(lastUpstreamHeaders['x-deus-proxy-token']).toBeUndefined();
  });

  it('allows requests when auth is disabled via kill-switch', async () => {
    Object.defineProperty(configModule, 'DEUS_PROXY_AUTH_ENABLED', {
      value: false,
      writable: true,
    });
    proxyPort = await startProxy({ ANTHROPIC_API_KEY: 'sk-ant-real-key' });

    const res = await makeRequest(
      proxyPort,
      {
        method: 'POST',
        path: '/v1/messages',
        headers: { 'content-type': 'application/json' },
      },
      '{}',
    );

    expect(res.statusCode).toBe(200);
  });

  // -------------------------------------------------------------------------
  // Proactive OAuth refresh timer (issue #625): an idle host with no incoming
  // traffic must still refresh the token before it expires. The timer is only
  // worth running for refreshable (dynamic file/keychain) OAuth credentials.
  // -------------------------------------------------------------------------
  describe('proactive OAuth refresh timer', () => {
    const PROACTIVE_INTERVAL_MS = 30 * 60 * 1000;

    interface IntervalCall {
      delay: number;
      unrefed: boolean;
      handle: ReturnType<typeof setInterval>;
    }

    function collectIntervals(): {
      restore: () => void;
      calls: IntervalCall[];
    } {
      const calls: IntervalCall[] = [];
      const real = global.setInterval;
      const spy = vi
        .spyOn(global, 'setInterval')
        .mockImplementation(
          (
            handler: (...handlerArgs: unknown[]) => void,
            timeout?: number,
            ...args: unknown[]
          ) => {
            const handle = real(handler, timeout, ...args) as ReturnType<
              typeof setInterval
            >;
            // Don't let the test's intervals fire — we only assert on creation.
            handle.unref();
            const entry: IntervalCall = {
              delay: timeout ?? 0,
              unrefed: false,
              handle,
            };
            const origUnref = handle.unref.bind(handle);
            handle.unref = () => {
              entry.unrefed = true;
              return origUnref();
            };
            calls.push(entry);
            return handle;
          },
        );
      return { restore: () => spy.mockRestore(), calls };
    }

    it('starts an unref-ed 30-min timer for dynamic file OAuth credentials', async () => {
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'dynamic-creds-token-valid-test',
            refreshToken: 'dynamic-refresh',
            expiresAt: Date.now() + 60 * 60 * 1000,
          },
        }),
      );
      const { restore, calls } = collectIntervals();
      try {
        proxyPort = await startProxy({});
        const proactive = calls.filter(
          (c) => c.delay === PROACTIVE_INTERVAL_MS,
        );
        expect(proactive).toHaveLength(1);
        expect(proactive[0].unrefed).toBe(true);
      } finally {
        restore();
      }
    });

    it('does NOT start the timer in API-key mode', async () => {
      const { restore, calls } = collectIntervals();
      try {
        proxyPort = await startProxy({ ANTHROPIC_API_KEY: 'sk-ant-real-key' });
        expect(
          calls.filter((c) => c.delay === PROACTIVE_INTERVAL_MS),
        ).toHaveLength(0);
      } finally {
        restore();
      }
    });

    it('does NOT start the timer for a static env OAuth token (not refreshable)', async () => {
      const { restore, calls } = collectIntervals();
      try {
        proxyPort = await startProxy({ CLAUDE_CODE_OAUTH_TOKEN: 'env-token' });
        expect(
          calls.filter((c) => c.delay === PROACTIVE_INTERVAL_MS),
        ).toHaveLength(0);
      } finally {
        restore();
      }
    });

    it('clears the timer when the proxy server closes', async () => {
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'dynamic-creds-token-valid-test',
            refreshToken: 'dynamic-refresh',
            expiresAt: Date.now() + 60 * 60 * 1000,
          },
        }),
      );
      const { restore, calls } = collectIntervals();
      const clearSpy = vi.spyOn(global, 'clearInterval');
      try {
        const server = await startCredentialProxy(0);
        proxyServer = server;

        const proactive = calls.find((c) => c.delay === PROACTIVE_INTERVAL_MS);
        expect(proactive).toBeDefined();

        await new Promise<void>((resolve) => server.close(() => resolve()));
        proxyServer = undefined;

        // The proactive timer handle specifically must be cleared on close.
        const clearedHandles = clearSpy.mock.calls.map((c) => c[0]);
        expect(clearedHandles).toContain(proactive!.handle);
      } finally {
        clearSpy.mockRestore();
        restore();
      }
    });

    // Regression for LIA-363: the EADDRINUSE retry loop calls server.close()
    // before re-listening. If the 'close' cleanup listener is registered before
    // the first bind attempt, that retry-close clears proactiveRefreshTimer, so
    // the proxy binds on a later attempt with proactive refresh permanently dead.
    it('does NOT clear the proactive timer when a port conflict forces a retry', async () => {
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'dynamic-creds-token-valid-test',
            refreshToken: 'dynamic-refresh',
            expiresAt: Date.now() + 60 * 60 * 1000,
          },
        }),
      );

      // Occupy an ephemeral port so the proxy's first bind attempt hits
      // EADDRINUSE and enters the retry path.
      const blocker = http.createServer();
      const busyPort = await new Promise<number>((resolve) => {
        blocker.listen(0, '127.0.0.1', () =>
          resolve((blocker.address() as AddressInfo).port),
        );
      });

      const { restore, calls } = collectIntervals();
      const clearSpy = vi.spyOn(global, 'clearInterval');
      try {
        // Start the proxy on the occupied port (do not await — it will not
        // resolve until we free the port and its retry binds).
        const proxyPromise = startCredentialProxy(busyPort);

        // Free the port shortly after the first (failing) bind attempt so the
        // 2s retry succeeds.
        await new Promise<void>((resolve) => setTimeout(resolve, 200));
        await new Promise<void>((resolve) => blocker.close(() => resolve()));

        const server = await proxyPromise;
        proxyServer = server;

        const proactive = calls.find((c) => c.delay === PROACTIVE_INTERVAL_MS);
        expect(proactive).toBeDefined();

        // The retry's server.close() must NOT have cleared the proactive timer.
        const clearedDuringRetry = clearSpy.mock.calls.map((c) => c[0]);
        expect(clearedDuringRetry).not.toContain(proactive!.handle);

        // And a real shutdown still clears it.
        await new Promise<void>((resolve) => server.close(() => resolve()));
        proxyServer = undefined;
        const clearedAfterClose = clearSpy.mock.calls.map((c) => c[0]);
        expect(clearedAfterClose).toContain(proactive!.handle);
      } finally {
        clearSpy.mockRestore();
        restore();
        if (blocker.listening) {
          await new Promise<void>((resolve) => blocker.close(() => resolve()));
        }
      }
    }, 10000);
  });

  describe('request bounds (LIA-236)', () => {
    afterEach(() => {
      delete process.env.DEUS_PROXY_MAX_BODY_BYTES;
      delete process.env.DEUS_PROXY_UPSTREAM_TIMEOUT_MS;
    });

    it('rejects a request body over the cap with 413 and never hits upstream', async () => {
      process.env.DEUS_PROXY_MAX_BODY_BYTES = '50';
      proxyPort = await startProxy({ ANTHROPIC_API_KEY: 'sk-ant-real-key' });

      const res = await makeRequest(
        proxyPort,
        withProxyToken({
          method: 'POST',
          path: '/v1/messages',
          headers: { 'content-type': 'application/json' },
        }),
        'x'.repeat(200), // 200 bytes > 50-byte cap
      );

      expect(res.statusCode).toBe(413);
      // Upstream was never reached — lastUpstreamHeaders stays the beforeEach {}.
      expect(Object.keys(lastUpstreamHeaders)).toHaveLength(0);
    });

    it('allows a request body under the cap', async () => {
      process.env.DEUS_PROXY_MAX_BODY_BYTES = '1000';
      proxyPort = await startProxy({ ANTHROPIC_API_KEY: 'sk-ant-real-key' });

      const res = await makeRequest(
        proxyPort,
        withProxyToken({
          method: 'POST',
          path: '/v1/messages',
          headers: { 'content-type': 'application/json' },
        }),
        '{}',
      );

      expect(res.statusCode).toBe(200);
      expect(lastUpstreamHeaders['x-api-key']).toBe('sk-ant-real-key');
    });

    it('returns 502 when the upstream black-holes past the inactivity timeout', async () => {
      // Hanging upstream: accepts the request but never responds.
      const hanging = http.createServer(() => {});
      await new Promise<void>((r) => hanging.listen(0, '127.0.0.1', () => r()));
      const hangingPort = (hanging.address() as AddressInfo).port;
      try {
        process.env.DEUS_PROXY_UPSTREAM_TIMEOUT_MS = '200';
        proxyPort = await startProxy({
          ANTHROPIC_API_KEY: 'sk-ant-real-key',
          ANTHROPIC_BASE_URL: `http://127.0.0.1:${hangingPort}`,
        });

        const res = await makeRequest(
          proxyPort,
          withProxyToken({
            method: 'POST',
            path: '/v1/messages',
            headers: { 'content-type': 'application/json' },
          }),
          '{}',
        );
        expect(res.statusCode).toBe(502);
      } finally {
        await new Promise<void>((r) => hanging.close(() => r()));
      }
    });

    it('destroys the upstream socket when the client aborts mid-response', async () => {
      let signalClosed: () => void;
      const upstreamClosed = new Promise<void>((r) => {
        signalClosed = r;
      });
      // Hanging upstream that signals when its incoming request closes.
      const hanging = http.createServer((upReq) => {
        upReq.on('close', () => signalClosed());
      });
      await new Promise<void>((r) => hanging.listen(0, '127.0.0.1', () => r()));
      const hangingPort = (hanging.address() as AddressInfo).port;
      try {
        proxyPort = await startProxy({
          ANTHROPIC_API_KEY: 'sk-ant-real-key',
          ANTHROPIC_BASE_URL: `http://127.0.0.1:${hangingPort}`,
        });

        const req = http.request({
          hostname: '127.0.0.1',
          port: proxyPort,
          method: 'POST',
          path: '/v1/messages',
          headers: withProxyToken({
            headers: { 'content-type': 'application/json' },
          }).headers,
        });
        req.on('error', () => {}); // aborting emits ECONNRESET client-side
        req.write('{}');
        req.end();

        // Let the request reach the (hanging) upstream, then abort the client.
        await new Promise((r) => setTimeout(r, 50));
        req.destroy();

        // The proxy must propagate the abort by destroying the upstream.
        await Promise.race([
          upstreamClosed,
          new Promise((_, rej) =>
            setTimeout(() => rej(new Error('upstream not closed')), 2000),
          ),
        ]);
      } finally {
        await new Promise<void>((r) => hanging.close(() => r()));
      }
    });
  });
});
