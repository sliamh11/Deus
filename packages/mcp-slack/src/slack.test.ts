import { describe, it, expect, beforeEach, vi, afterEach } from 'vitest';

// --- @slack/bolt mock ---

type Handler = (...args: any[]) => any;

const appRef = vi.hoisted(() => ({ current: null as any }));

vi.mock('@slack/bolt', () => ({
  App: class MockApp {
    eventHandlers = new Map<string, Handler>();
    token: string;
    appToken: string;

    client = {
      auth: {
        test: vi.fn().mockResolvedValue({ user_id: 'U_BOT_123' }),
      },
      chat: {
        postMessage: vi.fn().mockResolvedValue(undefined),
      },
      conversations: {
        list: vi.fn().mockResolvedValue({
          channels: [],
          response_metadata: {},
        }),
      },
      users: {
        info: vi.fn().mockResolvedValue({
          user: { real_name: 'Alice Smith', name: 'alice' },
        }),
      },
    };

    constructor(opts: any) {
      this.token = opts.token;
      this.appToken = opts.appToken;
      appRef.current = this;
    }

    event(name: string, handler: Handler) {
      this.eventHandlers.set(name, handler);
    }

    async start() {}
    async stop() {}
  },
  LogLevel: { ERROR: 'error' },
}));

// Mock pino to avoid stderr noise in tests
vi.mock('pino', () => {
  const mockLogger = {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  };
  const pinoFn = () => mockLogger;
  pinoFn.destination = () => ({});
  return { default: pinoFn };
});

// Set env vars before import
process.env.SLACK_BOT_TOKEN = 'xoxb-test-token';
process.env.SLACK_APP_TOKEN = 'xapp-test-token';
process.env.ASSISTANT_NAME = 'Jonesy';

import { SlackProvider } from './slack.js';

// --- Test helpers ---

function currentApp() {
  return appRef.current;
}

function createMessageEvent(overrides: {
  channel?: string;
  channelType?: string;
  user?: string;
  text?: string;
  ts?: string;
  threadTs?: string;
  subtype?: string;
  botId?: string;
}) {
  return {
    channel: overrides.channel ?? 'C0123456789',
    channel_type: overrides.channelType ?? 'channel',
    user: overrides.user ?? 'U_USER_456',
    text: 'text' in overrides ? overrides.text : 'Hello everyone',
    ts: overrides.ts ?? '1704067200.000000',
    thread_ts: overrides.threadTs,
    subtype: overrides.subtype,
    bot_id: overrides.botId,
  };
}

async function triggerMessageEvent(
  event: ReturnType<typeof createMessageEvent>,
) {
  const handler = currentApp().eventHandlers.get('message');
  if (handler) await handler({ event });
}

// --- Tests ---

describe('SlackProvider', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  // --- Connection lifecycle ---

  describe('connection lifecycle', () => {
    it('resolves connect() when app starts', async () => {
      const provider = new SlackProvider();
      await provider.connect();
      expect(provider.isConnected()).toBe(true);
    });

    it('gets bot user ID on connect', async () => {
      const provider = new SlackProvider();
      await provider.connect();
      expect(currentApp().client.auth.test).toHaveBeenCalled();
    });

    it('disconnects cleanly', async () => {
      const provider = new SlackProvider();
      await provider.connect();
      expect(provider.isConnected()).toBe(true);
      await provider.disconnect();
      expect(provider.isConnected()).toBe(false);
    });

    it('isConnected() returns false before connect', () => {
      const provider = new SlackProvider();
      expect(provider.isConnected()).toBe(false);
    });

    it('has name "slack"', () => {
      const provider = new SlackProvider();
      expect(provider.name).toBe('slack');
    });

    it('hasTokens() returns true when both tokens are set', () => {
      const provider = new SlackProvider();
      expect(provider.hasTokens()).toBe(true);
    });
  });

  // --- Status ---

  describe('getStatus', () => {
    it('returns connected status after connect', async () => {
      const provider = new SlackProvider();
      await provider.connect();
      const status = provider.getStatus();
      expect(status.connected).toBe(true);
      expect(status.channel).toBe('slack');
      expect(status.identity).toBe('U_BOT_123');
      expect(status.uptime_seconds).toBeGreaterThanOrEqual(0);
    });

    it('returns disconnected status before connect', () => {
      const provider = new SlackProvider();
      const status = provider.getStatus();
      expect(status.connected).toBe(false);
      expect(status.channel).toBe('slack');
      expect(status.uptime_seconds).toBe(0);
    });
  });

  // --- Message handling ---

  describe('message handling', () => {
    it('delivers message via onMessage callback', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({ text: 'Hello everyone' });
      await triggerMessageEvent(event);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          id: '1704067200.000000',
          chat_id: 'slack:C0123456789',
          sender: 'U_USER_456',
          content: 'Hello everyone',
          is_from_me: false,
          is_group: true,
        }),
      );
    });

    it('skips non-text subtypes (channel_join, etc.)', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({ subtype: 'channel_join' });
      await triggerMessageEvent(event);

      expect(onMessage).not.toHaveBeenCalled();
    });

    it('allows bot_message subtype through', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({
        subtype: 'bot_message',
        botId: 'B_OTHER_BOT',
        text: 'Bot message',
      });
      await triggerMessageEvent(event);

      expect(onMessage).toHaveBeenCalled();
    });

    it('skips messages with no text', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({ text: undefined as any });
      await triggerMessageEvent(event);

      expect(onMessage).not.toHaveBeenCalled();
    });

    it('detects bot messages by bot_id', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({
        subtype: 'bot_message',
        botId: 'B_MY_BOT',
        text: 'Bot response',
      });
      await triggerMessageEvent(event);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          is_from_me: true,
          sender_name: 'Jonesy',
        }),
      );
    });

    it('detects bot messages by matching bot user ID', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({
        user: 'U_BOT_123',
        text: 'Self message',
      });
      await triggerMessageEvent(event);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          is_from_me: true,
        }),
      );
    });

    it('identifies IM channel type as non-group', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({
        channel: 'D0123456789',
        channelType: 'im',
      });
      await triggerMessageEvent(event);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          chat_id: 'slack:D0123456789',
          is_group: false,
        }),
      );
    });

    it('converts ts to ISO timestamp', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({ ts: '1704067200.000000' });
      await triggerMessageEvent(event);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          timestamp: '2024-01-01T00:00:00.000Z',
        }),
      );
    });

    it('resolves user name from Slack API', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({ user: 'U_USER_456', text: 'Hello' });
      await triggerMessageEvent(event);

      expect(currentApp().client.users.info).toHaveBeenCalledWith({
        user: 'U_USER_456',
      });
      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          sender_name: 'Alice Smith',
        }),
      );
    });

    it('caches user names to avoid repeated API calls', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      await triggerMessageEvent(
        createMessageEvent({ user: 'U_USER_456', text: 'First' }),
      );
      await triggerMessageEvent(
        createMessageEvent({
          user: 'U_USER_456',
          text: 'Second',
          ts: '1704067201.000000',
        }),
      );

      expect(currentApp().client.users.info).toHaveBeenCalledTimes(1);
    });

    it('falls back to user ID when API fails', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      currentApp().client.users.info.mockRejectedValueOnce(
        new Error('API error'),
      );

      const event = createMessageEvent({ user: 'U_UNKNOWN', text: 'Hi' });
      await triggerMessageEvent(event);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          sender_name: 'U_UNKNOWN',
        }),
      );
    });
  });

  // --- @mention translation ---

  describe('@mention translation', () => {
    it('prepends trigger when bot is @mentioned via Slack format', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({
        text: 'Hey <@U_BOT_123> what do you think?',
        user: 'U_USER_456',
      });
      await triggerMessageEvent(event);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          content: '@Jonesy Hey <@U_BOT_123> what do you think?',
        }),
      );
    });

    it('does not translate mentions in bot messages', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({
        text: 'Echo: <@U_BOT_123>',
        subtype: 'bot_message',
        botId: 'B_MY_BOT',
      });
      await triggerMessageEvent(event);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          content: 'Echo: <@U_BOT_123>',
        }),
      );
    });

    it('does not translate mentions for other users', async () => {
      const provider = new SlackProvider();
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const event = createMessageEvent({
        text: 'Hey <@U_OTHER_USER> look at this',
        user: 'U_USER_456',
      });
      await triggerMessageEvent(event);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          content: 'Hey <@U_OTHER_USER> look at this',
        }),
      );
    });
  });

  // --- sendMessage ---

  describe('sendMessage', () => {
    it('sends message via Slack client', async () => {
      const provider = new SlackProvider();
      await provider.connect();

      await provider.sendMessage('slack:C0123456789', 'Hello');

      expect(currentApp().client.chat.postMessage).toHaveBeenCalledWith({
        channel: 'C0123456789',
        text: 'Hello',
      });
    });

    it('strips slack: prefix from chat ID', async () => {
      const provider = new SlackProvider();
      await provider.connect();

      await provider.sendMessage('slack:D9876543210', 'DM message');

      expect(currentApp().client.chat.postMessage).toHaveBeenCalledWith({
        channel: 'D9876543210',
        text: 'DM message',
      });
    });

    it('queues message when disconnected', async () => {
      const provider = new SlackProvider();

      await provider.sendMessage('slack:C0123456789', 'Queued message');

      // No app created yet, so postMessage should not be called
      expect(provider.isConnected()).toBe(false);
    });

    it('splits long messages at 4000 character boundary', async () => {
      const provider = new SlackProvider();
      await provider.connect();

      const longText = 'A'.repeat(4500);
      await provider.sendMessage('slack:C0123456789', longText);

      expect(currentApp().client.chat.postMessage).toHaveBeenCalledTimes(2);
      expect(currentApp().client.chat.postMessage).toHaveBeenNthCalledWith(1, {
        channel: 'C0123456789',
        text: 'A'.repeat(4000),
      });
      expect(currentApp().client.chat.postMessage).toHaveBeenNthCalledWith(2, {
        channel: 'C0123456789',
        text: 'A'.repeat(500),
      });
    });

    it('sends exactly-4000-char messages as a single message', async () => {
      const provider = new SlackProvider();
      await provider.connect();

      const text = 'B'.repeat(4000);
      await provider.sendMessage('slack:C0123456789', text);

      expect(currentApp().client.chat.postMessage).toHaveBeenCalledTimes(1);
    });

    it('flushes queued messages on connect', async () => {
      const provider = new SlackProvider();

      // Queue messages while disconnected — create the provider first,
      // then connect will create the app
      await provider.connect();

      // Queue after disconnect
      await provider.disconnect();
      const newProvider = new SlackProvider();
      await newProvider.sendMessage('slack:C0123456789', 'First queued');
      await newProvider.sendMessage('slack:C0123456789', 'Second queued');

      await newProvider.connect();

      expect(currentApp().client.chat.postMessage).toHaveBeenCalledWith({
        channel: 'C0123456789',
        text: 'First queued',
      });
      expect(currentApp().client.chat.postMessage).toHaveBeenCalledWith({
        channel: 'C0123456789',
        text: 'Second queued',
      });
    });
  });

  // --- setTyping ---

  describe('setTyping', () => {
    it('resolves without error (no-op)', async () => {
      const provider = new SlackProvider();

      await expect(
        provider.setTyping('slack:C0123456789', true),
      ).resolves.toBeUndefined();
    });

    it('accepts false without error', async () => {
      const provider = new SlackProvider();

      await expect(
        provider.setTyping('slack:C0123456789', false),
      ).resolves.toBeUndefined();
    });
  });

  // --- listChats ---

  describe('listChats', () => {
    it('returns empty array initially', async () => {
      const provider = new SlackProvider();
      const chats = await provider.listChats();
      expect(chats).toEqual([]);
    });

    it('returns chats after messages are received', async () => {
      const provider = new SlackProvider();
      provider.onMessage = vi.fn();
      await provider.connect();

      await triggerMessageEvent(createMessageEvent({ text: 'Hello' }));

      const chats = await provider.listChats();
      expect(chats.length).toBe(1);
      expect(chats[0].id).toBe('slack:C0123456789');
      expect(chats[0].is_group).toBe(true);
    });
  });

  // --- syncGroups ---

  describe('syncGroups', () => {
    it('fetches channels from Slack API', async () => {
      const provider = new SlackProvider();
      await provider.connect();

      currentApp().client.conversations.list.mockResolvedValue({
        channels: [
          { id: 'C001', name: 'general', is_member: true },
          { id: 'C002', name: 'random', is_member: true },
          { id: 'C003', name: 'external', is_member: false },
        ],
        response_metadata: {},
      });

      const groups = await provider.syncGroups();

      // Only member channels should be in the list
      const ids = groups.map((g) => g.id);
      expect(ids).toContain('slack:C001');
      expect(ids).toContain('slack:C002');
      expect(ids).not.toContain('slack:C003');
    });

    it('handles API errors gracefully', async () => {
      const provider = new SlackProvider();
      await provider.connect();

      currentApp().client.conversations.list.mockRejectedValue(
        new Error('API error'),
      );

      // Should not throw
      await expect(provider.syncGroups()).resolves.toBeDefined();
    });
  });
});
