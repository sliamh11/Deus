import { describe, it, expect, vi, beforeEach } from 'vitest';

// vi.hoisted runs before vi.mock hoisting — set env before module evaluation
vi.hoisted(() => {
  process.env.DISCORD_BOT_TOKEN = 'test-token-123';
});

// --- discord.js mock ---

type Handler = (...args: any[]) => any;

const clientRef = vi.hoisted(() => ({ current: null as any }));

const mockChannelFetch = vi.fn().mockResolvedValue({
  send: vi.fn().mockResolvedValue(undefined),
  sendTyping: vi.fn().mockResolvedValue(undefined),
});

vi.mock('discord.js', () => {
  const Events = {
    MessageCreate: 'messageCreate',
    ClientReady: 'ready',
    Error: 'error',
  };

  const GatewayIntentBits = {
    Guilds: 1,
    GuildMessages: 2,
    MessageContent: 4,
    DirectMessages: 8,
  };

  class MockClient {
    eventHandlers = new Map<string, Handler[]>();
    user: any = { id: '999888777', tag: 'Deus#1234' };
    private _ready = false;

    constructor(_opts: any) {
      clientRef.current = this;
    }

    on(event: string, handler: Handler) {
      const existing = this.eventHandlers.get(event) || [];
      existing.push(handler);
      this.eventHandlers.set(event, existing);
      return this;
    }

    once(event: string, handler: Handler) {
      return this.on(event, handler);
    }

    async login(_token: string) {
      this._ready = true;
      const readyHandlers = this.eventHandlers.get('ready') || [];
      for (const h of readyHandlers) {
        h({ user: this.user });
      }
    }

    isReady() {
      return this._ready;
    }

    channels = {
      fetch: mockChannelFetch,
    };

    destroy() {
      this._ready = false;
    }
  }

  class TextChannel {}

  return {
    Client: MockClient,
    Events,
    GatewayIntentBits,
    TextChannel,
  };
});

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

import { DiscordProvider } from './discord.js';

// --- Test helpers ---

function currentClient() {
  return clientRef.current;
}

function createMessage(overrides: {
  channelId?: string;
  content?: string;
  authorId?: string;
  authorUsername?: string;
  authorDisplayName?: string;
  memberDisplayName?: string;
  isBot?: boolean;
  guildName?: string;
  channelName?: string;
  messageId?: string;
  createdAt?: Date;
  attachments?: Map<string, any>;
  reference?: { messageId?: string };
  mentionsBotId?: boolean;
}) {
  const channelId = overrides.channelId ?? '1234567890123456';
  const authorId = overrides.authorId ?? '55512345';
  const botId = '999888777';

  const mentionsMap = new Map();
  if (overrides.mentionsBotId) {
    mentionsMap.set(botId, { id: botId });
  }

  return {
    channelId,
    id: overrides.messageId ?? 'msg_001',
    content: overrides.content ?? 'Hello everyone',
    createdAt: overrides.createdAt ?? new Date('2024-01-01T00:00:00.000Z'),
    author: {
      id: authorId,
      username: overrides.authorUsername ?? 'alice',
      displayName: overrides.authorDisplayName ?? 'Alice',
      bot: overrides.isBot ?? false,
    },
    member: overrides.memberDisplayName
      ? { displayName: overrides.memberDisplayName }
      : null,
    guild: overrides.guildName ? { name: overrides.guildName } : null,
    channel: {
      name: overrides.channelName ?? 'general',
      messages: {
        fetch: vi.fn().mockResolvedValue({
          id: 'replied_msg_id',
          content: 'Original message',
          author: { username: 'Bob', displayName: 'Bob' },
          member: { displayName: 'Bob' },
        }),
      },
    },
    mentions: {
      users: mentionsMap,
    },
    attachments: overrides.attachments ?? new Map(),
    reference: overrides.reference ?? null,
  };
}

async function triggerMessage(message: any) {
  const handlers = currentClient().eventHandlers.get('messageCreate') || [];
  for (const h of handlers) await h(message);
}

// --- Tests ---

describe('DiscordProvider', () => {
  let provider: DiscordProvider;

  beforeEach(() => {
    vi.clearAllMocks();
    mockChannelFetch.mockResolvedValue({
      send: vi.fn().mockResolvedValue(undefined),
      sendTyping: vi.fn().mockResolvedValue(undefined),
    });
    provider = new DiscordProvider();
  });

  // --- Connection lifecycle ---

  describe('connection lifecycle', () => {
    it('resolves connect() when client is ready', async () => {
      await provider.connect();
      expect(provider.isConnected()).toBe(true);
    });

    it('registers message handlers on connect', async () => {
      await provider.connect();
      expect(currentClient().eventHandlers.has('messageCreate')).toBe(true);
      expect(currentClient().eventHandlers.has('error')).toBe(true);
      expect(currentClient().eventHandlers.has('ready')).toBe(true);
    });

    it('disconnects cleanly', async () => {
      await provider.connect();
      expect(provider.isConnected()).toBe(true);

      await provider.disconnect();
      expect(provider.isConnected()).toBe(false);
    });

    it('isConnected() returns false before connect', () => {
      expect(provider.isConnected()).toBe(false);
    });
  });

  // --- Status ---

  describe('status', () => {
    it('returns correct status when connected', async () => {
      await provider.connect();
      const status = provider.getStatus();
      expect(status.connected).toBe(true);
      expect(status.channel).toBe('discord');
      expect(status.identity).toBe('Deus#1234');
    });

    it('returns disconnected status before connect', () => {
      const status = provider.getStatus();
      expect(status.connected).toBe(false);
      expect(status.channel).toBe('discord');
      expect(status.uptime_seconds).toBe(0);
    });

    it('has name "discord"', () => {
      expect(provider.name).toBe('discord');
    });
  });

  // --- Text message handling ---

  describe('text message handling', () => {
    it('delivers message via onMessage callback', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const msg = createMessage({
        content: 'Hello everyone',
        guildName: 'Test Server',
        channelName: 'general',
      });
      await triggerMessage(msg);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          id: 'msg_001',
          chat_id: 'dc:1234567890123456',
          sender: '55512345',
          sender_name: 'Alice',
          content: 'Hello everyone',
          is_from_me: false,
          is_group: true,
          chat_name: 'Test Server #general',
        }),
      );
    });

    it('ignores bot messages', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const msg = createMessage({ isBot: true, content: 'I am a bot' });
      await triggerMessage(msg);

      expect(onMessage).not.toHaveBeenCalled();
    });

    it('uses member displayName when available', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const msg = createMessage({
        content: 'Hi',
        memberDisplayName: 'Alice Nickname',
        authorDisplayName: 'Alice Global',
        guildName: 'Server',
      });
      await triggerMessage(msg);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({ sender_name: 'Alice Nickname' }),
      );
    });

    it('uses sender name for DM chats (no guild)', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const msg = createMessage({
        content: 'Hello',
        guildName: undefined,
        authorDisplayName: 'Alice',
      });
      await triggerMessage(msg);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          is_group: false,
          chat_name: 'Alice',
        }),
      );
    });
  });

  // --- @mention translation ---

  describe('@mention translation', () => {
    it('translates <@botId> mention to @AssistantName format', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const msg = createMessage({
        content: '<@999888777> what time is it?',
        mentionsBotId: true,
        guildName: 'Server',
      });
      await triggerMessage(msg);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          content: '@Deus what time is it?',
        }),
      );
    });

    it('handles <@!botId> (nickname mention format)', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const msg = createMessage({
        content: '<@!999888777> check this',
        mentionsBotId: true,
        guildName: 'Server',
      });
      await triggerMessage(msg);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          content: '@Deus check this',
        }),
      );
    });

    it('does not translate when bot is not mentioned', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const msg = createMessage({
        content: 'hello everyone',
        guildName: 'Server',
      });
      await triggerMessage(msg);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          content: 'hello everyone',
        }),
      );
    });
  });

  // --- Attachments ---

  describe('attachments', () => {
    it('stores image attachment with placeholder', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const attachments = new Map([
        ['att1', { name: 'photo.png', contentType: 'image/png' }],
      ]);
      const msg = createMessage({
        content: '',
        attachments,
        guildName: 'Server',
      });
      await triggerMessage(msg);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          content: '[Image: photo.png]',
        }),
      );
    });

    it('includes text content with attachments', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const attachments = new Map([
        ['att1', { name: 'photo.jpg', contentType: 'image/jpeg' }],
      ]);
      const msg = createMessage({
        content: 'Check this out',
        attachments,
        guildName: 'Server',
      });
      await triggerMessage(msg);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          content: 'Check this out\n[Image: photo.jpg]',
        }),
      );
    });

    it('handles multiple attachments', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const attachments = new Map([
        ['att1', { name: 'a.png', contentType: 'image/png' }],
        ['att2', { name: 'b.txt', contentType: 'text/plain' }],
      ]);
      const msg = createMessage({
        content: '',
        attachments,
        guildName: 'Server',
      });
      await triggerMessage(msg);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          content: '[Image: a.png]\n[File: b.txt]',
        }),
      );
    });
  });

  // --- Reply context ---

  describe('reply context', () => {
    it('includes reply metadata', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const msg = createMessage({
        content: 'I agree with that',
        reference: { messageId: 'original_msg_id' },
        guildName: 'Server',
      });
      await triggerMessage(msg);

      expect(onMessage).toHaveBeenCalledWith(
        expect.objectContaining({
          content: 'I agree with that',
          metadata: expect.objectContaining({
            reply_to_message_id: 'replied_msg_id',
            reply_to_sender_name: 'Bob',
          }),
        }),
      );
    });
  });

  // --- sendMessage ---

  describe('sendMessage', () => {
    it('sends message via channel', async () => {
      await provider.connect();

      await provider.sendMessage('dc:1234567890123456', 'Hello');
      expect(mockChannelFetch).toHaveBeenCalledWith('1234567890123456');
    });

    it('strips dc: prefix from chat ID', async () => {
      await provider.connect();

      await provider.sendMessage('dc:9876543210', 'Test');
      expect(mockChannelFetch).toHaveBeenCalledWith('9876543210');
    });

    it('handles send failure gracefully', async () => {
      await provider.connect();

      mockChannelFetch.mockRejectedValueOnce(new Error('Channel not found'));

      await expect(
        provider.sendMessage('dc:1234567890123456', 'Will fail'),
      ).resolves.toBeUndefined();
    });

    it('does nothing when client is not initialized', async () => {
      await provider.sendMessage('dc:1234567890123456', 'No client');
      // No error, no API call
      expect(mockChannelFetch).not.toHaveBeenCalled();
    });

    it('splits messages exceeding 2000 characters', async () => {
      await provider.connect();

      const mockSend = vi.fn().mockResolvedValue(undefined);
      mockChannelFetch.mockResolvedValue({
        send: mockSend,
        sendTyping: vi.fn(),
      });

      const longText = 'x'.repeat(3000);
      await provider.sendMessage('dc:1234567890123456', longText);

      expect(mockSend).toHaveBeenCalledTimes(2);
      expect(mockSend).toHaveBeenNthCalledWith(1, 'x'.repeat(2000));
      expect(mockSend).toHaveBeenNthCalledWith(2, 'x'.repeat(1000));
    });
  });

  // --- setTyping ---

  describe('setTyping', () => {
    it('sends typing indicator when isTyping is true', async () => {
      await provider.connect();

      const mockSendTyping = vi.fn().mockResolvedValue(undefined);
      mockChannelFetch.mockResolvedValue({
        send: vi.fn(),
        sendTyping: mockSendTyping,
      });

      await provider.setTyping('dc:1234567890123456', true);
      expect(mockSendTyping).toHaveBeenCalled();
    });

    it('does nothing when isTyping is false', async () => {
      await provider.connect();

      await provider.setTyping('dc:1234567890123456', false);
      expect(mockChannelFetch).not.toHaveBeenCalled();
    });

    it('does nothing when client is not initialized', async () => {
      await provider.setTyping('dc:1234567890123456', true);
      // No error
    });
  });

  // --- listChats ---

  describe('listChats', () => {
    it('returns empty list initially', async () => {
      const chats = await provider.listChats();
      expect(chats).toEqual([]);
    });

    it('tracks chats from incoming messages', async () => {
      const onMessage = vi.fn();
      provider.onMessage = onMessage;
      await provider.connect();

      const msg = createMessage({
        guildName: 'My Server',
        channelName: 'general',
      });
      await triggerMessage(msg);

      const chats = await provider.listChats();
      expect(chats).toEqual([
        {
          id: 'dc:1234567890123456',
          name: 'My Server #general',
          is_group: true,
        },
      ]);
    });
  });

  // --- hasToken ---

  describe('hasToken', () => {
    it('returns true when DISCORD_BOT_TOKEN is set', () => {
      expect(provider.hasToken()).toBe(true);
    });
  });
});
