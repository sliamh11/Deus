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
  return { ...actual, readFileSync: vi.fn(actual.readFileSync) };
});

import { readFileSync } from 'fs';
import { AuthProviderRegistry, NoProviderAvailableError } from './types.js';
import type { AuthProvider } from './types.js';
import {
  AnthropicAuthProvider,
  _resetCredentialsCacheForTest,
} from './anthropic.js';

const mockReadFileSync = readFileSync as ReturnType<typeof vi.fn>;

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

// ---------------------------------------------------------------------------
// AnthropicAuthProvider tests
// ---------------------------------------------------------------------------
describe('AnthropicAuthProvider', () => {
  beforeEach(() => {
    _resetCredentialsCacheForTest();
    mockReadFileSync.mockImplementation(() => {
      throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
    });
    for (const key of Object.keys(mockEnv)) delete mockEnv[key];
  });

  afterEach(() => {
    for (const key of Object.keys(mockEnv)) delete mockEnv[key];
    mockReadFileSync.mockReset();
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
});
