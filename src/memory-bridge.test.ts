import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import http from 'http';
import type { AddressInfo } from 'net';

/* ── Mocks (must precede imports from the module under test) ───────── */

vi.mock('./config.js', () => ({
  DEUS_PROXY_AUTH_ENABLED: true,
}));

vi.mock('./group-tokens.js', () => ({
  validateGroupToken: (token: string) =>
    token === 'test-proxy-token-abc123' ? 'test-group' : null,
}));

vi.mock('./env.js', () => ({
  readEnvFile: vi.fn(() => ({})),
}));

vi.mock('./logger.js', () => ({
  logger: { info: vi.fn(), error: vi.fn(), debug: vi.fn(), warn: vi.fn() },
}));

vi.mock('fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('fs')>();
  return { ...actual, readFileSync: vi.fn(actual.readFileSync) };
});

vi.mock('child_process', async (importOriginal) => {
  const actual = await importOriginal<typeof import('child_process')>();
  return {
    ...actual,
    execFile: vi.fn(),
    execFileSync: vi.fn(),
    execSync: vi.fn(),
  };
});

import { readFileSync } from 'fs';
import { execFile, execFileSync } from 'child_process';
import {
  startCredentialProxy,
  _resetCredentialsCacheForTest,
  _resetRateLimiterForTest,
} from './credential-proxy.js';
import { AuthProviderRegistry } from './auth-providers/types.js';

const TEST_TOKEN = 'test-proxy-token-abc123';
const mockExecFile = vi.mocked(execFile);
const mockExecFileSync = vi.mocked(execFileSync);
const mockReadFileSync = readFileSync as ReturnType<typeof vi.fn>;

/* ── Helpers ───────────────────────────────────────────────────────── */

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

function memoryRequest(
  port: number,
  body: string,
  headers: Record<string, string> = {},
) {
  return makeRequest(
    port,
    {
      method: 'POST',
      path: '/memory/query',
      headers: {
        'x-deus-proxy-token': TEST_TOKEN,
        'content-type': 'application/json',
        ...headers,
      },
    },
    body,
  );
}

async function closeServer(server: http.Server | undefined): Promise<void> {
  if (!server) return;
  await new Promise<void>((resolve) => server.close(() => resolve()));
}

/* ── Test suite ────────────────────────────────────────────────────── */

describe('memory bridge — POST /memory/query', () => {
  let proxyServer: http.Server | undefined;
  let proxyPort: number;

  beforeEach(async () => {
    AuthProviderRegistry.reset();
    _resetCredentialsCacheForTest();
    _resetRateLimiterForTest();
    mockReadFileSync.mockImplementation(() => {
      throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
    });
    // Block keychain lookups to prevent real credentials in tests
    mockExecFileSync.mockImplementation(() => {
      throw new Error('no keychain (test isolation)');
    });
    // Default: execFile succeeds with valid JSON
    mockExecFile.mockImplementation(
      (_file: unknown, _args: unknown, _opts: unknown, cb: unknown) => {
        const callback = cb as (
          err: Error | null,
          stdout: string,
          stderr: string,
        ) => void;
        callback(
          null,
          JSON.stringify({
            context: 'test memory context',
            paths: ['Atoms/test.md'],
            confidence: 0.85,
            fell_back: false,
          }),
          '',
        );
        return {} as ReturnType<typeof execFile>;
      },
    );

    proxyServer = await startCredentialProxy(0, '127.0.0.1');
    proxyPort = (proxyServer.address() as AddressInfo).port;
  });

  afterEach(async () => {
    await closeServer(proxyServer);
    _resetCredentialsCacheForTest();
    _resetRateLimiterForTest();
    AuthProviderRegistry.reset();
    vi.restoreAllMocks();
  });

  it('returns 200 with JSON on valid query', async () => {
    const res = await memoryRequest(
      proxyPort,
      JSON.stringify({ query: 'what is my timezone?' }),
    );

    expect(res.statusCode).toBe(200);
    expect(res.headers['content-type']).toBe('application/json');
    const json = JSON.parse(res.body);
    expect(json.confidence).toBe(0.85);
    expect(json.paths).toContain('Atoms/test.md');

    // Verify execFile was called with expected args
    expect(mockExecFile).toHaveBeenCalledWith(
      expect.any(String), // python binary
      expect.arrayContaining([
        expect.stringContaining('memory_query.py'),
        'what is my timezone?',
        '--json',
        '--source',
        'bridge',
        '-k',
        '3',
      ]),
      expect.objectContaining({ timeout: 4_000 }),
      expect.any(Function),
    );
  });

  it('returns 401 without auth token', async () => {
    const res = await makeRequest(
      proxyPort,
      {
        method: 'POST',
        path: '/memory/query',
        headers: { 'content-type': 'application/json' },
      },
      JSON.stringify({ query: 'test' }),
    );

    expect(res.statusCode).toBe(401);
  });

  it('returns 400 on invalid JSON body', async () => {
    const res = await memoryRequest(proxyPort, 'not json at all');
    expect(res.statusCode).toBe(400);
    const json = JSON.parse(res.body);
    expect(json.error).toMatch(/Invalid JSON/i);
  });

  it('returns 400 when query field is missing', async () => {
    const res = await memoryRequest(proxyPort, JSON.stringify({ k: 5 }));
    expect(res.statusCode).toBe(400);
    const json = JSON.parse(res.body);
    expect(json.error).toMatch(/query/i);
  });

  it('returns 400 when query is empty string', async () => {
    const res = await memoryRequest(proxyPort, JSON.stringify({ query: '' }));
    expect(res.statusCode).toBe(400);
  });

  it('returns 500 on execFile spawn failure', async () => {
    mockExecFile.mockImplementation(
      (_file: unknown, _args: unknown, _opts: unknown, cb: unknown) => {
        const callback = cb as (
          err: Error | null,
          stdout: string,
          stderr: string,
        ) => void;
        callback(
          Object.assign(new Error('spawn ENOENT'), {
            code: 'ENOENT',
          }) as Error,
          '',
          '',
        );
        return {} as ReturnType<typeof execFile>;
      },
    );

    const res = await memoryRequest(
      proxyPort,
      JSON.stringify({ query: 'test' }),
    );
    expect(res.statusCode).toBe(500);
    const json = JSON.parse(res.body);
    expect(json.error).toMatch(/failed/i);
  });

  it('returns 504 on timeout (killed process)', async () => {
    mockExecFile.mockImplementation(
      (_file: unknown, _args: unknown, _opts: unknown, cb: unknown) => {
        const callback = cb as (
          err: Error | null,
          stdout: string,
          stderr: string,
        ) => void;
        callback(
          Object.assign(new Error('process timed out'), {
            killed: true,
            signal: 'SIGTERM',
          }) as Error,
          '',
          '',
        );
        return {} as ReturnType<typeof execFile>;
      },
    );

    const res = await memoryRequest(
      proxyPort,
      JSON.stringify({ query: 'test' }),
    );
    expect(res.statusCode).toBe(504);
    const json = JSON.parse(res.body);
    expect(json.error).toMatch(/timed out/i);
  });

  it('returns 429 when rate limit is exceeded (6 rapid requests)', async () => {
    const results: number[] = [];

    for (let i = 0; i < 6; i++) {
      const res = await memoryRequest(
        proxyPort,
        JSON.stringify({ query: `query ${i}` }),
        { 'x-deus-source': 'rate-limit-test' },
      );
      results.push(res.statusCode);
    }

    // First 5 should succeed, 6th should be rate limited
    expect(results.slice(0, 5)).toEqual([200, 200, 200, 200, 200]);
    expect(results[5]).toBe(429);
  });

  it('passes custom k and source to the script', async () => {
    await memoryRequest(
      proxyPort,
      JSON.stringify({ query: 'test', k: 10, source: 'telegram' }),
    );

    expect(mockExecFile).toHaveBeenLastCalledWith(
      expect.any(String),
      expect.arrayContaining(['--source', 'telegram', '-k', '10']),
      expect.objectContaining({ timeout: 4_000 }),
      expect.any(Function),
    );
  });
});
