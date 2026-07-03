import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import http from 'http';
import type { AddressInfo } from 'net';

/* ── Mocks (must precede imports from the module under test) ───────── */

vi.mock('./config.js', () => ({
  DEUS_PROXY_AUTH_ENABLED: true,
}));

vi.mock('./group-tokens.js', () => ({
  validateGroupToken: (token: string) =>
    token === 'test-proxy-token-abc123'
      ? 'test-group'
      : token === 'test-proxy-token-group-b'
        ? 'test-group-b'
        : null,
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
        '--max-context-chars',
        '8192',
        '--exclude-paths',
        'CLAUDE.md,INFRA.md',
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

  it('returns 429 for a group after RATE_LIMIT_MAX (20) requests/min (LIA-244)', async () => {
    for (let i = 0; i < 20; i++) {
      const res = await memoryRequest(
        proxyPort,
        JSON.stringify({ query: `query ${i}` }),
      );
      expect(res.statusCode).toBe(200);
    }
    const blocked = await memoryRequest(
      proxyPort,
      JSON.stringify({ query: 'one too many' }),
    );
    expect(blocked.statusCode).toBe(429);
  });

  it('keys the limit per authenticated group, not globally (LIA-244)', async () => {
    // Exhaust group A's bucket. Under the old x-deus-source keying this was one
    // global bucket, so group B would have been wrongly blocked too.
    for (let i = 0; i < 20; i++) {
      await memoryRequest(proxyPort, JSON.stringify({ query: `a ${i}` }));
    }
    expect(
      (await memoryRequest(proxyPort, JSON.stringify({ query: 'a blocked' })))
        .statusCode,
    ).toBe(429);

    // Group B (different proxy token → different group) has its own bucket.
    const bFirst = await memoryRequest(
      proxyPort,
      JSON.stringify({ query: 'b first' }),
      { 'x-deus-proxy-token': 'test-proxy-token-group-b' },
    );
    expect(bFirst.statusCode).toBe(200);
  });

  it('ignores the spoofable x-deus-source header for keying (LIA-244)', async () => {
    // The old code keyed on x-deus-source, so rotating it dodged the limit.
    // Now the same group shares one bucket regardless of the header.
    for (let i = 0; i < 20; i++) {
      const res = await memoryRequest(
        proxyPort,
        JSON.stringify({ query: `q ${i}` }),
        { 'x-deus-source': `rotating-source-${i}` },
      );
      expect(res.statusCode).toBe(200);
    }
    const blocked = await memoryRequest(
      proxyPort,
      JSON.stringify({ query: 'still blocked' }),
      { 'x-deus-source': 'a-fresh-source' },
    );
    expect(blocked.statusCode).toBe(429);
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

  /* ── LIA-354: server-side recall cap + index-file blocklist ────────── */

  function lastExecFileArgs(): string[] {
    return mockExecFile.mock.lastCall?.[1] as string[];
  }

  it('respects env overrides for the cap and blocklist (LIA-354)', async () => {
    vi.stubEnv('DEUS_BRIDGE_RECALL_MAX_CHARS', '4096');
    vi.stubEnv('DEUS_BRIDGE_RECALL_EXCLUDE', 'STUDY.md');
    try {
      await memoryRequest(proxyPort, JSON.stringify({ query: 'test' }));
    } finally {
      vi.unstubAllEnvs();
    }

    expect(lastExecFileArgs()).toEqual(
      expect.arrayContaining([
        '--max-context-chars',
        '4096',
        '--exclude-paths',
        'STUDY.md',
      ]),
    );
  });

  it('invalid cap env falls back to the 8192 default (LIA-354)', async () => {
    vi.stubEnv('DEUS_BRIDGE_RECALL_MAX_CHARS', 'abc');
    try {
      await memoryRequest(proxyPort, JSON.stringify({ query: 'test' }));
    } finally {
      vi.unstubAllEnvs();
    }

    expect(lastExecFileArgs()).toEqual(
      expect.arrayContaining(['--max-context-chars', '8192']),
    );
  });

  it('empty blocklist env omits --exclude-paths but keeps the cap (LIA-354)', async () => {
    vi.stubEnv('DEUS_BRIDGE_RECALL_EXCLUDE', '');
    try {
      await memoryRequest(proxyPort, JSON.stringify({ query: 'test' }));
    } finally {
      vi.unstubAllEnvs();
    }

    const args = lastExecFileArgs();
    expect(args).toEqual(
      expect.arrayContaining(['--max-context-chars', '8192']),
    );
    expect(args).not.toContain('--exclude-paths');
  });

  it('client body cannot lift the cap or blocklist (LIA-354)', async () => {
    await memoryRequest(
      proxyPort,
      JSON.stringify({
        query: 'test',
        max_context_chars: 999999,
        maxContextChars: 999999,
        exclude_paths: '',
      }),
    );

    // Server-side values, regardless of what the container sent.
    expect(lastExecFileArgs()).toEqual(
      expect.arrayContaining([
        '--max-context-chars',
        '8192',
        '--exclude-paths',
        'CLAUDE.md,INFRA.md',
      ]),
    );
  });
});
