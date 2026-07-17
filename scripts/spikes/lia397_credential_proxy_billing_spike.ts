import { spawn, type ChildProcess } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import Anthropic from '@anthropic-ai/sdk';
import { ChatAnthropic } from '@langchain/anthropic';

import {
  CREDENTIALS_PATH,
  EARLY_EXPIRE_WINDOW_MS,
  readCredentialsFile,
} from '../../src/auth-providers/anthropic.js';
import { readEnvFile } from '../../src/env.js';

export { EARLY_EXPIRE_WINDOW_MS };

export interface CredentialFreshness {
  safe: boolean;
  reason?: string;
}

export interface HeaderCapture {
  hasAuthorization: boolean;
  authorizationPrefix: string;
  hasXApiKey: boolean;
  anthropicVersion: string | undefined;
}

export interface InvokeResult {
  succeeded: boolean;
  responseText?: string;
  error?: string;
}

export type ApiKeyFallbackResult =
  | InvokeResult
  | {
      skipped: true;
      reason: string;
    };

export type Invoke = (model: ChatAnthropic) => Promise<InvokeResult>;

export type ChildReadiness =
  | {
      outcome: 'started';
      authMode: string;
      usesRefreshableOAuth: boolean;
    }
  | { outcome: 'aborted'; reason: string };

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Mirrors the production credential resolver's no-keychain fast path so the
 * isolated proxy child can decline startup without refreshing or rewriting the
 * credential file shared with the live Deus host.
 *
 * Deliberately conservative in both directions, reviewed and accepted across
 * plan review:
 * - Only checks the fast path, not the keychain fallback the live host also
 *   consults — a stale file with a fresh keychain token reports UNSAFE here
 *   even though the live host would actually succeed. False-negative, not
 *   false-positive: never a race, just an occasional unnecessary skip.
 * - This snapshot is taken once, before the child starts; the actual proxied
 *   request happens moments later and re-resolves credentials independently
 *   inside the child. A credential that crosses into the refresh window in
 *   that gap could still trigger a write there — accepted as a low-probability,
 *   residual risk: atomic-rename writes avoid partial-file corruption, but a
 *   rare last-write-wins clobber between the host and this child (if both
 *   refresh within the same short window) remains theoretically possible.
 */
export function checkCredentialFreshness(): CredentialFreshness {
  const now = Date.now();
  const file = readCredentialsFile();

  if (
    file &&
    file.expiresAt !== Infinity &&
    file.expiresAt >= now + EARLY_EXPIRE_WINDOW_MS
  ) {
    return { safe: true };
  }

  if (!file) {
    return {
      safe: false,
      reason: `no readable OAuth credentials found at ${CREDENTIALS_PATH}`,
    };
  }
  if (file.expiresAt === Infinity) {
    return {
      safe: false,
      reason: 'OAuth credential expiry is unknown (Infinity sentinel)',
    };
  }
  return {
    safe: false,
    reason: 'OAuth credentials are expired or within the early-refresh window',
  };
}

/**
 * Spawns the local tsx binary directly so killing the returned handle cannot
 * leave an npx-owned descendant proxy running.
 */
export function spawnProxyChild(port: number): ChildProcess {
  const spikeDirectory = path.dirname(fileURLToPath(import.meta.url));
  const childEntryPath = path.join(
    spikeDirectory,
    'lia397_proxy_child_entry.ts',
  );
  const tsxPath = path.resolve(process.cwd(), 'node_modules/.bin/tsx');

  // Throwaway spike scope is intentionally POSIX-only; production portability remains centralized in src/platform.ts.
  return spawn(tsxPath, [childEntryPath], {
    env: {
      ...process.env,
      // Safe only because: (1) src/config.ts hardcodes DEUS_PROXY_AUTH_ENABLED=true
      // whenever NODE_ENV==='production', so this override is ignored in prod builds;
      // (2) startCredentialProxy binds 127.0.0.1-only for this child's whole lifetime.
      DEUS_PROXY_AUTH: '0',
      NODE_ENV: 'development',
      SPIKE_PROXY_PORT: String(port),
    },
  });
}

export function waitForChildReady(
  child: ChildProcess,
  timeoutMs: number,
): Promise<ChildReadiness> {
  return new Promise((resolve, reject) => {
    if (!child.stdout) {
      reject(new Error('credential proxy child has no stdout pipe'));
      return;
    }

    let buffer = '';
    let listening = false;
    let authMode: string | undefined;
    let usesRefreshableOAuth: boolean | undefined;
    let settled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const cleanup = (): void => {
      if (timer) clearTimeout(timer);
      child.stdout?.off('data', onData);
      child.off('exit', onExit);
      child.off('error', onError);
    };

    const finish = (outcome: ChildReadiness): void => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(outcome);
    };

    const fail = (error: Error): void => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };

    const parseLine = (rawLine: string): void => {
      const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine;
      if (line.startsWith('UNSAFE:')) {
        finish({ outcome: 'aborted', reason: line.slice('UNSAFE:'.length) });
        return;
      }
      if (line.startsWith('LISTENING:')) {
        listening = true;
      } else if (line.startsWith('AUTH_MODE:')) {
        authMode = line.slice('AUTH_MODE:'.length);
      } else if (line.startsWith('REFRESHABLE:')) {
        const value = line.slice('REFRESHABLE:'.length);
        if (value === 'true' || value === 'false') {
          usesRefreshableOAuth = value === 'true';
        }
      }

      if (
        listening &&
        authMode !== undefined &&
        usesRefreshableOAuth !== undefined
      ) {
        finish({ outcome: 'started', authMode, usesRefreshableOAuth });
      }
    };

    const onData = (chunk: Buffer | string): void => {
      buffer += chunk.toString();
      const lines = buffer.split('\n');
      buffer = lines.pop() ?? '';
      for (const line of lines) parseLine(line);
    };

    const onExit = (
      code: number | null,
      signal: NodeJS.Signals | null,
    ): void => {
      fail(
        new Error(
          `credential proxy child exited before readiness (code=${String(code)}, signal=${String(signal)})`,
        ),
      );
    };

    const onError = (error: Error): void => {
      fail(
        new Error(
          `credential proxy child failed before readiness: ${error.message}`,
        ),
      );
    };

    child.stdout.on('data', onData);
    child.once('exit', onExit);
    child.once('error', onError);
    timer = setTimeout(() => {
      fail(
        new Error(
          `timed out after ${timeoutMs}ms waiting for credential proxy child readiness`,
        ),
      );
    }, timeoutMs);
  });
}

export function createHeaderCapturingFetch(): {
  fetch: typeof fetch;
  captured: HeaderCapture[];
} {
  const realFetch = globalThis.fetch;
  const captured: HeaderCapture[] = [];

  const capturingFetch: typeof fetch = async (input, init) => {
    const headers = new Request(input, init).headers;
    const authorization = headers.get('authorization');
    captured.push({
      hasAuthorization: authorization !== null,
      authorizationPrefix: authorization?.slice(0, 7) ?? '',
      hasXApiKey: headers.has('x-api-key'),
      anthropicVersion: headers.get('anthropic-version') ?? undefined,
    });
    return realFetch(input, init);
  };

  return { fetch: capturingFetch, captured };
}

export function buildProxyRoutedChatAnthropic(
  baseURL: string,
  fetchOverride?: typeof fetch,
): ChatAnthropic {
  return new ChatAnthropic({
    model: 'claude-opus-4-8',
    createClient: (options) =>
      new Anthropic({
        baseURL: options.baseURL ?? baseURL,
        authToken: 'placeholder',
        apiKey: null,
        // Plain `authToken` never populates the SDK's own OAuth credential
        // state (only its structured credentials/config/profile flow does),
        // so it never auto-appends this beta header — add it explicitly, or
        // the upstream OAuth-authenticated request can be rejected.
        defaultHeaders: { 'anthropic-beta': 'oauth-2025-04-20' },
        fetch: fetchOverride,
      }),
  });
}

export const defaultInvoke: Invoke = async (model) => {
  try {
    const response = await model.invoke([
      { role: 'user', content: 'Say "ok" and nothing else.' },
    ]);
    return {
      succeeded: true,
      responseText:
        typeof response.content === 'string'
          ? response.content
          : JSON.stringify(response.content),
    };
  } catch (error) {
    return { succeeded: false, error: errorMessage(error) };
  }
};

export async function runProxyRoutedSmokeTest(
  port: number,
  invoke: Invoke = defaultInvoke,
): Promise<{
  succeeded: boolean;
  responseText?: string;
  capturedHeaders?: unknown[];
  error?: string;
}> {
  const headerCapture = createHeaderCapturingFetch();
  const model = buildProxyRoutedChatAnthropic(
    `http://127.0.0.1:${port}`,
    headerCapture.fetch,
  );
  const result = await invoke(model);
  return { ...result, capturedHeaders: headerCapture.captured };
}

export function resolveConfiguredApiKey(): string | undefined {
  return (
    readEnvFile(['ANTHROPIC_API_KEY']).ANTHROPIC_API_KEY ||
    process.env.ANTHROPIC_API_KEY
  );
}

export async function runApiKeyFallbackSmokeTest(
  invoke: Invoke = defaultInvoke,
  resolveKey: () => string | undefined = resolveConfiguredApiKey,
): Promise<ApiKeyFallbackResult> {
  const apiKey = resolveKey();
  if (apiKey === undefined) {
    return { skipped: true, reason: 'no ANTHROPIC_API_KEY configured' };
  }

  const model = new ChatAnthropic({ apiKey });
  return invoke(model);
}

export interface MainDependencies {
  spawnProxyChild: typeof spawnProxyChild;
  waitForChildReady: typeof waitForChildReady;
  runProxyRoutedSmokeTest: typeof runProxyRoutedSmokeTest;
  runApiKeyFallbackSmokeTest: typeof runApiKeyFallbackSmokeTest;
}

type Criterion2Result =
  | Awaited<ReturnType<typeof runProxyRoutedSmokeTest>>
  | {
      succeeded: false;
      phase: 'startup';
      error: string;
    }
  | {
      succeeded: false;
      phase: 'precondition';
      skipped: true;
      reason: string;
    };

export async function main(
  deps: MainDependencies = {
    spawnProxyChild,
    waitForChildReady,
    runProxyRoutedSmokeTest,
    runApiKeyFallbackSmokeTest,
  },
): Promise<void> {
  const port = Number(process.env.SPIKE_PROXY_PORT ?? '3099');
  const child = deps.spawnProxyChild(port);

  try {
    let readiness: ChildReadiness | undefined;
    let startupResult:
      { succeeded: false; phase: 'startup'; error: string } | undefined;
    let criterion2Result: Criterion2Result;
    let criterion2Pass = false;

    try {
      readiness = await deps.waitForChildReady(child, 10_000);
    } catch (error) {
      startupResult = {
        succeeded: false,
        phase: 'startup',
        error: errorMessage(error),
      };
    }

    if (startupResult) {
      criterion2Result = startupResult;
    } else if (readiness?.outcome === 'aborted') {
      criterion2Result = {
        succeeded: false,
        phase: 'precondition',
        skipped: true,
        reason: readiness.reason,
      };
    } else if (readiness?.authMode !== 'oauth') {
      criterion2Result = {
        succeeded: false,
        phase: 'precondition',
        skipped: true,
        reason:
          'proxy resolved to api-key mode (ANTHROPIC_API_KEY present in .env) — cannot validate the subscription/OAuth billing path while an API key is configured; temporarily unset it or run on a machine without one configured for a clean AC2 signal',
      };
    } else {
      criterion2Result = await deps.runProxyRoutedSmokeTest(port);
      criterion2Pass = criterion2Result.succeeded;
    }

    const fallbackResult = await deps.runApiKeyFallbackSmokeTest();
    console.log(
      JSON.stringify(
        {
          readiness: readiness ?? startupResult,
          criterion2: criterion2Result,
          criterion4: fallbackResult,
          criterion2Pass,
        },
        null,
        2,
      ),
    );
  } finally {
    child.kill();
  }
}

const invokedDirectly =
  process.argv[1] !== undefined &&
  fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (invokedDirectly) {
  main().catch((error) => {
    console.error('A4 spike failed:', errorMessage(error));
    process.exitCode = 1;
  });
}
