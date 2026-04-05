/**
 * Standalone Slack bot provider.
 * Adapted from Deus SlackChannel — no Deus-specific dependencies.
 * Config comes from env vars; all messages are forwarded to onMessage.
 *
 * Uses Socket Mode (requires both SLACK_BOT_TOKEN and SLACK_APP_TOKEN).
 */

import { App, LogLevel } from '@slack/bolt';

import pino from 'pino';

import type {
  ChannelProvider,
  ChannelStatus,
  ChatInfo,
  IncomingMessage,
} from '@deus-ai/channel-core';

// Read env vars lazily so tests can set them before connect()
function getAssistantName(): string {
  return process.env.ASSISTANT_NAME || 'Deus';
}
function getBotToken(): string {
  return process.env.SLACK_BOT_TOKEN || '';
}
function getAppToken(): string {
  return process.env.SLACK_APP_TOKEN || '';
}

// Use stderr for logging (stdout is reserved for MCP JSON-RPC)
const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

// Slack's chat.postMessage API limits text to ~4000 characters per call.
const MAX_MESSAGE_LENGTH = 4000;

export class SlackProvider implements ChannelProvider {
  readonly name = 'slack';

  private app: App | null = null;
  private botUserId: string | undefined;
  private connected = false;
  private connectTime = 0;
  private knownChats = new Map<string, { name: string; isGroup: boolean }>();
  private userNameCache = new Map<string, string>();
  private outgoingQueue: Array<{ chatId: string; text: string }> = [];
  private flushing = false;

  // Set by server-base.ts
  onMessage: (msg: IncomingMessage) => void = () => {};

  async connect(): Promise<void> {
    if (!getBotToken() || !getAppToken()) {
      throw new Error('SLACK_BOT_TOKEN and SLACK_APP_TOKEN must be set');
    }

    this.app = new App({
      token: getBotToken(),
      appToken: getAppToken(),
      socketMode: true,
      logLevel: LogLevel.ERROR,
    });

    this.setupEventHandlers();

    await this.app.start();

    // Get bot's own user ID for self-message detection
    try {
      const auth = await this.app.client.auth.test();
      this.botUserId = auth.user_id as string;
      logger.info({ botUserId: this.botUserId }, 'Connected to Slack');
    } catch (err) {
      logger.warn({ err }, 'Connected to Slack but failed to get bot user ID');
    }

    this.connectTime = Date.now();
    this.connected = true;

    // Flush any messages queued before connection
    await this.flushOutgoingQueue();

    // Sync channel metadata on startup
    await this.syncChannelMetadata();
  }

  private setupEventHandlers(): void {
    if (!this.app) return;

    this.app.event('message', async ({ event }) => {
      const subtype = (event as { subtype?: string }).subtype;
      if (subtype && subtype !== 'bot_message') return;

      const msg = event as {
        channel: string;
        channel_type?: string;
        user?: string;
        text?: string;
        ts: string;
        thread_ts?: string;
        bot_id?: string;
      };

      if (!msg.text) return;

      const chatId = `slack:${msg.channel}`;
      const timestamp = new Date(parseFloat(msg.ts) * 1000).toISOString();
      const isGroup = msg.channel_type !== 'im';

      const isBotMessage = !!msg.bot_id || msg.user === this.botUserId;

      let senderName: string;
      if (isBotMessage) {
        senderName = getAssistantName();
      } else {
        senderName =
          (msg.user ? await this.resolveUserName(msg.user) : undefined) ||
          msg.user ||
          'unknown';
      }

      // Track chat
      this.knownChats.set(chatId, { name: senderName, isGroup });

      // Translate Slack <@UBOTID> mentions into @AssistantName format
      let content = msg.text;
      if (this.botUserId && !isBotMessage) {
        const mentionPattern = `<@${this.botUserId}>`;
        if (content.includes(mentionPattern)) {
          content = `@${getAssistantName()} ${content}`;
        }
      }

      this.onMessage({
        id: msg.ts,
        chat_id: chatId,
        sender: msg.user || msg.bot_id || '',
        sender_name: senderName,
        content,
        timestamp,
        is_from_me: isBotMessage,
        is_group: isGroup,
        metadata: {
          thread_ts: msg.thread_ts,
          is_bot_message: isBotMessage,
        },
      });
    });
  }

  async sendMessage(chatId: string, text: string): Promise<void> {
    const channelId = chatId.replace(/^slack:/, '');

    if (!this.connected || !this.app) {
      this.outgoingQueue.push({ chatId, text });
      logger.info(
        { chatId, queueSize: this.outgoingQueue.length },
        'Slack disconnected, message queued',
      );
      return;
    }

    try {
      if (text.length <= MAX_MESSAGE_LENGTH) {
        await this.app.client.chat.postMessage({ channel: channelId, text });
      } else {
        for (let i = 0; i < text.length; i += MAX_MESSAGE_LENGTH) {
          await this.app.client.chat.postMessage({
            channel: channelId,
            text: text.slice(i, i + MAX_MESSAGE_LENGTH),
          });
        }
      }
      logger.info({ chatId, length: text.length }, 'Slack message sent');
    } catch (err) {
      this.outgoingQueue.push({ chatId, text });
      logger.warn(
        { chatId, err, queueSize: this.outgoingQueue.length },
        'Failed to send Slack message, queued',
      );
    }
  }

  isConnected(): boolean {
    return this.connected;
  }

  getStatus(): ChannelStatus {
    return {
      connected: this.connected,
      channel: 'slack',
      identity: this.botUserId,
      uptime_seconds: this.connectTime
        ? Math.floor((Date.now() - this.connectTime) / 1000)
        : 0,
    };
  }

  async disconnect(): Promise<void> {
    this.connected = false;
    if (this.app) {
      await this.app.stop();
      this.app = null;
      logger.info('Slack bot stopped');
    }
  }

  // Slack Bot API has no typing indicator endpoint — no-op
  async setTyping(_chatId: string, _isTyping: boolean): Promise<void> {
    // no-op
  }

  async listChats(): Promise<ChatInfo[]> {
    return Array.from(this.knownChats.entries()).map(([id, info]) => ({
      id,
      name: info.name,
      is_group: info.isGroup,
    }));
  }

  /**
   * Sync channel metadata from Slack.
   * Fetches channels the bot is a member of and records them in knownChats.
   */
  async syncGroups(): Promise<ChatInfo[]> {
    await this.syncChannelMetadata();
    return this.listChats();
  }

  /** Check if both tokens are configured. */
  hasTokens(): boolean {
    return !!getBotToken() && !!getAppToken();
  }

  // ── Private helpers ──────────────────────────────────────────────────

  private async syncChannelMetadata(): Promise<void> {
    if (!this.app) return;

    try {
      logger.info('Syncing channel metadata from Slack...');
      let cursor: string | undefined;
      let count = 0;

      do {
        const result = await this.app.client.conversations.list({
          types: 'public_channel,private_channel',
          exclude_archived: true,
          limit: 200,
          cursor,
        });

        for (const ch of result.channels || []) {
          if (ch.id && ch.name && ch.is_member) {
            this.knownChats.set(`slack:${ch.id}`, {
              name: ch.name,
              isGroup: true,
            });
            count++;
          }
        }

        cursor = result.response_metadata?.next_cursor || undefined;
      } while (cursor);

      logger.info({ count }, 'Slack channel metadata synced');
    } catch (err) {
      logger.error({ err }, 'Failed to sync Slack channel metadata');
    }
  }

  private async resolveUserName(userId: string): Promise<string | undefined> {
    if (!userId || !this.app) return undefined;

    const cached = this.userNameCache.get(userId);
    if (cached) return cached;

    try {
      const result = await this.app.client.users.info({ user: userId });
      const name = result.user?.real_name || result.user?.name;
      if (name) this.userNameCache.set(userId, name);
      return name;
    } catch (err) {
      logger.debug({ userId, err }, 'Failed to resolve Slack user name');
      return undefined;
    }
  }

  private async flushOutgoingQueue(): Promise<void> {
    if (this.flushing || this.outgoingQueue.length === 0) return;
    this.flushing = true;
    try {
      logger.info(
        { count: this.outgoingQueue.length },
        'Flushing Slack outgoing queue',
      );
      while (this.outgoingQueue.length > 0) {
        const item = this.outgoingQueue.shift()!;
        const channelId = item.chatId.replace(/^slack:/, '');
        await this.app!.client.chat.postMessage({
          channel: channelId,
          text: item.text,
        });
        logger.info(
          { chatId: item.chatId, length: item.text.length },
          'Queued Slack message sent',
        );
      }
    } finally {
      this.flushing = false;
    }
  }
}
