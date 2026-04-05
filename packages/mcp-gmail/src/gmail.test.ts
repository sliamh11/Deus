import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('googleapis', () => {
  const mockGmail = {
    users: {
      getProfile: vi.fn(),
      messages: {
        list: vi.fn(),
        get: vi.fn(),
        send: vi.fn(),
        modify: vi.fn(),
      },
      drafts: {
        create: vi.fn(),
      },
    },
  };
  return {
    google: {
      auth: {
        OAuth2: class MockOAuth2 {
          setCredentials = vi.fn();
          on = vi.fn();
        },
      },
      gmail: () => mockGmail,
    },
    gmail_v1: {},
  };
});

vi.mock('google-auth-library', () => ({
  OAuth2Client: class MockOAuth2Client {
    setCredentials = vi.fn();
    on = vi.fn();
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
  const pinoFn: any = () => mockLogger;
  pinoFn.destination = () => ({});
  return { default: pinoFn };
});

import { GmailProvider } from './gmail.js';

describe('GmailProvider', () => {
  let provider: GmailProvider;

  beforeEach(() => {
    provider = new GmailProvider();
  });

  describe('name', () => {
    it('is gmail', () => {
      expect(provider.name).toBe('gmail');
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
      expect(status.channel).toBe('gmail');
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
    it('returns false when credentials directory does not exist', () => {
      // Default CREDENTIALS_DIR is ~/.gmail-mcp/ which likely has no keys in test
      expect(provider.hasCredentials()).toBe(false);
    });
  });

  describe('listChats', () => {
    it('returns empty array before any messages', async () => {
      const chats = await provider.listChats();
      expect(chats).toEqual([]);
    });
  });
});
