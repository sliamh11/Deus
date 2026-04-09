/**
 * Standalone WhatsApp connection provider.
 * Extracted from Deus WhatsAppChannel — no Deus-specific dependencies.
 * All config comes from env vars; all messages are forwarded to onMessage.
 */

import fs from 'fs';
import path from 'path';

import {
  makeWASocket,
  Browsers,
  DisconnectReason,
  fetchLatestWaWebVersion,
  makeCacheableSignalKeyStore,
  normalizeMessageContent,
  useMultiFileAuthState,
} from '@whiskeysockets/baileys';
import type {
  GroupMetadata,
  WAMessageKey,
  WASocket,
  proto as ProtoTypes,
} from '@whiskeysockets/baileys';
import { createRequire } from 'module';
const { proto } = createRequire(import.meta.url)('@whiskeysockets/baileys') as {
  proto: typeof ProtoTypes;
};
import pino from 'pino';

import type {
  ChannelProvider,
  ChannelStatus,
  ChatInfo,
  IncomingMessage,
} from '@deus-ai/channel-core';

// ── Config from env vars ────────────────────────────────────────────────

const AUTH_DIR =
  process.env.WHATSAPP_AUTH_DIR || path.resolve(process.cwd(), 'store', 'auth');
const ASSISTANT_NAME = process.env.ASSISTANT_NAME || 'Deus';
const ASSISTANT_HAS_OWN_NUMBER =
  process.env.ASSISTANT_HAS_OWN_NUMBER === 'true';

const CHATS_FILE = path.join(AUTH_DIR, 'chats.json');

// Use stderr for logging (stdout is reserved for MCP JSON-RPC)
const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);
const baileysLogger = pino({ level: 'silent' });

export class WhatsAppProvider implements ChannelProvider {
  readonly name = 'whatsapp';

  private sock!: WASocket;
  private connected = false;
  private connectTime = 0;
  private lidToPhoneMap: Record<string, string> = {};
  private outgoingQueue: Array<{ jid: string; text: string }> = [];
  private flushing = false;
  private sentMessageCache = new Map<string, ProtoTypes.IMessage>();
  private groupMetadataCache = new Map<
    string,
    { metadata: GroupMetadata; expiresAt: number }
  >();
  private botLidUser?: string;
  private pendingFirstOpen?: () => void;
  private knownChats = this.loadChats();
  private saveChatsTimer: ReturnType<typeof setTimeout> | null = null;
  private readyPromise: Promise<void> | null = null;
  private readyResolve: (() => void) | null = null;

  // Set by server-base.ts — called for every incoming message
  onMessage: (msg: IncomingMessage) => void = () => {};

  async connect(): Promise<void> {
    this.readyPromise = new Promise<void>((resolve) => {
      this.readyResolve = resolve;
    });
    return new Promise<void>((resolve, reject) => {
      this.pendingFirstOpen = resolve;
      this.connectInternal().catch(reject);
    });
  }

  async waitForReady(): Promise<void> {
    if (this.connected) return;
    if (this.readyPromise) await this.readyPromise;
  }

  private async connectInternal(): Promise<void> {
    fs.mkdirSync(AUTH_DIR, { recursive: true });

    const { state, saveCreds } = await useMultiFileAuthState(AUTH_DIR);

    const { version } = await fetchLatestWaWebVersion({}).catch((err) => {
      logger.warn(
        { err },
        'Failed to fetch latest WA Web version, using default',
      );
      return { version: undefined };
    });

    this.sock = makeWASocket({
      version,
      auth: {
        creds: state.creds,
        keys: makeCacheableSignalKeyStore(state.keys, baileysLogger),
      },
      printQRInTerminal: false,
      logger: baileysLogger,
      browser:
        process.platform === 'win32'
          ? Browsers.windows('Chrome')
          : Browsers.macOS('Chrome'),
      cachedGroupMetadata: async (jid: string) =>
        this.getNormalizedGroupMetadata(jid),
      getMessage: async (key: WAMessageKey) => {
        const cached = this.sentMessageCache.get(key.id || '');
        if (cached) return cached;
        return proto.Message.fromObject({});
      },
    });

    this.sock.ev.on('connection.update', (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (qr) {
        // Write QR data to a file for auth tools to pick up
        const qrPath = path.join(path.dirname(AUTH_DIR), 'qr-data.txt');
        fs.writeFileSync(qrPath, qr);
        logger.warn('WhatsApp authentication required — use start_auth tool');
      }

      if (connection === 'close') {
        this.connected = false;
        const reason = (
          lastDisconnect?.error as { output?: { statusCode?: number } }
        )?.output?.statusCode;
        const shouldReconnect = reason !== DisconnectReason.loggedOut;
        logger.info({ reason, shouldReconnect }, 'Connection closed');

        if (shouldReconnect) {
          logger.info('Reconnecting...');
          this.connectInternal().catch((err) => {
            logger.error({ err }, 'Failed to reconnect, retrying in 5s');
            setTimeout(() => {
              this.connectInternal().catch((err2) => {
                logger.error({ err: err2 }, 'Reconnection retry failed');
              });
            }, 5000);
          });
        } else {
          logger.info('Logged out. Re-authenticate to continue.');
        }
      } else if (connection === 'open') {
        this.connected = true;
        this.connectTime = Date.now();
        logger.info('Connected to WhatsApp');

        this.sock.sendPresenceUpdate('available').catch(() => {});

        // Build LID to phone mapping
        if (this.sock.user) {
          const phoneUser = this.sock.user.id.split(':')[0];
          const lidUser = this.sock.user.lid?.split(':')[0];
          if (lidUser && phoneUser) {
            this.lidToPhoneMap[lidUser] = `${phoneUser}@s.whatsapp.net`;
            this.botLidUser = lidUser;
          }
        }

        this.flushOutgoingQueue().catch(() => {});

        // Seed knownChats on first-ever boot only.
        // On reconnects, Baileys handles group re-sync internally via the
        // CB:ib,,dirty WebSocket handler — no manual fetch needed.
        if (this.knownChats.size === 0) {
          this.seedGroupsFromBaileys().catch(() => {});
        }

        if (this.pendingFirstOpen) {
          this.pendingFirstOpen();
          this.pendingFirstOpen = undefined;
        }
        if (this.readyResolve) {
          this.readyResolve();
          this.readyResolve = null;
        }
      }
    });

    this.sock.ev.on('creds.update', saveCreds);

    // ── Incremental group updates from Baileys events ─────────────────
    // Baileys fires these after its internal CB:ib,,dirty sync and in real time.

    this.sock.ev.on('groups.upsert', (groups) => {
      for (const g of groups) {
        if (g.id && g.subject) {
          this.knownChats.set(g.id, { name: g.subject, isGroup: true });
        }
      }
      this.scheduleSaveChats();
    });

    this.sock.ev.on('groups.update', (updates) => {
      for (const u of updates) {
        if (!u.id) continue;
        const existing = this.knownChats.get(u.id);
        if (u.subject) {
          this.knownChats.set(u.id, {
            name: u.subject,
            isGroup: true,
          });
        } else if (!existing) {
          // Group exists but we don't have metadata yet — mark as known
          this.knownChats.set(u.id, {
            name: u.id.split('@')[0],
            isGroup: true,
          });
        }
      }
      this.scheduleSaveChats();
    });

    this.sock.ev.on('chats.upsert', (chats) => {
      for (const chat of chats) {
        if (!chat.id || chat.id === 'status@broadcast') continue;
        if (!this.knownChats.has(chat.id)) {
          const isGroup = chat.id.endsWith('@g.us');
          this.knownChats.set(chat.id, {
            name: chat.name || chat.id.split('@')[0],
            isGroup,
          });
        }
      }
      this.scheduleSaveChats();
    });

    this.sock.ev.on('chats.delete', (deletedIds) => {
      for (const id of deletedIds) {
        this.knownChats.delete(id);
        this.groupMetadataCache.delete(id);
      }
      this.scheduleSaveChats();
    });

    this.sock.ev.on('group-participants.update', ({ id }) => {
      // Invalidate cached metadata so next message fetch gets fresh participants
      this.groupMetadataCache.delete(id);
    });

    // Phone number share event — not typed in all baileys versions
    (this.sock.ev as any).on(
      'chats.phoneNumberShare',
      (data: { lid?: string; jid?: string }) => {
        const lidUser = data.lid?.split('@')[0].split(':')[0];
        if (lidUser && data.jid) {
          this.lidToPhoneMap[lidUser] = data.jid;
          this.groupMetadataCache.clear();
        }
      },
    );

    this.sock.ev.on('messages.upsert', async ({ messages }) => {
      for (const msg of messages) {
        try {
          if (!msg.message) continue;
          const normalized = normalizeMessageContent(msg.message);
          if (!normalized) continue;
          const rawJid = msg.key.remoteJid;
          if (!rawJid || rawJid === 'status@broadcast') continue;

          let chatJid = await this.translateJid(rawJid);
          if (chatJid.endsWith('@lid') && (msg.key as any).senderPn) {
            const pn = (msg.key as any).senderPn as string;
            const phoneJid = pn.includes('@') ? pn : `${pn}@s.whatsapp.net`;
            this.lidToPhoneMap[rawJid.split('@')[0].split(':')[0]] = phoneJid;
            chatJid = phoneJid;
          }

          const timestamp = new Date(
            Number(msg.messageTimestamp) * 1000,
          ).toISOString();
          const isGroup = chatJid.endsWith('@g.us');

          // Track chat metadata
          this.knownChats.set(chatJid, {
            name: msg.pushName || chatJid.split('@')[0],
            isGroup,
          });

          let content =
            normalized.conversation ||
            normalized.extendedTextMessage?.text ||
            normalized.imageMessage?.caption ||
            normalized.videoMessage?.caption ||
            '';

          // Normalize bot LID mentions to assistant name
          if (this.botLidUser && content.includes(`@${this.botLidUser}`)) {
            content = content.replace(
              `@${this.botLidUser}`,
              `@${ASSISTANT_NAME}`,
            );
          }

          if (!content) continue;

          const sender = msg.key.participant || msg.key.remoteJid || '';
          const senderName = msg.pushName || sender.split('@')[0];
          const fromMe = msg.key.fromMe || false;
          const isBotMessage = ASSISTANT_HAS_OWN_NUMBER
            ? fromMe
            : content.startsWith(`${ASSISTANT_NAME}:`);

          // Forward ALL messages — the host decides which to act on
          this.onMessage({
            id: msg.key.id || '',
            chat_id: chatJid,
            sender,
            sender_name: senderName,
            content,
            timestamp,
            is_from_me: fromMe,
            is_group: isGroup,
            metadata: { is_bot_message: isBotMessage },
          });
        } catch (err) {
          logger.error(
            { err, remoteJid: msg.key?.remoteJid },
            'Error processing message',
          );
        }
      }
    });
  }

  async sendMessage(chatId: string, text: string): Promise<void> {
    const prefixed = ASSISTANT_HAS_OWN_NUMBER
      ? text
      : `${ASSISTANT_NAME}: ${text}`;

    if (!this.connected) {
      this.outgoingQueue.push({ jid: chatId, text: prefixed });
      return;
    }
    try {
      const sent = await this.sock.sendMessage(chatId, { text: prefixed });
      if (sent?.key?.id && sent.message) {
        this.sentMessageCache.set(sent.key.id, sent.message);
        if (this.sentMessageCache.size > 256) {
          const oldest = this.sentMessageCache.keys().next().value!;
          this.sentMessageCache.delete(oldest);
        }
      }
    } catch (err) {
      this.outgoingQueue.push({ jid: chatId, text: prefixed });
      logger.warn({ chatId, err }, 'Failed to send, message queued');
    }
  }

  isConnected(): boolean {
    return this.connected;
  }

  getStatus(): ChannelStatus {
    return {
      connected: this.connected,
      channel: 'whatsapp',
      identity: this.sock?.user?.id?.split(':')[0],
      uptime_seconds: this.connected
        ? Math.floor((Date.now() - this.connectTime) / 1000)
        : 0,
    };
  }

  async disconnect(): Promise<void> {
    this.connected = false;
    this.sock?.end(undefined);
  }

  async setTyping(chatId: string, isTyping: boolean): Promise<void> {
    try {
      await this.sock.sendPresenceUpdate(
        isTyping ? 'composing' : 'paused',
        chatId,
      );
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

  async syncGroups(): Promise<ChatInfo[]> {
    return this.seedGroupsFromBaileys();
  }

  /** Check if WhatsApp credentials exist on disk. */
  hasAuth(): boolean {
    return fs.existsSync(path.join(AUTH_DIR, 'creds.json'));
  }

  // ── Internal helpers ──────────────────────────────────────────────────

  /** Full group fetch — first boot or manual sync_groups tool only. */
  private async seedGroupsFromBaileys(): Promise<ChatInfo[]> {
    try {
      const groups = await this.sock.groupFetchAllParticipating();
      const result: ChatInfo[] = [];
      for (const [jid, metadata] of Object.entries(groups)) {
        if (metadata.subject) {
          this.knownChats.set(jid, { name: metadata.subject, isGroup: true });
          result.push({ id: jid, name: metadata.subject, is_group: true });
        }
      }
      this.scheduleSaveChats();
      logger.info({ count: result.length }, 'Group metadata seeded');
      return result;
    } catch (err) {
      logger.error({ err }, 'Failed to seed group metadata');
      return [];
    }
  }

  private loadChats(): Map<string, { name: string; isGroup: boolean }> {
    try {
      const raw = fs.readFileSync(CHATS_FILE, 'utf8');
      const arr = JSON.parse(raw) as Array<{
        id: string;
        name: string;
        isGroup: boolean;
      }>;
      return new Map(
        arr.map((c) => [c.id, { name: c.name, isGroup: c.isGroup }]),
      );
    } catch {
      return new Map();
    }
  }

  private scheduleSaveChats(): void {
    if (this.saveChatsTimer) clearTimeout(this.saveChatsTimer);
    this.saveChatsTimer = setTimeout(() => {
      try {
        const arr = Array.from(this.knownChats.entries()).map(([id, info]) => ({
          id,
          name: info.name,
          isGroup: info.isGroup,
        }));
        fs.writeFileSync(CHATS_FILE, JSON.stringify(arr));
      } catch (err) {
        logger.warn({ err }, 'Failed to persist chats');
      }
    }, 500);
  }

  private async translateJid(jid: string): Promise<string> {
    if (!jid.endsWith('@lid')) return jid;
    const lidUser = jid.split('@')[0].split(':')[0];
    const cached = this.lidToPhoneMap[lidUser];
    if (cached) return cached;

    try {
      const pn = await (
        this.sock.signalRepository as any
      )?.lidMapping?.getPNForLID(jid);
      if (pn) {
        const phoneJid = `${pn.split('@')[0].split(':')[0]}@s.whatsapp.net`;
        this.lidToPhoneMap[lidUser] = phoneJid;
        return phoneJid;
      }
    } catch {
      // Best effort
    }
    return jid;
  }

  private async getNormalizedGroupMetadata(
    jid: string,
  ): Promise<GroupMetadata | undefined> {
    if (!jid.endsWith('@g.us')) return undefined;
    const cached = this.groupMetadataCache.get(jid);
    if (cached && cached.expiresAt > Date.now()) return cached.metadata;

    const metadata = await this.sock.groupMetadata(jid);
    const participants = await Promise.all(
      metadata.participants.map(async (p) => ({
        ...p,
        id: await this.translateJid(p.id),
      })),
    );
    const normalized = { ...metadata, participants };
    this.groupMetadataCache.set(jid, {
      metadata: normalized,
      expiresAt: Date.now() + 60_000,
    });
    return normalized;
  }

  private async flushOutgoingQueue(): Promise<void> {
    if (this.flushing || this.outgoingQueue.length === 0) return;
    this.flushing = true;
    try {
      while (this.outgoingQueue.length > 0) {
        const item = this.outgoingQueue.shift()!;
        const sent = await this.sock.sendMessage(item.jid, { text: item.text });
        if (sent?.key?.id && sent.message) {
          this.sentMessageCache.set(sent.key.id, sent.message);
        }
      }
    } finally {
      this.flushing = false;
    }
  }
}
