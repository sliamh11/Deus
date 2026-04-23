import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

const mockEnv: Record<string, string> = {};
vi.mock('../env.js', () => ({
  readEnvFile: vi.fn(() => ({ ...mockEnv })),
}));

vi.mock('../logger.js', () => ({
  logger: { info: vi.fn(), error: vi.fn(), debug: vi.fn(), warn: vi.fn() },
}));

vi.mock('fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('fs')>();
  return {
    ...actual,
    readFileSync: vi.fn(actual.readFileSync),
    writeFileSync: vi.fn(),
  };
});

vi.mock('child_process', async (importOriginal) => {
  const actual = await importOriginal<typeof import('child_process')>();
  return {
    ...actual,
    execFileSync: vi.fn(() => {
      throw new Error('no keychain in test');
    }),
  };
});

// Platform flags — mutable so tests can override per-case
const platformMock = { IS_MACOS: false, IS_LINUX: false, IS_WINDOWS: false };
vi.mock('../platform.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../platform.js')>();
  return {
    ...actual,
    get IS_MACOS() {
      return platformMock.IS_MACOS;
    },
    get IS_LINUX() {
      return platformMock.IS_LINUX;
    },
    get IS_WINDOWS() {
      return platformMock.IS_WINDOWS;
    },
  };
});

import { readFileSync, writeFileSync } from 'fs';
import { execFileSync } from 'child_process';
import { AuthProviderRegistry, NoProviderAvailableError } from './types.js';
import type { AuthProvider } from './types.js';
import { ensureDefaultProviders } from './index.js';
import {
  AnthropicAuthProvider,
  _resetCredentialsCacheForTest,
} from './anthropic.js';
import { OpenAIAuthProvider } from './openai.js';

const mockReadFileSync = readFileSync as ReturnType<typeof vi.fn>;
const mockWriteFileSync = writeFileSync as ReturnType<typeof vi.fn>;
const mockExecFileSync = execFileSync as ReturnType<typeof vi.fn>;

// ---------------------------------------------------------------------------
// Helper: create a minimal mock provider
// ---------------------------------------------------------------------------
function mockProvider(
  name: string,
  priority: number,
  available = true,
): AuthProvider {
  return {
    name,
    priority,
    isAvailable: () => available,
    getUpstreamUrl: () => `https://${name}.example.com`,
    injectAuth: vi.fn(),
    envKeys: [],
  };
}

// ---------------------------------------------------------------------------
// Registry tests
// ---------------------------------------------------------------------------
describe('AuthProviderRegistry', () => {
  beforeEach(() => {
    AuthProviderRegistry.reset();
  });

  afterEach(() => {
    AuthProviderRegistry.reset();
    delete process.env.DEUS_AUTH_PROVIDER;
  });

  it('singleton: default() returns same instance', () => {
    const a = AuthProviderRegistry.default();
    const b = AuthProviderRegistry.default();
    expect(a).toBe(b);
  });

  it('reset clears singleton', () => {
    const a = AuthProviderRegistry.default();
    AuthProviderRegistry.reset();
    const b = AuthProviderRegistry.default();
    expect(a).not.toBe(b);
  });

  it('register + get', () => {
    const reg = AuthProviderRegistry.default();
    const p = mockProvider('test', 10);
    reg.register(p);
    expect(reg.get('test')).toBe(p);
  });

  it('get throws for unknown provider', () => {
    const reg = AuthProviderRegistry.default();
    expect(() => reg.get('nope')).toThrow(NoProviderAvailableError);
  });

  it('unregister removes provider', () => {
    const reg = AuthProviderRegistry.default();
    reg.register(mockProvider('test', 10));
    reg.unregister('test');
    expect(() => reg.get('test')).toThrow(NoProviderAvailableError);
  });

  it('unregister is silent for unknown name', () => {
    const reg = AuthProviderRegistry.default();
    reg.unregister('nope'); // no throw
  });

  it('listProviders returns names sorted by priority', () => {
    const reg = AuthProviderRegistry.default();
    reg.register(mockProvider('low', 30));
    reg.register(mockProvider('high', 5));
    reg.register(mockProvider('mid', 15));
    expect(reg.listProviders()).toEqual(['high', 'mid', 'low']);
  });

  it('listAvailable filters unavailable', () => {
    const reg = AuthProviderRegistry.default();
    reg.register(mockProvider('ok', 10, true));
    reg.register(mockProvider('no', 5, false));
    expect(reg.listAvailable()).toEqual(['ok']);
  });

  it('resolve: auto-detect picks lowest priority available', () => {
    const reg = AuthProviderRegistry.default();
    reg.register(mockProvider('first', 10, true));
    reg.register(mockProvider('second', 20, true));
    expect(reg.resolve().name).toBe('first');
  });

  it('resolve: explicit preference', () => {
    const reg = AuthProviderRegistry.default();
    reg.register(mockProvider('a', 10, true));
    reg.register(mockProvider('b', 20, true));
    expect(reg.resolve('b').name).toBe('b');
  });

  it('resolve: env var overrides preference', () => {
    const reg = AuthProviderRegistry.default();
    reg.register(mockProvider('a', 10, true));
    reg.register(mockProvider('b', 20, true));
    process.env.DEUS_AUTH_PROVIDER = 'b';
    expect(reg.resolve('a').name).toBe('b');
  });

  it('resolve: throws when preferred is not registered', () => {
    const reg = AuthProviderRegistry.default();
    reg.register(mockProvider('a', 10, true));
    expect(() => reg.resolve('nope')).toThrow(NoProviderAvailableError);
  });

  it('resolve: throws when preferred is unavailable', () => {
    const reg = AuthProviderRegistry.default();
    reg.register(mockProvider('a', 10, false));
    expect(() => reg.resolve('a')).toThrow(NoProviderAvailableError);
  });

  it('resolve: throws when nothing is available', () => {
    const reg = AuthProviderRegistry.default();
    reg.register(mockProvider('a', 10, false));
    expect(() => reg.resolve()).toThrow(NoProviderAvailableError);
  });

  it('last-write-wins for same name', () => {
    const reg = AuthProviderRegistry.default();
    reg.register(mockProvider('a', 10, true));
    const replacement = mockProvider('a', 5, true);
    reg.register(replacement);
    expect(reg.get('a')).toBe(replacement);
  });
});

describe('OpenAIAuthProvider', () => {
  beforeEach(() => {
    for (const key of Object.keys(mockEnv)) delete mockEnv[key];
  });

  afterEach(() => {
    for (const key of Object.keys(mockEnv)) delete mockEnv[key];
  });

  it('is available when OPENAI_API_KEY is configured', () => {
    Object.assign(mockEnv, { OPENAI_API_KEY: 'sk-openai-test' });
    const provider = new OpenAIAuthProvider();

    expect(provider.isAvailable()).toBe(true);
    expect(provider.getUpstreamUrl()).toBe('https://api.openai.com');
  });

  it('injects bearer auth and strips x-api-key', () => {
    Object.assign(mockEnv, {
      OPENAI_API_KEY: 'sk-openai-real',
      OPENAI_BASE_URL: 'https://proxy.example.com',
    });
    const provider = new OpenAIAuthProvider();

    const headers: Record<string, string | string[] | undefined> = {
      authorization: 'Bearer placeholder',
      'x-api-key': 'temp-key',
    };
    provider.injectAuth(headers);

    expect(provider.getUpstreamUrl()).toBe('https://proxy.example.com');
    expect(headers.authorization).toBe('Bearer sk-openai-real');
    expect(headers['x-api-key']).toBeUndefined();
  });
});

describe('ensureDefaultProviders', () => {
  beforeEach(() => {
    AuthProviderRegistry.reset();
  });

  afterEach(() => {
    AuthProviderRegistry.reset();
  });

  it('registers anthropic and openai providers once', () => {
    ensureDefaultProviders();
    ensureDefaultProviders();

    const registry = AuthProviderRegistry.default();
    expect(registry.listProviders()).toContain('anthropic');
    expect(registry.listProviders()).toContain('openai');
  });
});

// ---------------------------------------------------------------------------
// AnthropicAuthProvider tests
// ---------------------------------------------------------------------------
describe('AnthropicAuthProvider', () => {
  beforeEach(() => {
    _resetCredentialsCacheForTest();
    mockReadFileSync.mockImplementation(() => {
      throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
    });
    mockWriteFileSync.mockReset();
    mockExecFileSync.mockImplementation(() => {
      throw new Error('no keychain in test');
    });
    platformMock.IS_MACOS = false;
    platformMock.IS_LINUX = false;
    platformMock.IS_WINDOWS = false;
    for (const key of Object.keys(mockEnv)) delete mockEnv[key];
  });

  afterEach(() => {
    for (const key of Object.keys(mockEnv)) delete mockEnv[key];
    mockReadFileSync.mockReset();
    mockWriteFileSync.mockReset();
    mockExecFileSync.mockReset();
    _resetCredentialsCacheForTest();
  });

  it('api-key mode: injectAuth sets x-api-key', () => {
    Object.assign(mockEnv, { ANTHROPIC_API_KEY: 'sk-ant-test' });
    const provider = new AnthropicAuthProvider();
    expect(provider.getAuthMode()).toBe('api-key');

    const headers: Record<string, string | string[] | undefined> = {
      'x-api-key': 'placeholder',
    };
    provider.injectAuth(headers);
    expect(headers['x-api-key']).toBe('sk-ant-test');
  });

  it('oauth mode: injectAuth replaces Authorization header', () => {
    Object.assign(mockEnv, { CLAUDE_CODE_OAUTH_TOKEN: 'oauth-tok' });
    const provider = new AnthropicAuthProvider();
    expect(provider.getAuthMode()).toBe('oauth');

    const headers: Record<string, string | string[] | undefined> = {
      authorization: 'Bearer placeholder',
    };
    provider.injectAuth(headers);
    expect(headers['authorization']).toBe('Bearer oauth-tok');
  });

  it('oauth mode: does not inject when no Authorization header', () => {
    Object.assign(mockEnv, { CLAUDE_CODE_OAUTH_TOKEN: 'oauth-tok' });
    const provider = new AnthropicAuthProvider();

    const headers: Record<string, string | string[] | undefined> = {
      'x-api-key': 'temp-key',
    };
    provider.injectAuth(headers);
    expect(headers['authorization']).toBeUndefined();
    expect(headers['x-api-key']).toBe('temp-key');
  });

  it('isAvailable: true with API key', () => {
    Object.assign(mockEnv, { ANTHROPIC_API_KEY: 'sk-ant-test' });
    const provider = new AnthropicAuthProvider();
    expect(provider.isAvailable()).toBe(true);
  });

  it('isAvailable: true with OAuth token', () => {
    Object.assign(mockEnv, { CLAUDE_CODE_OAUTH_TOKEN: 'tok' });
    const provider = new AnthropicAuthProvider();
    expect(provider.isAvailable()).toBe(true);
  });

  it('isAvailable: true with credentials file', () => {
    mockReadFileSync.mockReturnValue(
      JSON.stringify({
        claudeAiOauth: {
          accessToken: 'creds-tok',
          expiresAt: Date.now() + 3600000,
        },
      }),
    );
    const provider = new AnthropicAuthProvider();
    expect(provider.isAvailable()).toBe(true);
  });

  it('isAvailable: false with nothing configured', () => {
    const provider = new AnthropicAuthProvider();
    expect(provider.isAvailable()).toBe(false);
  });

  it('getUpstreamUrl: returns default', () => {
    const provider = new AnthropicAuthProvider();
    expect(provider.getUpstreamUrl()).toBe('https://api.anthropic.com');
  });

  it('getUpstreamUrl: returns custom base URL', () => {
    Object.assign(mockEnv, {
      ANTHROPIC_BASE_URL: 'http://localhost:9999',
    });
    const provider = new AnthropicAuthProvider();
    expect(provider.getUpstreamUrl()).toBe('http://localhost:9999');
  });

  it('envKeys includes expected keys', () => {
    const provider = new AnthropicAuthProvider();
    expect(provider.envKeys).toContain('ANTHROPIC_API_KEY');
    expect(provider.envKeys).toContain('CLAUDE_CODE_OAUTH_TOKEN');
    expect(provider.envKeys).toContain('ANTHROPIC_AUTH_TOKEN');
    expect(provider.envKeys).toContain('ANTHROPIC_BASE_URL');
  });

  it('name and priority', () => {
    const provider = new AnthropicAuthProvider();
    expect(provider.name).toBe('anthropic');
    expect(provider.priority).toBe(10);
  });

  // -------------------------------------------------------------------------
  // Credential store fallback tests
  // -------------------------------------------------------------------------
  describe('credential store fallback', () => {
    const keychainCreds = JSON.stringify({
      claudeAiOauth: {
        accessToken: 'keychain-tok',
        refreshToken: 'keychain-refresh',
        expiresAt: Date.now() + 7200000,
      },
    });

    it('macOS: reads from Keychain when file is missing', () => {
      platformMock.IS_MACOS = true;
      mockExecFileSync.mockReturnValue(keychainCreds);
      const provider = new AnthropicAuthProvider();
      expect(provider.isAvailable()).toBe(true);
      // Verify the right CLI was called
      expect(mockExecFileSync).toHaveBeenCalledWith(
        'security',
        expect.arrayContaining([
          'find-generic-password',
          '-s',
          'Claude Code-credentials',
        ]),
        expect.any(Object),
      );
    });

    it('Linux: reads from secret-tool when file is missing', () => {
      platformMock.IS_LINUX = true;
      mockExecFileSync.mockReturnValue(keychainCreds);
      const provider = new AnthropicAuthProvider();
      expect(provider.isAvailable()).toBe(true);
      expect(mockExecFileSync).toHaveBeenCalledWith(
        'secret-tool',
        expect.arrayContaining([
          'lookup',
          'service',
          'Claude Code-credentials',
        ]),
        expect.any(Object),
      );
    });

    it('Windows: reads from Credential Manager when file is missing', () => {
      platformMock.IS_WINDOWS = true;
      mockExecFileSync.mockReturnValue(keychainCreds);
      const provider = new AnthropicAuthProvider();
      expect(provider.isAvailable()).toBe(true);
      expect(mockExecFileSync).toHaveBeenCalledWith(
        'powershell.exe',
        expect.arrayContaining([
          '-NoProfile',
          '-NonInteractive',
          '-Command',
          expect.stringContaining('Get-StoredCredential'),
        ]),
        expect.any(Object),
      );
    });

    it('syncs keychain credentials to disk via writeFileSync', () => {
      platformMock.IS_MACOS = true;
      mockExecFileSync.mockReturnValue(keychainCreds);
      const provider = new AnthropicAuthProvider();
      provider.isAvailable(); // triggers getDynamicOAuthToken → keychain read → write
      expect(mockWriteFileSync).toHaveBeenCalledWith(
        expect.stringContaining('.credentials.json'),
        expect.stringContaining('keychain-tok'),
        expect.objectContaining({ mode: 0o600 }),
      );
    });

    it('returns undefined when both file and credential store are empty', () => {
      platformMock.IS_MACOS = true;
      // readFileSync throws (no file), execFileSync throws (no keychain entry)
      const provider = new AnthropicAuthProvider();
      expect(provider.isAvailable()).toBe(false);
    });

    it('prefers file over credential store when file exists', () => {
      platformMock.IS_MACOS = true;
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'file-tok',
            expiresAt: Date.now() + 7200000,
          },
        }),
      );
      mockExecFileSync.mockReturnValue(keychainCreds);
      const provider = new AnthropicAuthProvider();
      const headers: Record<string, string | string[] | undefined> = {
        authorization: 'Bearer placeholder',
      };
      provider.injectAuth(headers);
      expect(headers['authorization']).toBe('Bearer file-tok');
      // Keychain should not have been called
      expect(mockExecFileSync).not.toHaveBeenCalled();
    });

    it('credential store token is injected into Authorization header', () => {
      platformMock.IS_LINUX = true;
      mockExecFileSync.mockReturnValue(keychainCreds);
      const provider = new AnthropicAuthProvider();
      const headers: Record<string, string | string[] | undefined> = {
        authorization: 'Bearer placeholder',
      };
      provider.injectAuth(headers);
      expect(headers['authorization']).toBe('Bearer keychain-tok');
    });

    it('handles malformed JSON from credential store gracefully', () => {
      platformMock.IS_MACOS = true;
      mockExecFileSync.mockReturnValue('not-json{{{');
      const provider = new AnthropicAuthProvider();
      expect(provider.isAvailable()).toBe(false);
    });

    it('handles credential store returning empty accessToken', () => {
      platformMock.IS_MACOS = true;
      mockExecFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: { accessToken: '', expiresAt: Date.now() + 3600000 },
        }),
      );
      const provider = new AnthropicAuthProvider();
      expect(provider.isAvailable()).toBe(false);
    });
  });

  // -------------------------------------------------------------------------
  // Cache behavior tests
  // -------------------------------------------------------------------------
  describe('cache behavior', () => {
    it('returns cached token without re-reading file', () => {
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'cached-tok',
            expiresAt: Date.now() + 7200000,
          },
        }),
      );
      const provider = new AnthropicAuthProvider();

      const h1: Record<string, string | string[] | undefined> = {
        authorization: 'Bearer x',
      };
      provider.injectAuth(h1);
      expect(h1['authorization']).toBe('Bearer cached-tok');

      // Change what the file returns — should still get cached value
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'new-tok',
            expiresAt: Date.now() + 7200000,
          },
        }),
      );
      const h2: Record<string, string | string[] | undefined> = {
        authorization: 'Bearer x',
      };
      provider.injectAuth(h2);
      expect(h2['authorization']).toBe('Bearer cached-tok');
    });

    it('invalidates cache when token is about to expire', () => {
      // Token expires in 10 min (within 30-min early-expire window)
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'expiring-tok',
            expiresAt: Date.now() + 10 * 60 * 1000,
          },
        }),
      );
      const provider = new AnthropicAuthProvider();

      const h1: Record<string, string | string[] | undefined> = {
        authorization: 'Bearer x',
      };
      provider.injectAuth(h1);
      expect(h1['authorization']).toBe('Bearer expiring-tok');

      // Now update the file — cache should be stale due to early-expire window
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'refreshed-tok',
            expiresAt: Date.now() + 7200000,
          },
        }),
      );
      const h2: Record<string, string | string[] | undefined> = {
        authorization: 'Bearer x',
      };
      provider.injectAuth(h2);
      expect(h2['authorization']).toBe('Bearer refreshed-tok');
    });
  });

  // -------------------------------------------------------------------------
  // Auto-refresh tests
  // -------------------------------------------------------------------------
  describe('auto-refresh', () => {
    let fetchSpy: ReturnType<typeof vi.fn>;

    beforeEach(() => {
      fetchSpy = vi.fn();
      vi.stubGlobal('fetch', fetchSpy);
    });

    afterEach(() => {
      vi.unstubAllGlobals();
    });

    it('triggers refresh when token expires within 30-min window', async () => {
      fetchSpy.mockResolvedValue({
        ok: true,
        json: () =>
          Promise.resolve({
            access_token: 'fresh-tok',
            refresh_token: 'fresh-refresh',
            expires_in: 28800,
          }),
      });

      // Token expires in 10 min, has refresh_token
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'old-tok',
            refreshToken: 'old-refresh',
            expiresAt: Date.now() + 10 * 60 * 1000,
          },
        }),
      );

      const provider = new AnthropicAuthProvider();
      provider.isAvailable(); // triggers getDynamicOAuthToken

      // Wait for the async refresh to complete
      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledTimes(1);
      });

      expect(fetchSpy).toHaveBeenCalledWith(
        'https://platform.claude.com/v1/oauth/token',
        expect.objectContaining({
          method: 'POST',
          body: expect.stringContaining('old-refresh'),
        }),
      );

      // After refresh, writeFileSync should have been called with the new token
      await vi.waitFor(() => {
        expect(mockWriteFileSync).toHaveBeenCalledWith(
          expect.stringContaining('.credentials.json'),
          expect.stringContaining('fresh-tok'),
          expect.objectContaining({ mode: 0o600 }),
        );
      });
    });

    it('does not trigger refresh when token has plenty of time left', () => {
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'good-tok',
            refreshToken: 'refresh-tok',
            expiresAt: Date.now() + 7200000, // 2 hours
          },
        }),
      );

      const provider = new AnthropicAuthProvider();
      provider.isAvailable();

      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('does not trigger refresh when no refresh_token available', () => {
      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'no-refresh-tok',
            expiresAt: Date.now() + 10 * 60 * 1000, // expiring soon
          },
        }),
      );

      const provider = new AnthropicAuthProvider();
      provider.isAvailable();

      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('deduplicates concurrent refresh attempts', async () => {
      fetchSpy.mockImplementation(
        () =>
          new Promise((resolve) =>
            setTimeout(
              () =>
                resolve({
                  ok: true,
                  json: () =>
                    Promise.resolve({
                      access_token: 'deduped-tok',
                      refresh_token: 'deduped-refresh',
                      expires_in: 28800,
                    }),
                }),
              50,
            ),
          ),
      );

      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'dup-tok',
            refreshToken: 'dup-refresh',
            expiresAt: Date.now() + 10 * 60 * 1000,
          },
        }),
      );

      const provider = new AnthropicAuthProvider();
      // First call triggers refresh, sets refreshInFlight = true
      const h1: Record<string, string | string[] | undefined> = {
        authorization: 'Bearer x',
      };
      provider.injectAuth(h1);
      // Second call while refresh is still in flight — should be deduped
      // Manually clear just the credentials cache (not refreshInFlight)
      // by calling injectAuth again which re-enters getDynamicOAuthToken
      const h2: Record<string, string | string[] | undefined> = {
        authorization: 'Bearer x',
      };
      provider.injectAuth(h2);

      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalled();
      });

      // Only one fetch call despite two injectAuth calls
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });

    it('handles failed refresh gracefully', async () => {
      fetchSpy.mockResolvedValue({ ok: false, status: 401 });

      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'stale-tok',
            refreshToken: 'bad-refresh',
            expiresAt: Date.now() + 10 * 60 * 1000,
          },
        }),
      );

      const provider = new AnthropicAuthProvider();
      const headers: Record<string, string | string[] | undefined> = {
        authorization: 'Bearer placeholder',
      };
      provider.injectAuth(headers);

      // Should still return the stale token
      expect(headers['authorization']).toBe('Bearer stale-tok');

      // Wait for the failed refresh to complete
      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledTimes(1);
      });

      // writeFileSync should NOT have been called (refresh failed)
      expect(mockWriteFileSync).not.toHaveBeenCalled();
    });

    it('handles network error during refresh gracefully', async () => {
      fetchSpy.mockRejectedValue(new Error('network error'));

      mockReadFileSync.mockReturnValue(
        JSON.stringify({
          claudeAiOauth: {
            accessToken: 'net-err-tok',
            refreshToken: 'net-refresh',
            expiresAt: Date.now() + 10 * 60 * 1000,
          },
        }),
      );

      const provider = new AnthropicAuthProvider();
      expect(provider.isAvailable()).toBe(true);

      await vi.waitFor(() => {
        expect(fetchSpy).toHaveBeenCalledTimes(1);
      });

      // No crash, no write
      expect(mockWriteFileSync).not.toHaveBeenCalled();
    });
  });
});
