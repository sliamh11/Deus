/**
 * Standalone Telegram bot provider.
 * Extracted from Deus TelegramChannel — no Deus-specific dependencies.
 * Config comes from env vars; all messages are forwarded to onMessage.
 */

import https from 'https';

import { Api, Bot } from 'grammy';
import pino from 'pino';

import type {
  ChannelProvider,
  ChannelStatus,
  ChatInfo,
  IncomingMessage,
  IncomingReaction,
} from '@deus-ai/channel-core';

const ASSISTANT_NAME = process.env.ASSISTANT_NAME || 'Deus';
const BOT_TOKEN = process.env.TELEGRAM_BOT_TOKEN || '';

// Use stderr for logging (stdout is reserved for MCP JSON-RPC)
const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

const MAX_MESSAGE_LENGTH = 4096;
const MAX_CONSECUTIVE_ERRORS = 5;
const MAX_RECONNECT_RETRIES = 3;
const BASE_BACKOFF_MS = 1000;

/**
 * Send a message with Telegram Markdown parse mode, falling back to plain text.
 */
async function sendTelegramMessage(
  api: { sendMessage: Api['sendMessage'] },
  chatId: string | number,
  text: string,
  options: { message_thread_id?: number } = {},
): Promise<void> {
  try {
    await api.sendMessage(chatId, text, { ...options, parse_mode: 'Markdown' });
  } catch {
    await api.sendMessage(chatId, text, options);
  }
}

export class TelegramProvider implements ChannelProvider {
  readonly name = 'telegram';

  private bot: Bot | null = null;
  private connectTime = 0;
  private knownChats = new Map<string, { name: string; isGroup: boolean }>();
  private botUsername?: string;
  private consecutiveErrors = 0;
  private resetting = false;

  // Set by server-base.ts
  onMessage: (msg: IncomingMessage) => void = () => {};

  // Set by server-base.ts — called on Telegram message_reaction updates.
  onReaction?: (reaction: IncomingReaction) => void;

  async connect(): Promise<void> {
    if (!BOT_TOKEN) {
      throw new Error('TELEGRAM_BOT_TOKEN not set');
    }

    this.bot = new Bot(BOT_TOKEN, {
      client: {
        baseFetchConfig: { agent: https.globalAgent, compress: true },
      },
    });

    // /chatid command for registration
    this.bot.command('chatid', (ctx) => {
      const chatId = ctx.chat.id;
      const chatType = ctx.chat.type;
      const chatName =
        chatType === 'private'
          ? ctx.from?.first_name || 'Private'
          : (ctx.chat as any).title || 'Unknown';
      ctx
        .reply(
          `Chat ID: \`tg:${chatId}\`\nName: ${chatName}\nType: ${chatType}`,
          { parse_mode: 'Markdown' },
        )
        .catch((err: unknown) => {
          logger.error(
            { err, task: 'telegram.command.chatid.reply' },
            'floating-promise',
          );
        });
    });

    this.bot.command('ping', (ctx) => {
      ctx.reply(`${ASSISTANT_NAME} is online.`).catch((err: unknown) => {
        logger.error(
          { err, task: 'telegram.command.ping.reply' },
          'floating-promise',
        );
      });
    });

    const BOT_COMMANDS = new Set(['chatid', 'ping']);

    // Handle text messages
    this.bot.on('message:text', async (ctx) => {
      this.consecutiveErrors = 0;
      if (ctx.message.text.startsWith('/')) {
        const cmd = ctx.message.text.slice(1).split(/[\s@]/)[0].toLowerCase();
        if (BOT_COMMANDS.has(cmd)) return;
      }

      const chatJid = `tg:${ctx.chat.id}`;
      let content = ctx.message.text;
      const timestamp = new Date(ctx.message.date * 1000).toISOString();
      const senderName =
        ctx.from?.first_name ||
        ctx.from?.username ||
        ctx.from?.id.toString() ||
        'Unknown';
      const sender = ctx.from?.id.toString() || '';
      const msgId = ctx.message.message_id.toString();
      const isGroup =
        ctx.chat.type === 'group' || ctx.chat.type === 'supergroup';
      const chatName =
        ctx.chat.type === 'private'
          ? senderName
          : (ctx.chat as any).title || chatJid;

      // Track chat
      this.knownChats.set(chatJid, { name: chatName, isGroup });

      // Translate @bot mentions to @AssistantName
      const botUser = this.botUsername?.toLowerCase();
      if (botUser) {
        const entities = ctx.message.entities || [];
        const isBotMentioned = entities.some((e) => {
          if (e.type === 'mention') {
            return (
              content.substring(e.offset, e.offset + e.length).toLowerCase() ===
              `@${botUser}`
            );
          }
          return false;
        });
        if (isBotMentioned) {
          content = `@${ASSISTANT_NAME} ${content}`;
        }
      }

      // Reply context
      const replyTo = ctx.message.reply_to_message;

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
          thread_id: ctx.message.message_thread_id?.toString(),
          reply_to_message_id: replyTo?.message_id?.toString(),
          reply_to_content: replyTo?.text || replyTo?.caption,
          reply_to_sender_name: replyTo
            ? replyTo.from?.first_name ||
              replyTo.from?.username ||
              replyTo.from?.id?.toString()
            : undefined,
        },
      });
    });

    // Handle media messages
    const storeMedia = (ctx: any, placeholder: string) => {
      const chatJid = `tg:${ctx.chat.id}`;
      const timestamp = new Date(ctx.message.date * 1000).toISOString();
      const senderName =
        ctx.from?.first_name ||
        ctx.from?.username ||
        ctx.from?.id?.toString() ||
        'Unknown';
      const caption = ctx.message.caption ? ` ${ctx.message.caption}` : '';
      const isGroup =
        ctx.chat.type === 'group' || ctx.chat.type === 'supergroup';

      this.knownChats.set(chatJid, {
        name:
          ctx.chat.type === 'private'
            ? senderName
            : (ctx.chat as any).title || chatJid,
        isGroup,
      });

      this.onMessage({
        id: ctx.message.message_id.toString(),
        chat_id: chatJid,
        sender: ctx.from?.id?.toString() || '',
        sender_name: senderName,
        content: `${placeholder}${caption}`,
        timestamp,
        is_from_me: false,
        is_group: isGroup,
      });
    };

    this.bot.on('message:photo', (ctx) => storeMedia(ctx, '[Photo]'));
    this.bot.on('message:video', (ctx) => storeMedia(ctx, '[Video]'));
    this.bot.on('message:voice', (ctx) => storeMedia(ctx, '[Voice message]'));
    this.bot.on('message:audio', (ctx) => storeMedia(ctx, '[Audio]'));
    this.bot.on('message:document', (ctx) => {
      const name = ctx.message.document?.file_name || 'file';
      storeMedia(ctx, `[Document: ${name}]`);
    });
    this.bot.on('message:sticker', (ctx) => {
      const emoji = ctx.message.sticker?.emoji || '';
      storeMedia(ctx, `[Sticker ${emoji}]`);
    });
    this.bot.on('message:location', (ctx) => storeMedia(ctx, '[Location]'));
    this.bot.on('message:contact', (ctx) => storeMedia(ctx, '[Contact]'));

    // Reactions: Telegram delivers an `old_reaction` + `new_reaction` diff.
    // We emit one IncomingReaction per emoji added. Removal = empty emoji string.
    this.bot.on('message_reaction', (ctx) => {
      if (!this.onReaction) return;
      try {
        const upd = ctx.messageReaction;
        if (!upd) return;
        const chatId = `tg:${upd.chat.id}`;
        const isGroup =
          upd.chat.type === 'group' || upd.chat.type === 'supergroup';
        const chatName = (upd.chat as any).title || undefined;
        const senderId =
          upd.user?.id != null
            ? String(upd.user.id)
            : `actor:${upd.actor_chat?.id ?? 'unknown'}`;
        const senderName = upd.user
          ? [upd.user.first_name, upd.user.last_name].filter(Boolean).join(' ')
          : senderId;
        const reactedTo = String(upd.message_id);
        const timestamp = new Date(upd.date * 1000).toISOString();

        const oldSet = new Set(
          (upd.old_reaction || [])
            .filter((r) => r.type === 'emoji')
            .map((r) => (r as { type: 'emoji'; emoji: string }).emoji),
        );
        const newList = (upd.new_reaction || [])
          .filter((r) => r.type === 'emoji')
          .map((r) => (r as { type: 'emoji'; emoji: string }).emoji);

        const added = newList.filter((e) => !oldSet.has(e));

        if (added.length === 0) {
          // Pure removal — emit one empty-emoji event (host treats as no-op).
          this.onReaction({
            chat_id: chatId,
            sender: senderId,
            sender_name: senderName,
            reacted_to_message_id: reactedTo,
            emoji: '',
            timestamp,
            is_group: isGroup,
            chat_name: chatName,
          });
          return;
        }

        for (const emoji of added) {
          this.onReaction({
            chat_id: chatId,
            sender: senderId,
            sender_name: senderName,
            reacted_to_message_id: reactedTo,
            emoji,
            timestamp,
            is_group: isGroup,
            chat_name: chatName,
          });
        }
      } catch (err) {
        logger.warn({ err }, 'Failed to handle Telegram reaction');
      }
    });

    this.bot.catch((err) => {
      this.consecutiveErrors++;
      logger.error(
        { err: err.message, consecutiveErrors: this.consecutiveErrors },
        'Telegram bot error',
      );

      if (this.consecutiveErrors >= MAX_CONSECUTIVE_ERRORS && !this.resetting) {
        this.resetPolling().catch((err: unknown) => {
          logger.error(
            { err, task: 'telegram.error-handler.resetPolling' },
            'floating-promise',
          );
        });
      }
    });

    return new Promise<void>((resolve) => {
      this.bot!.start({
        // message_reaction is not in the default allowed_updates set — opt in.
        allowed_updates: ['message', 'edited_message', 'message_reaction'],
        onStart: (botInfo) => {
          this.botUsername = botInfo.username;
          this.connectTime = Date.now();
          this.consecutiveErrors = 0;
          logger.info(
            { username: botInfo.username, id: botInfo.id },
            'Telegram bot connected',
          );
          resolve();
        },
      });
    });
  }

  /**
   * Reset the polling session after consecutive errors.
   * Retries with exponential backoff (1s, 2s, 4s), then exits on failure.
   */
  private async resetPolling(): Promise<void> {
    if (!this.bot || this.resetting) return;
    this.resetting = true;
    logger.warn(
      { consecutiveErrors: this.consecutiveErrors },
      'Too many consecutive errors, resetting Telegram polling session',
    );

    const bot = this.bot;
    bot.stop().catch((err: unknown) => {
      logger.error(
        { err, task: 'telegram.resetPolling.stop.pre-retry' },
        'floating-promise',
      );
    });
    this.consecutiveErrors = 0;

    for (let attempt = 0; attempt < MAX_RECONNECT_RETRIES; attempt++) {
      const delayMs = BASE_BACKOFF_MS * Math.pow(2, attempt);
      logger.info(
        { attempt: attempt + 1, delayMs },
        'Attempting polling reset with backoff',
      );

      await new Promise((r) => setTimeout(r, delayMs));

      try {
        await new Promise<void>((resolve, reject) => {
          const timeout = setTimeout(() => {
            reject(new Error('Polling restart timed out'));
          }, 30_000);

          bot.start({
            onStart: (botInfo) => {
              clearTimeout(timeout);
              this.botUsername = botInfo.username;
              this.connectTime = Date.now();
              this.resetting = false;
              this.consecutiveErrors = 0;
              logger.info(
                { username: botInfo.username, id: botInfo.id },
                'Telegram bot reconnected after polling reset',
              );
              resolve();
            },
          });
        });
        return; // Success — exit retry loop
      } catch (err) {
        logger.error(
          { attempt: attempt + 1, err },
          'Polling reset attempt failed',
        );
        bot.stop().catch((stopErr: unknown) => {
          logger.error(
            { err: stopErr, task: 'telegram.resetPolling.stop.post-error' },
            'floating-promise',
          );
        });
      }
    }

    logger.fatal(
      'Telegram bot failed to reconnect after %d retries — exiting',
      MAX_RECONNECT_RETRIES,
    );
    process.exit(1);
  }

  async sendMessage(chatId: string, text: string): Promise<void> {
    if (!this.bot) return;
    try {
      const numericId = chatId.replace(/^tg:/, '');
      if (text.length <= MAX_MESSAGE_LENGTH) {
        await sendTelegramMessage(this.bot.api, numericId, text);
      } else {
        for (let i = 0; i < text.length; i += MAX_MESSAGE_LENGTH) {
          await sendTelegramMessage(
            this.bot.api,
            numericId,
            text.slice(i, i + MAX_MESSAGE_LENGTH),
          );
        }
      }
    } catch (err) {
      logger.error({ chatId, err }, 'Failed to send Telegram message');
    }
  }

  isConnected(): boolean {
    return this.bot !== null;
  }

  getStatus(): ChannelStatus {
    return {
      connected: this.bot !== null,
      channel: 'telegram',
      identity: this.botUsername,
      uptime_seconds: this.connectTime
        ? Math.floor((Date.now() - this.connectTime) / 1000)
        : 0,
    };
  }

  async disconnect(): Promise<void> {
    if (this.bot) {
      this.resetting = false;
      this.consecutiveErrors = 0;
      this.bot.stop().catch((err: unknown) => {
        logger.error(
          { err, task: 'telegram.disconnect.stop' },
          'floating-promise',
        );
      });
      this.bot = null;
      logger.info('Telegram bot stopped');
    }
  }

  async setTyping(chatId: string, isTyping: boolean): Promise<void> {
    if (!this.bot || !isTyping) return;
    try {
      const numericId = chatId.replace(/^tg:/, '');
      await this.bot.api.sendChatAction(numericId, 'typing');
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
