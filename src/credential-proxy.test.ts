import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import http from 'http';
import type { AddressInfo } from 'net';

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
  });

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
      {
        method: 'POST',
        path: '/v1/messages',
        headers: {
          'content-type': 'application/json',
          'x-api-key': 'placeholder',
        },
      },
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
      {
        method: 'POST',
        path: '/openai/v1/responses',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
          'x-api-key': 'temp-key',
        },
      },
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
      {
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      },
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
      {
        method: 'POST',
        path: '/v1/messages',
        headers: {
          'content-type': 'application/json',
          'x-api-key': 'temp-key-from-exchange',
        },
      },
      '{}',
    );

    expect(lastUpstreamHeaders['x-api-key']).toBe('temp-key-from-exchange');
    expect(lastUpstreamHeaders['authorization']).toBeUndefined();
  });

  it('strips hop-by-hop headers', async () => {
    proxyPort = await startProxy({ ANTHROPIC_API_KEY: 'sk-ant-real-key' });

    await makeRequest(
      proxyPort,
      {
        method: 'POST',
        path: '/v1/messages',
        headers: {
          'content-type': 'application/json',
          connection: 'keep-alive',
          'keep-alive': 'timeout=5',
          'transfer-encoding': 'chunked',
        },
      },
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
      {
        method: 'POST',
        path: '/v1/messages',
        headers: { 'content-type': 'application/json' },
      },
      '{}',
    );

    expect(res.statusCode).toBe(502);
    expect(res.body).toBe('Bad Gateway');
  });

  it('OAuth mode reads token from credentials file when env token absent', async () => {
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'creds-file-token',
          expiresAt: Date.now() + 60 * 60 * 1000,
        },
      }),
    );

    proxyPort = await startProxy({});

    await makeRequest(
      proxyPort,
      {
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      },
      '{}',
    );

    expect(lastUpstreamHeaders['authorization']).toBe(
      'Bearer creds-file-token',
    );
  });

  it('OAuth mode: env CLAUDE_CODE_OAUTH_TOKEN takes priority over credentials file', async () => {
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'creds-file-token',
          expiresAt: Date.now() + 60 * 60 * 1000,
        },
      }),
    );

    proxyPort = await startProxy({ CLAUDE_CODE_OAUTH_TOKEN: 'env-token' });

    await makeRequest(
      proxyPort,
      {
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      },
      '{}',
    );

    expect(lastUpstreamHeaders['authorization']).toBe('Bearer env-token');
  });

  it('OAuth mode: re-reads credentials file when cached token is about to expire', async () => {
    // First call: token expiring in 2 min (within 5-min early-expire window)
    mockReadFileSync.mockReturnValueOnce(
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'expiring-token',
          expiresAt: Date.now() + 2 * 60 * 1000,
        },
      }),
    );
    // Second call: refreshed token
    mockReadFileSync.mockReturnValueOnce(
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'refreshed-token',
          expiresAt: Date.now() + 60 * 60 * 1000,
        },
      }),
    );

    proxyPort = await startProxy({});

    await makeRequest(
      proxyPort,
      {
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      },
      '{}',
    );
    expect(lastUpstreamHeaders['authorization']).toBe('Bearer expiring-token');

    // Cache is stale (token about to expire) — re-read on next request
    await makeRequest(
      proxyPort,
      {
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      },
      '{}',
    );
    expect(lastUpstreamHeaders['authorization']).toBe('Bearer refreshed-token');
  });

  it('OAuth mode replaces Bearer token on session endpoints', async () => {
    proxyPort = await startProxy({
      CLAUDE_CODE_OAUTH_TOKEN: 'real-oauth-token',
    });

    await makeRequest(
      proxyPort,
      {
        method: 'POST',
        path: '/v1/sessions',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
          'anthropic-beta': 'ccr-byoc-2025-07-29',
        },
      },
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
      {
        method: 'POST',
        path: '/api/oauth/claude_cli/create_api_key',
        headers: {
          'content-type': 'application/json',
          authorization: 'Bearer placeholder',
        },
      },
      '{}',
    );

    expect(res.statusCode).toBe(200);
    expect(lastUpstreamHeaders['authorization']).toBeUndefined();
  });
});
