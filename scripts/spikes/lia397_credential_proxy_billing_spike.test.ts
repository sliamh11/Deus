import { EventEmitter } from 'node:events';
import { readFileSync, rmSync, writeFileSync, mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { PassThrough } from 'node:stream';
import type { ChildProcess } from 'node:child_process';

import { ChatAnthropic } from '@langchain/anthropic';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const dependencyMocks = vi.hoisted(() => ({
  readEnvFile: vi.fn(),
  readCredentialsFile: vi.fn(),
}));

vi.mock('../../src/env.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/env.js')>();
  return { ...actual, readEnvFile: dependencyMocks.readEnvFile };
});

vi.mock('../../src/auth-providers/anthropic.js', async (importOriginal) => {
  const actual =
    await importOriginal<
      typeof import('../../src/auth-providers/anthropic.js')
    >();
  return {
    ...actual,
    readCredentialsFile: dependencyMocks.readCredentialsFile,
  };
});

import {
  EARLY_EXPIRE_WINDOW_MS,
  buildProxyRoutedChatAnthropic,
  checkCredentialFreshness,
  createHeaderCapturingFetch,
  main,
  resolveConfiguredApiKey,
  runApiKeyFallbackSmokeTest,
  waitForChildReady,
  type ChildReadiness,
  type MainDependencies,
} from './lia397_credential_proxy_billing_spike.js';

const originalApiKey = process.env.ANTHROPIC_API_KEY;
const originalSpikePort = process.env.SPIKE_PROXY_PORT;
let fixtureDirectory: string | undefined;
let fixturePath: string | undefined;

beforeEach(() => {
  dependencyMocks.readEnvFile.mockReset().mockReturnValue({});
  dependencyMocks.readCredentialsFile.mockReset();
  delete process.env.ANTHROPIC_API_KEY;
  delete process.env.SPIKE_PROXY_PORT;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  if (originalApiKey === undefined) delete process.env.ANTHROPIC_API_KEY;
  else process.env.ANTHROPIC_API_KEY = originalApiKey;
  if (originalSpikePort === undefined) delete process.env.SPIKE_PROXY_PORT;
  else process.env.SPIKE_PROXY_PORT = originalSpikePort;
  if (fixtureDirectory)
    rmSync(fixtureDirectory, { recursive: true, force: true });
  fixtureDirectory = undefined;
  fixturePath = undefined;
});

function writeCredentialsFixture(expiresAt?: number): void {
  fixtureDirectory ??= mkdtempSync(path.join(tmpdir(), 'lia397-'));
  fixturePath = path.join(fixtureDirectory, '.credentials.json');
  const claudeAiOauth: { accessToken: string; expiresAt?: number } = {
    accessToken: 'fixture-access-token-long-enough',
  };
  if (expiresAt !== undefined) claudeAiOauth.expiresAt = expiresAt;
  writeFileSync(fixturePath, JSON.stringify({ claudeAiOauth }), 'utf-8');
}

function readCredentialsFixture():
  { accessToken: string; expiresAt: number } | undefined {
  if (!fixturePath) return undefined;
  try {
    const parsed = JSON.parse(readFileSync(fixturePath, 'utf-8')) as {
      claudeAiOauth?: { accessToken?: string; expiresAt?: number };
    };
    const oauth = parsed.claudeAiOauth;
    if (!oauth?.accessToken) return undefined;
    return {
      accessToken: oauth.accessToken,
      expiresAt: oauth.expiresAt ?? Infinity,
    };
  } catch {
    return undefined;
  }
}

function makeFakeChild(): {
  child: ChildProcess;
  stdout: PassThrough;
  kill: ReturnType<typeof vi.fn>;
} {
  const stdout = new PassThrough();
  const kill = vi.fn();
  const child = Object.assign(new EventEmitter(), { stdout, kill });
  return { child: child as unknown as ChildProcess, stdout, kill };
}

function readTranscript(
  log: ReturnType<typeof vi.spyOn>,
): Record<string, unknown> {
  expect(log).toHaveBeenCalledTimes(1);
  return JSON.parse(String(log.mock.calls[0]?.[0])) as Record<string, unknown>;
}

function fakeMainDependencies(
  child: ChildProcess,
  readiness: Promise<ChildReadiness>,
  smokeResult: {
    succeeded: boolean;
    responseText?: string;
    capturedHeaders?: unknown[];
    error?: string;
  } = { succeeded: true, responseText: 'ok' },
): {
  deps: MainDependencies;
  runProxyRoutedSmokeTest: ReturnType<typeof vi.fn>;
  runApiKeyFallbackSmokeTest: ReturnType<typeof vi.fn>;
} {
  const runProxyRoutedSmokeTest = vi.fn().mockResolvedValue(smokeResult);
  const runApiKeyFallbackSmokeTest = vi
    .fn()
    .mockResolvedValue({ skipped: true, reason: 'no key' });
  return {
    deps: {
      spawnProxyChild: vi.fn().mockReturnValue(child),
      waitForChildReady: vi.fn().mockReturnValue(readiness),
      runProxyRoutedSmokeTest,
      runApiKeyFallbackSmokeTest,
    } as unknown as MainDependencies,
    runProxyRoutedSmokeTest,
    runApiKeyFallbackSmokeTest,
  };
}

describe('buildProxyRoutedChatAnthropic', () => {
  it('pins the proxy client to bearer auth with apiKey explicitly null', () => {
    const baseURL = 'http://127.0.0.1:3099';
    const model = buildProxyRoutedChatAnthropic(baseURL, vi.fn());
    const client = model.createClient({});

    expect(model).toBeInstanceOf(ChatAnthropic);
    expect(model.model).toBe('claude-opus-4-8');
    expect(client.baseURL).toBe(baseURL);
    expect(client.authToken).toBe('placeholder');
    expect(client.apiKey).toBeNull();
  });

  it('sets the oauth-2025-04-20 anthropic-beta header explicitly', () => {
    // Plain `authToken` never populates the SDK's own OAuth credential state,
    // so it never auto-appends this header (verified against
    // node_modules/@anthropic-ai/sdk/client.js's prepareRequest) — the spike
    // must set it itself via defaultHeaders. No public getter exists for
    // defaultHeaders, so this reaches into the SDK's internal _options field.
    const model = buildProxyRoutedChatAnthropic('http://127.0.0.1:3099');
    const client = model.createClient({}) as unknown as {
      _options: { defaultHeaders?: Record<string, string> };
    };

    expect(client._options.defaultHeaders).toMatchObject({
      'anthropic-beta': 'oauth-2025-04-20',
    });
  });

  it('honors an explicit client baseURL supplied by LangChain', () => {
    const model = buildProxyRoutedChatAnthropic('http://127.0.0.1:3099');
    const client = model.createClient({ baseURL: 'http://127.0.0.1:3100' });

    expect(client.baseURL).toBe('http://127.0.0.1:3100');
    expect(client.apiKey).toBeNull();
  });
});

describe('createHeaderCapturingFetch', () => {
  it('records only safe header metadata and delegates Request and init calls', async () => {
    const realFetch = vi
      .fn()
      .mockImplementation(async () => new Response('delegated'));
    vi.stubGlobal('fetch', realFetch);
    const capture = createHeaderCapturingFetch();
    const rawToken = 'Bearer fixture-secret-value-never-captured';
    const request = new Request('http://127.0.0.1:3099/v1/messages', {
      headers: {
        authorization: rawToken,
        'anthropic-version': '2023-06-01',
      },
    });

    await capture.fetch(request);
    await capture.fetch('http://127.0.0.1:3099/v1/messages', {
      headers: { 'x-api-key': 'fixture-key-never-captured' },
    });

    expect(capture.captured).toEqual([
      {
        hasAuthorization: true,
        authorizationPrefix: 'Bearer ',
        hasXApiKey: false,
        anthropicVersion: '2023-06-01',
      },
      {
        hasAuthorization: false,
        authorizationPrefix: '',
        hasXApiKey: true,
        anthropicVersion: undefined,
      },
    ]);
    expect(JSON.stringify(capture.captured)).not.toContain(rawToken);
    expect(JSON.stringify(capture.captured)).not.toContain(
      'fixture-key-never-captured',
    );
    expect(realFetch).toHaveBeenNthCalledWith(1, request, undefined);
    expect(realFetch).toHaveBeenCalledTimes(2);
  });
});

describe('resolveConfiguredApiKey', () => {
  it('prefers the .env-file value', () => {
    dependencyMocks.readEnvFile.mockReturnValue({
      ANTHROPIC_API_KEY: 'env-file-fixture',
    });
    process.env.ANTHROPIC_API_KEY = 'process-env-fixture';

    expect(resolveConfiguredApiKey()).toBe('env-file-fixture');
    expect(dependencyMocks.readEnvFile).toHaveBeenCalledWith([
      'ANTHROPIC_API_KEY',
    ]);
  });

  it('falls back to process.env when the .env file has no key', () => {
    process.env.ANTHROPIC_API_KEY = 'process-env-fixture';

    expect(resolveConfiguredApiKey()).toBe('process-env-fixture');
  });
});

describe('runApiKeyFallbackSmokeTest', () => {
  it('skips without invoking when no API key is configured', async () => {
    const invoke = vi.fn();

    await expect(
      runApiKeyFallbackSmokeTest(invoke, () => undefined),
    ).resolves.toEqual({
      skipped: true,
      reason: 'no ANTHROPIC_API_KEY configured',
    });
    expect(invoke).not.toHaveBeenCalled();
  });

  it('constructs a plain API-key model and uses the injected invoke seam', async () => {
    const invoke = vi.fn().mockResolvedValue({
      succeeded: true,
      responseText: 'ok',
    });

    await expect(
      runApiKeyFallbackSmokeTest(invoke, () => 'configured-fixture-key'),
    ).resolves.toEqual({ succeeded: true, responseText: 'ok' });
    expect(invoke).toHaveBeenCalledTimes(1);
    const model = invoke.mock.calls[0]?.[0] as ChatAnthropic;
    expect(model).toBeInstanceOf(ChatAnthropic);
    expect(model.apiKey).toBe('configured-fixture-key');
  });
});

describe('checkCredentialFreshness', () => {
  const now = 2_000_000_000_000;

  beforeEach(() => {
    vi.spyOn(Date, 'now').mockReturnValue(now);
    dependencyMocks.readCredentialsFile.mockImplementation(
      readCredentialsFixture,
    );
  });

  it('rejects a credential inside the early-refresh window', () => {
    writeCredentialsFixture(now + EARLY_EXPIRE_WINDOW_MS - 1);

    expect(checkCredentialFreshness()).toMatchObject({ safe: false });
  });

  it('accepts a comfortably safe credential at the production boundary', () => {
    writeCredentialsFixture(now + EARLY_EXPIRE_WINDOW_MS);

    expect(checkCredentialFreshness()).toEqual({ safe: true });
  });

  it('rejects the Infinity sentinel represented by an omitted expiry', () => {
    writeCredentialsFixture();

    expect(checkCredentialFreshness()).toEqual({
      safe: false,
      reason: 'OAuth credential expiry is unknown (Infinity sentinel)',
    });
  });

  it('rejects a missing credential file', () => {
    expect(checkCredentialFreshness()).toMatchObject({
      safe: false,
      reason: expect.stringContaining('no readable OAuth credentials found'),
    });
  });
});

describe('waitForChildReady', () => {
  it('resolves started after all three readiness lines across chunks', async () => {
    const { child, stdout } = makeFakeChild();
    const readiness = waitForChildReady(child, 100);

    stdout.write('LISTEN');
    stdout.write('ING:3099\nAUTH_MODE:oauth\n');
    stdout.write('REFRESHABLE:true\n');

    await expect(readiness).resolves.toEqual({
      outcome: 'started',
      authMode: 'oauth',
      usesRefreshableOAuth: true,
    });
  });

  it('resolves an UNSAFE line as an orderly abort', async () => {
    const { child, stdout } = makeFakeChild();
    const readiness = waitForChildReady(child, 100);

    stdout.write('UNSAFE:credential expires too soon\n');

    await expect(readiness).resolves.toEqual({
      outcome: 'aborted',
      reason: 'credential expires too soon',
    });
  });

  it('rejects when readiness times out', async () => {
    const { child } = makeFakeChild();

    await expect(waitForChildReady(child, 10)).rejects.toThrow(
      'timed out after 10ms',
    );
  });

  it('rejects when the child exits before readiness', async () => {
    const { child } = makeFakeChild();
    const readiness = waitForChildReady(child, 100);

    child.emit('exit', 1, null);

    await expect(readiness).rejects.toThrow('exited before readiness');
  });

  it('rejects when the child emits an error before readiness', async () => {
    const { child } = makeFakeChild();
    const readiness = waitForChildReady(child, 100);

    child.emit('error', new Error('spawn failed'));

    await expect(readiness).rejects.toThrow(
      'failed before readiness: spawn failed',
    );
  });
});

describe('main', () => {
  it('normalizes startup failure and always kills the child', async () => {
    const { child, kill } = makeFakeChild();
    const { deps, runProxyRoutedSmokeTest, runApiKeyFallbackSmokeTest } =
      fakeMainDependencies(child, Promise.reject(new Error('startup broke')));
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);

    await main(deps);

    expect(readTranscript(log)).toMatchObject({
      readiness: {
        succeeded: false,
        phase: 'startup',
        error: 'startup broke',
      },
      criterion2: {
        succeeded: false,
        phase: 'startup',
        error: 'startup broke',
      },
      criterion2Pass: false,
    });
    expect(runProxyRoutedSmokeTest).not.toHaveBeenCalled();
    expect(runApiKeyFallbackSmokeTest).toHaveBeenCalledTimes(1);
    expect(kill).toHaveBeenCalledTimes(1);
  });

  it('uses the child reason and skips the proxy smoke test after an abort', async () => {
    const { child, kill } = makeFakeChild();
    const { deps, runProxyRoutedSmokeTest } = fakeMainDependencies(
      child,
      Promise.resolve({ outcome: 'aborted', reason: 'shared file unsafe' }),
    );
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);

    await main(deps);

    expect(readTranscript(log)).toMatchObject({
      readiness: { outcome: 'aborted', reason: 'shared file unsafe' },
      criterion2: {
        succeeded: false,
        phase: 'precondition',
        skipped: true,
        reason: 'shared file unsafe',
      },
      criterion2Pass: false,
    });
    expect(runProxyRoutedSmokeTest).not.toHaveBeenCalled();
    expect(kill).toHaveBeenCalledTimes(1);
  });

  it('skips the subscription smoke test when the proxy resolves api-key mode', async () => {
    const { child } = makeFakeChild();
    const { deps, runProxyRoutedSmokeTest } = fakeMainDependencies(
      child,
      Promise.resolve({
        outcome: 'started',
        authMode: 'api-key',
        usesRefreshableOAuth: false,
      }),
    );
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);

    await main(deps);

    expect(readTranscript(log)).toMatchObject({
      criterion2: {
        succeeded: false,
        phase: 'precondition',
        skipped: true,
        reason: expect.stringContaining('proxy resolved to api-key mode'),
      },
      criterion2Pass: false,
    });
    expect(runProxyRoutedSmokeTest).not.toHaveBeenCalled();
  });

  it('runs the OAuth smoke path and derives criterion2Pass from its result', async () => {
    const { child, kill } = makeFakeChild();
    const { deps, runProxyRoutedSmokeTest } = fakeMainDependencies(
      child,
      Promise.resolve({
        outcome: 'started',
        authMode: 'oauth',
        usesRefreshableOAuth: true,
      }),
      {
        succeeded: true,
        responseText: 'ok',
        capturedHeaders: [{ hasAuthorization: true }],
      },
    );
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined);

    await main(deps);

    expect(runProxyRoutedSmokeTest).toHaveBeenCalledWith(3099);
    expect(readTranscript(log)).toMatchObject({
      criterion2: {
        succeeded: true,
        responseText: 'ok',
        capturedHeaders: [{ hasAuthorization: true }],
      },
      criterion2Pass: true,
    });
    expect(kill).toHaveBeenCalledTimes(1);
  });
});
