import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('botbuilder', () => ({
  ActivityTypes: { Message: 'message' },
  CloudAdapter: class MockCloudAdapter {
    onTurnError: unknown = null;
    process = vi.fn();
    continueConversationAsync = vi.fn();
  },
  ConfigurationBotFrameworkAuthentication: class MockAuth {},
  ConfigurationServiceClientCredentialFactory: class MockCredFactory {},
  TurnContext: {
    getConversationReference: vi.fn(() => ({ conversation: { id: 'c1' } })),
  },
}));

vi.mock('express', () => {
  const app = {
    use: vi.fn(),
    post: vi.fn(),
    listen: vi.fn((_port: number, cb: () => void) => {
      cb();
      return { close: (done: () => void) => done() };
    }),
  };
  const expressFn: (() => typeof app) & { json: () => unknown } = Object.assign(
    () => app,
    { json: () => () => {} },
  );
  return { default: expressFn };
});

vi.mock('pino', () => {
  const mockLogger = {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
    fatal: vi.fn(),
  };
  const pinoFn = (() => mockLogger) as (() => typeof mockLogger) & {
    destination: () => unknown;
  };
  pinoFn.destination = () => ({});
  return { default: pinoFn };
});

import { TeamsProvider } from './teams.js';

describe('TeamsProvider', () => {
  let provider: TeamsProvider;

  beforeEach(() => {
    delete process.env.TEAMS_APP_ID;
    delete process.env.TEAMS_APP_PASSWORD;
    provider = new TeamsProvider();
  });

  describe('name', () => {
    it('is teams', () => {
      expect(provider.name).toBe('teams');
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
      expect(status.channel).toBe('teams');
      expect(status.uptime_seconds).toBe(0);
    });
  });

  describe('hasCredentials', () => {
    it('returns false when app id/password are unset', () => {
      expect(provider.hasCredentials()).toBe(false);
    });
    it('returns true when both app id and password are set', () => {
      process.env.TEAMS_APP_ID = 'app-id';
      process.env.TEAMS_APP_PASSWORD = 'secret';
      expect(provider.hasCredentials()).toBe(true);
    });
  });

  describe('listChats', () => {
    it('returns empty array before any messages', async () => {
      const chats = await provider.listChats();
      expect(chats).toEqual([]);
    });
  });

  describe('sendMessage failure propagation', () => {
    it('throws when no conversation reference exists', async () => {
      // No prior inbound → no stored reference — must throw, not swallow.
      await expect(
        provider.sendMessage('teams:conv-1', 'hello'),
      ).rejects.toThrow('No conversation reference for reply');
    });

    it('throws when the underlying send fails', async () => {
      (provider as any).adapter = {
        continueConversationAsync: vi
          .fn()
          .mockRejectedValue(new Error('bot framework error')),
      };
      (provider as any).conversationRefs.set('conv-1', {
        conversation: { id: 'c1' },
      });

      await expect(
        provider.sendMessage('teams:conv-1', 'hello'),
      ).rejects.toThrow('bot framework error');
    });
  });

  describe('disconnect', () => {
    it('sets connected to false', async () => {
      await provider.disconnect();
      expect(provider.isConnected()).toBe(false);
    });
  });
});
