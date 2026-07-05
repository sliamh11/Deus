import { describe, it, expect, vi, beforeEach } from 'vitest';

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
