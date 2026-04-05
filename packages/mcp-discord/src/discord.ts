/**
 * Standalone Discord bot provider.
 * Adapted from the Deus DiscordChannel — no Deus-specific dependencies.
 * Config comes from env vars; all messages are forwarded to onMessage.
 */

import {
  Client,
  Events,
  GatewayIntentBits,
  Message,
  TextChannel,
} from 'discord.js';
import pino from 'pino';

import type {
  ChannelProvider,
  ChannelStatus,
  ChatInfo,
  IncomingMessage,
} from '@deus-ai/channel-core';

const ASSISTANT_NAME = process.env.ASSISTANT_NAME || 'Deus';
const BOT_TOKEN = process.env.DISCORD_BOT_TOKEN || '';

// Use stderr for logging (stdout is reserved for MCP JSON-RPC)
const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

const MAX_MESSAGE_LENGTH = 2000;

export class DiscordProvider implements ChannelProvider {
  readonly name = 'discord';

  private client: Client | null = null;
  private connectTime = 0;
  private knownChats = new Map<string, { name: string; isGroup: boolean }>();
  private botUserId?: string;
  private botTag?: string;

  // Set by channel-core registerCommonTools
  onMessage: (msg: IncomingMessage) => void = () => {};

  async connect(): Promise<void> {
    if (!BOT_TOKEN) {
      throw new Error('DISCORD_BOT_TOKEN not set');
    }

    this.client = new Client({
      intents: [
        GatewayIntentBits.Guilds,
        GatewayIntentBits.GuildMessages,
        GatewayIntentBits.MessageContent,
        GatewayIntentBits.DirectMessages,
      ],
    });

    this.client.on(Events.MessageCreate, async (message: Message) => {
      // Ignore bot messages (including own)
      if (message.author.bot) return;

      const channelId = message.channelId;
      const chatJid = `dc:${channelId}`;
      let content = message.content;
      const timestamp = message.createdAt.toISOString();
      const senderName =
        message.member?.displayName ||
        message.author.displayName ||
        message.author.username;
      const sender = message.author.id;
      const msgId = message.id;

      // Determine chat name
      let chatName: string;
      const isGroup = message.guild !== null;
      if (message.guild) {
        const textChannel = message.channel as TextChannel;
        chatName = `${message.guild.name} #${textChannel.name}`;
      } else {
        chatName = senderName;
      }

      // Track chat
      this.knownChats.set(chatJid, { name: chatName, isGroup });

      // Translate Discord @bot mentions into @AssistantName format.
      // Discord mentions look like <@botUserId>.
      if (this.botUserId) {
        const botId = this.botUserId;
        const isBotMentioned =
          message.mentions.users.has(botId) ||
          content.includes(`<@${botId}>`) ||
          content.includes(`<@!${botId}>`);

        if (isBotMentioned) {
          // Strip the <@botId> mention to avoid visual clutter
          content = content
            .replace(new RegExp(`<@!?${botId}>`, 'g'), '')
            .trim();
          // Prepend @AssistantName
          content = `@${ASSISTANT_NAME} ${content}`;
        }
      }

      // Handle attachments — store placeholders
      if (message.attachments.size > 0) {
        const attachmentDescriptions = [...message.attachments.values()].map(
          (att) => {
            const contentType = att.contentType || '';
            if (contentType.startsWith('image/')) {
              return `[Image: ${att.name || 'image'}]`;
            } else if (contentType.startsWith('video/')) {
              return `[Video: ${att.name || 'video'}]`;
            } else if (contentType.startsWith('audio/')) {
              return `[Audio: ${att.name || 'audio'}]`;
            } else {
              return `[File: ${att.name || 'file'}]`;
            }
          },
        );
        if (content) {
          content = `${content}\n${attachmentDescriptions.join('\n')}`;
        } else {
          content = attachmentDescriptions.join('\n');
        }
      }

      // Handle reply context
      let replyToMessageId: string | undefined;
      let replyToContent: string | undefined;
      let replyToSenderName: string | undefined;

      if (message.reference?.messageId) {
        try {
          const repliedTo = await message.channel.messages.fetch(
            message.reference.messageId,
          );
          replyToMessageId = repliedTo.id;
          replyToContent = repliedTo.content || undefined;
          replyToSenderName =
            repliedTo.member?.displayName ||
            repliedTo.author.displayName ||
            repliedTo.author.username;
        } catch {
          // Referenced message may have been deleted
        }
      }

      // Forward ALL messages
      this.onMessage({
        id: msgId,
        chat_id: chatJid,
        sender,
        sender_name: senderName,
        content,
        timestamp,
        is_from_me: false,
        is_group: isGroup,
        chat_name: chatName,
        metadata: {
          reply_to_message_id: replyToMessageId,
          reply_to_content: replyToContent,
          reply_to_sender_name: replyToSenderName,
        },
      });
    });

    // Handle errors gracefully
    this.client.on(Events.Error, (err) => {
      logger.error({ err: err.message }, 'Discord client error');
    });

    return new Promise<void>((resolve) => {
      this.client!.once(Events.ClientReady, (readyClient) => {
        this.botUserId = readyClient.user.id;
        this.botTag = readyClient.user.tag;
        this.connectTime = Date.now();
        logger.info(
          { username: readyClient.user.tag, id: readyClient.user.id },
          'Discord bot connected',
        );
        resolve();
      });

      this.client!.login(BOT_TOKEN);
    });
  }

  async sendMessage(chatId: string, text: string): Promise<void> {
    if (!this.client) return;
    try {
      const channelId = chatId.replace(/^dc:/, '');
      const channel = await this.client.channels.fetch(channelId);

      if (!channel || !('send' in channel)) {
        logger.warn({ chatId }, 'Discord channel not found or not text-based');
        return;
      }

      const textChannel = channel as TextChannel;

      if (text.length <= MAX_MESSAGE_LENGTH) {
        await textChannel.send(text);
      } else {
        for (let i = 0; i < text.length; i += MAX_MESSAGE_LENGTH) {
          await textChannel.send(text.slice(i, i + MAX_MESSAGE_LENGTH));
        }
      }
    } catch (err) {
      logger.error({ chatId, err }, 'Failed to send Discord message');
    }
  }

  isConnected(): boolean {
    return this.client !== null && this.client.isReady();
  }

  getStatus(): ChannelStatus {
    return {
      connected: this.client !== null && this.client.isReady(),
      channel: 'discord',
      identity: this.botTag,
      uptime_seconds: this.connectTime
        ? Math.floor((Date.now() - this.connectTime) / 1000)
        : 0,
    };
  }

  async disconnect(): Promise<void> {
    if (this.client) {
      this.client.destroy();
      this.client = null;
      logger.info('Discord bot stopped');
    }
  }

  async setTyping(chatId: string, isTyping: boolean): Promise<void> {
    if (!this.client || !isTyping) return;
    try {
      const channelId = chatId.replace(/^dc:/, '');
      const channel = await this.client.channels.fetch(channelId);
      if (channel && 'sendTyping' in channel) {
        await (channel as TextChannel).sendTyping();
      }
    } catch {
      // Best effort
    }
  }

  async listChats(): Promise<ChatInfo[]> {
    return Array.from(this.knownChats.entries()).map(([id, info]) => ({
      id,
      name: info.name,
      is_group: info.isGroup,
    }));
  }

  /** Check if a bot token is configured. */
  hasToken(): boolean {
    return !!BOT_TOKEN;
  }
}
