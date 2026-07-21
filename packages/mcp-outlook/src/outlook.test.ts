import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

vi.mock('@microsoft/microsoft-graph-client', () => ({
  Client: {
    init: vi.fn(() => ({
      api: vi.fn(() => ({
        filter: vi.fn().mockReturnThis(),
        top: vi.fn().mockReturnThis(),
        select: vi.fn().mockReturnThis(),
        search: vi.fn().mockReturnThis(),
        get: vi.fn().mockResolvedValue({ value: [] }),
        post: vi.fn().mockResolvedValue({}),
        patch: vi.fn().mockResolvedValue({}),
      })),
    })),
  },
}));

vi.mock('@azure/msal-node', () => ({
  // Only the public client is used (delegated device-code flow).
  PublicClientApplication: class MockPublic {
    getTokenCache = () => ({ getAllAccounts: vi.fn().mockResolvedValue([]) });
    acquireTokenSilent = vi.fn();
    acquireTokenByDeviceCode = vi.fn();
  },
}));

vi.mock('pino', () => {
  const mockLogger = {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
    fatal: vi.fn(),
  };
  // pino is called as `pino(opts, dest)` and exposes `pino.destination`; model
  // both as a callable carrying a `destination` member (mirrors gmail.test.ts).
  const pinoFn = (() => mockLogger) as (() => typeof mockLogger) & {
    destination: () => unknown;
  };
  pinoFn.destination = () => ({});
  return { default: pinoFn };
});

import { OutlookProvider, buildMsalClient } from './outlook.js';

describe('OutlookProvider', () => {
  let provider: OutlookProvider;

  beforeEach(() => {
    provider = new OutlookProvider();
  });

  describe('name', () => {
    it('is outlook', () => {
      expect(provider.name).toBe('outlook');
    });
  });

  describe('isConnected', () => {
    it('returns false before connect', () => {
      expect(provider.isConnected()).toBe(false);
    });
  });

  describe('getStatus', () => {
    it('returns disconnected status before connect', () => {
      const status = provider.getStatus();
      expect(status.connected).toBe(false);
      expect(status.channel).toBe('outlook');
      expect(status.uptime_seconds).toBe(0);
    });
  });

  describe('disconnect', () => {
    it('sets connected to false', async () => {
      await provider.disconnect();
      expect(provider.isConnected()).toBe(false);
    });
  });

  describe('hasCredentials', () => {
    it('returns false when the credentials directory has no files', () => {
      // Default CREDENTIALS_DIR is ~/.outlook-mcp/ which has no creds in test.
      expect(provider.hasCredentials()).toBe(false);
    });
  });

  describe('listChats', () => {
    it('returns empty array before any messages', async () => {
      const chats = await provider.listChats();
      expect(chats).toEqual([]);
    });
  });

  describe('sendMessage failure propagation', () => {
    it('throws when not connected (no graph client)', async () => {
      await expect(
        provider.sendMessage('outlook:conv-123', 'hello'),
      ).rejects.toThrow('Outlook not initialized');
    });

    it('throws when there is no conversation metadata for the reply', async () => {
      (provider as any).graph = { api: vi.fn() };

      await expect(
        provider.sendMessage('outlook:conv-123', 'hello'),
      ).rejects.toThrow('No conversation metadata for reply');
    });

    it('throws when the underlying send fails', async () => {
      const post = vi.fn().mockRejectedValue(new Error('graph API error'));
      (provider as any).graph = { api: vi.fn(() => ({ post })) };
      (provider as any).convMeta.set('conv-123', {
        messageId: 'msg-1',
        sender: 'a@b.com',
        senderName: 'A',
        subject: 'hi',
      });

      await expect(
        provider.sendMessage('outlook:conv-123', 'hello'),
      ).rejects.toThrow('graph API error');
    });
  });

  describe('buildMsalClient', () => {
    it('builds a public client (device-code capable, no confidential flow)', () => {
      const client = buildMsalClient({ clientId: 'cid', tenantId: 'tid' });
      // Public client exposes the device-code flow; confidential is unsupported.
      expect(
        typeof (client as { acquireTokenByDeviceCode?: unknown })
          .acquireTokenByDeviceCode,
      ).toBe('function');
    });
  });
});

// Separate describe block: exercises real file I/O against a scratch
// CREDENTIALS_DIR, so the module must be freshly re-imported per test after
// setting OUTLOOK_CREDENTIALS_DIR (the const is computed once at module load).
describe('buildCachePlugin (token cache write/read race fix)', () => {
  let scratchDir: string;

  beforeEach(async () => {
    const fs = await import('fs');
    const os = await import('os');
    const path = await import('path');
    scratchDir = fs.mkdtempSync(
      path.join(os.tmpdir(), 'mcp-outlook-cache-test-'),
    );
    process.env.OUTLOOK_CREDENTIALS_DIR = scratchDir;
    vi.resetModules();
  });

  afterEach(async () => {
    const fs = await import('fs');
    delete process.env.OUTLOOK_CREDENTIALS_DIR;
    fs.rmSync(scratchDir, { recursive: true, force: true });
  });

  it('does not throw when the on-disk cache is corrupt — logs and continues', async () => {
    const fs = await import('fs');
    const path = await import('path');
    const { buildCachePlugin, tokenCachePath } = await import('./outlook.js');

    // Same shape as the real corruption observed: a complete JSON object
    // followed by a trailing fragment of another writer's content.
    fs.writeFileSync(
      path.join(scratchDir, 'token.json'),
      '{"Account":{}},"AppMetadata":{}}ta":{}}',
    );

    const plugin = buildCachePlugin();
    const deserialize = vi.fn(() => {
      throw new SyntaxError('Unexpected non-whitespace character after JSON');
    });
    await expect(
      plugin.beforeCacheAccess({
        tokenCache: { deserialize },
      } as never),
    ).resolves.toBeUndefined();
    expect(deserialize).toHaveBeenCalledOnce();
    // The corrupt file is left alone by the read path (only the next write
    // replaces it) — confirms no crash occurred trying to "fix" it in place.
    expect(fs.existsSync(tokenCachePath())).toBe(true);
  });

  it('writes atomically — no partial file, no leftover temp file', async () => {
    const fs = await import('fs');
    const path = await import('path');
    const { buildCachePlugin, tokenCachePath } = await import('./outlook.js');

    const plugin = buildCachePlugin();
    const serialized = '{"Account":{"real":"cache-content"}}';
    await plugin.afterCacheAccess({
      cacheHasChanged: true,
      tokenCache: { serialize: () => serialized },
    } as never);

    expect(fs.readFileSync(tokenCachePath(), 'utf-8')).toBe(serialized);
    const leftoverTempFiles = fs
      .readdirSync(scratchDir)
      .filter((f) => f.includes('.tmp.'));
    expect(leftoverTempFiles).toEqual([]);
  });

  it('skips the write entirely when the cache has not changed', async () => {
    const fs = await import('fs');
    const path = await import('path');
    const { buildCachePlugin, tokenCachePath } = await import('./outlook.js');

    const plugin = buildCachePlugin();
    await plugin.afterCacheAccess({
      cacheHasChanged: false,
      tokenCache: { serialize: () => 'should-not-be-written' },
    } as never);

    expect(fs.existsSync(tokenCachePath())).toBe(false);
  });
});
