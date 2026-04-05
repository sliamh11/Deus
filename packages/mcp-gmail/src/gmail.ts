/**
 * Standalone Gmail provider.
 * Adapted from Deus GmailChannel — no Deus-specific dependencies.
 * Config comes from env vars; incoming emails are forwarded to onMessage.
 *
 * Uses polling (60s default) against the Gmail API for unread Primary emails.
 * Each email thread maps to a chat with JID format: gmail:<thread_id>.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

import { google, gmail_v1 } from 'googleapis';
import { OAuth2Client } from 'google-auth-library';
import pino from 'pino';

import type {
  ChannelProvider,
  ChannelStatus,
  ChatInfo,
  IncomingMessage,
} from '@deus-ai/channel-core';

const CREDENTIALS_DIR =
  process.env.GMAIL_CREDENTIALS_DIR || path.join(os.homedir(), '.gmail-mcp');
const POLL_INTERVAL_MS = parseInt(
  process.env.GMAIL_POLL_INTERVAL_MS || '60000',
  10,
);
const MAX_BACKOFF_MS = 30 * 60 * 1000; // 30 minutes

// Use stderr for logging (stdout is reserved for MCP JSON-RPC)
const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

interface ThreadMeta {
  sender: string;
  senderName: string;
  subject: string;
  messageId: string; // RFC 2822 Message-ID for In-Reply-To
}

export class GmailProvider implements ChannelProvider {
  readonly name = 'gmail';

  private oauth2Client: OAuth2Client | null = null;
  private gmail: gmail_v1.Gmail | null = null;
  private connectTime = 0;
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private processedIds = new Set<string>();
  private threadMeta = new Map<string, ThreadMeta>();
  private knownChats = new Map<string, { name: string; isGroup: boolean }>();
  private consecutiveErrors = 0;
  private userEmail = '';

  // Set by server-base.ts
  onMessage: (msg: IncomingMessage) => void = () => {};

  /** Check whether OAuth credential files exist. */
  hasCredentials(): boolean {
    const keysPath = path.join(CREDENTIALS_DIR, 'gcp-oauth.keys.json');
    const tokensPath = path.join(CREDENTIALS_DIR, 'credentials.json');
    return fs.existsSync(keysPath) && fs.existsSync(tokensPath);
  }

  async connect(): Promise<void> {
    const keysPath = path.join(CREDENTIALS_DIR, 'gcp-oauth.keys.json');
    const tokensPath = path.join(CREDENTIALS_DIR, 'credentials.json');

    if (!fs.existsSync(keysPath) || !fs.existsSync(tokensPath)) {
      throw new Error(
        `Gmail credentials not found in ${CREDENTIALS_DIR}. ` +
          'Place gcp-oauth.keys.json and credentials.json there, or set GMAIL_CREDENTIALS_DIR.',
      );
    }

    const keys = JSON.parse(fs.readFileSync(keysPath, 'utf-8'));
    const tokens = JSON.parse(fs.readFileSync(tokensPath, 'utf-8'));

    const clientConfig = keys.installed || keys.web || keys;
    const { client_id, client_secret, redirect_uris } = clientConfig;
    this.oauth2Client = new google.auth.OAuth2(
      client_id,
      client_secret,
      redirect_uris?.[0],
    );
    this.oauth2Client.setCredentials(tokens);

    // Persist refreshed tokens
    this.oauth2Client.on('tokens', (newTokens) => {
      try {
        const current = JSON.parse(fs.readFileSync(tokensPath, 'utf-8'));
        Object.assign(current, newTokens);
        fs.writeFileSync(tokensPath, JSON.stringify(current, null, 2));
        logger.debug('Gmail OAuth tokens refreshed');
      } catch (err) {
        logger.warn({ err }, 'Failed to persist refreshed Gmail tokens');
      }
    });

    this.gmail = google.gmail({ version: 'v1', auth: this.oauth2Client });

    // Verify connection
    const profile = await this.gmail.users.getProfile({ userId: 'me' });
    this.userEmail = profile.data.emailAddress || '';
    this.connectTime = Date.now();
    logger.info({ email: this.userEmail }, 'Gmail channel connected');

    // Start polling with error backoff
    const schedulePoll = () => {
      const backoffMs =
        this.consecutiveErrors > 0
          ? Math.min(
              POLL_INTERVAL_MS * Math.pow(2, this.consecutiveErrors),
              MAX_BACKOFF_MS,
            )
          : POLL_INTERVAL_MS;
      this.pollTimer = setTimeout(() => {
        this.pollForMessages()
          .catch((err) => logger.error({ err }, 'Gmail poll error'))
          .finally(() => {
            if (this.gmail) schedulePoll();
          });
      }, backoffMs);
    };

    // Initial poll
    await this.pollForMessages();
    schedulePoll();
  }

  async sendMessage(chatId: string, text: string): Promise<void> {
    if (!this.gmail) {
      logger.warn('Gmail not initialized');
      return;
    }

    const threadId = chatId.replace(/^gmail:/, '');
    const meta = this.threadMeta.get(threadId);

    if (!meta) {
      logger.warn({ chatId }, 'No thread metadata for reply, cannot send');
      return;
    }

    const subject = meta.subject.startsWith('Re:')
      ? meta.subject
      : `Re: ${meta.subject}`;

    const headers = [
      `To: ${meta.sender}`,
      `From: ${this.userEmail}`,
      `Subject: ${subject}`,
      `In-Reply-To: ${meta.messageId}`,
      `References: ${meta.messageId}`,
      'Content-Type: text/plain; charset=utf-8',
      '',
      text,
    ].join('\r\n');

    const encodedMessage = Buffer.from(headers)
      .toString('base64')
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '');

    try {
      await this.gmail.users.messages.send({
        userId: 'me',
        requestBody: {
          raw: encodedMessage,
          threadId,
        },
      });
      logger.info({ to: meta.sender, threadId }, 'Gmail reply sent');
    } catch (err) {
      logger.error({ chatId, err }, 'Failed to send Gmail reply');
    }
  }

  isConnected(): boolean {
    return this.gmail !== null;
  }

  getStatus(): ChannelStatus {
    return {
      connected: this.gmail !== null,
      channel: 'gmail',
      identity: this.userEmail || undefined,
      uptime_seconds: this.connectTime
        ? Math.floor((Date.now() - this.connectTime) / 1000)
        : 0,
    };
  }

  async disconnect(): Promise<void> {
    if (this.pollTimer) {
      clearTimeout(this.pollTimer);
      this.pollTimer = null;
    }
    this.gmail = null;
    this.oauth2Client = null;
    this.consecutiveErrors = 0;
    logger.info('Gmail channel stopped');
  }

  async listChats(): Promise<ChatInfo[]> {
    return Array.from(this.knownChats.entries()).map(([id, info]) => ({
      id,
      name: info.name,
      is_group: info.isGroup,
    }));
  }

  // ── Gmail-specific public methods (exposed as MCP tools) ─────────────

  /** Read a full email by message ID. */
  async readEmail(messageId: string): Promise<{
    from: string;
    to: string;
    subject: string;
    date: string;
    body: string;
    threadId: string;
  }> {
    if (!this.gmail) throw new Error('Gmail not connected');

    const msg = await this.gmail.users.messages.get({
      userId: 'me',
      id: messageId,
      format: 'full',
    });

    const headers = msg.data.payload?.headers || [];
    const getHeader = (name: string) =>
      headers.find((h) => h.name?.toLowerCase() === name.toLowerCase())
        ?.value || '';

    return {
      from: getHeader('From'),
      to: getHeader('To'),
      subject: getHeader('Subject'),
      date: getHeader('Date'),
      body: this.extractTextBody(msg.data.payload),
      threadId: msg.data.threadId || messageId,
    };
  }

  /** Search emails by query string. */
  async searchEmails(
    query: string,
    maxResults = 10,
  ): Promise<Array<{ id: string; threadId: string; snippet: string }>> {
    if (!this.gmail) throw new Error('Gmail not connected');

    const res = await this.gmail.users.messages.list({
      userId: 'me',
      q: query,
      maxResults,
    });

    const results: Array<{ id: string; threadId: string; snippet: string }> =
      [];
    for (const stub of res.data.messages || []) {
      if (!stub.id) continue;
      const msg = await this.gmail.users.messages.get({
        userId: 'me',
        id: stub.id,
        format: 'metadata',
        metadataHeaders: ['Subject', 'From', 'Date'],
      });
      results.push({
        id: stub.id,
        threadId: msg.data.threadId || stub.id,
        snippet: msg.data.snippet || '',
      });
    }
    return results;
  }

  /** Send a new email (not a reply). */
  async sendEmail(to: string, subject: string, body: string): Promise<void> {
    if (!this.gmail) throw new Error('Gmail not connected');

    const headers = [
      `To: ${to}`,
      `From: ${this.userEmail}`,
      `Subject: ${subject}`,
      'Content-Type: text/plain; charset=utf-8',
      '',
      body,
    ].join('\r\n');

    const encodedMessage = Buffer.from(headers)
      .toString('base64')
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '');

    await this.gmail.users.messages.send({
      userId: 'me',
      requestBody: { raw: encodedMessage },
    });
    logger.info({ to, subject }, 'Email sent');
  }

  /** Create a draft email. */
  async draftEmail(to: string, subject: string, body: string): Promise<string> {
    if (!this.gmail) throw new Error('Gmail not connected');

    const headers = [
      `To: ${to}`,
      `From: ${this.userEmail}`,
      `Subject: ${subject}`,
      'Content-Type: text/plain; charset=utf-8',
      '',
      body,
    ].join('\r\n');

    const encodedMessage = Buffer.from(headers)
      .toString('base64')
      .replace(/\+/g, '-')
      .replace(/\//g, '_')
      .replace(/=+$/, '');

    const res = await this.gmail.users.drafts.create({
      userId: 'me',
      requestBody: {
        message: { raw: encodedMessage },
      },
    });
    const draftId = res.data.id || '';
    logger.info({ to, subject, draftId }, 'Draft created');
    return draftId;
  }

  // ── Private ──────────────────────────────────────────────────────────

  private buildQuery(): string {
    return 'is:unread category:primary';
  }

  private async pollForMessages(): Promise<void> {
    if (!this.gmail) return;

    try {
      const query = this.buildQuery();
      const res = await this.gmail.users.messages.list({
        userId: 'me',
        q: query,
        maxResults: 10,
      });

      const messages = res.data.messages || [];

      for (const stub of messages) {
        if (!stub.id || this.processedIds.has(stub.id)) continue;
        this.processedIds.add(stub.id);

        await this.processMessage(stub.id);
      }

      // Cap processed ID set to prevent unbounded growth
      if (this.processedIds.size > 5000) {
        const ids = [...this.processedIds];
        this.processedIds = new Set(ids.slice(ids.length - 2500));
      }

      this.consecutiveErrors = 0;
    } catch (err) {
      this.consecutiveErrors++;
      const backoffMs = Math.min(
        POLL_INTERVAL_MS * Math.pow(2, this.consecutiveErrors),
        MAX_BACKOFF_MS,
      );
      logger.error(
        {
          err,
          consecutiveErrors: this.consecutiveErrors,
          nextPollMs: backoffMs,
        },
        'Gmail poll failed',
      );
    }
  }

  private async processMessage(messageId: string): Promise<void> {
    if (!this.gmail) return;

    const msg = await this.gmail.users.messages.get({
      userId: 'me',
      id: messageId,
      format: 'full',
    });

    const headers = msg.data.payload?.headers || [];
    const getHeader = (name: string) =>
      headers.find((h) => h.name?.toLowerCase() === name.toLowerCase())
        ?.value || '';

    const from = getHeader('From');
    const subject = getHeader('Subject');
    const rfc2822MessageId = getHeader('Message-ID');
    const threadId = msg.data.threadId || messageId;
    const timestamp = new Date(
      parseInt(msg.data.internalDate || '0', 10),
    ).toISOString();

    // Extract sender name and email
    const senderMatch = from.match(/^(.+?)\s*<(.+?)>$/);
    const senderName = senderMatch ? senderMatch[1].replace(/"/g, '') : from;
    const senderEmail = senderMatch ? senderMatch[2] : from;

    // Skip emails from self (our own replies)
    if (senderEmail === this.userEmail) return;

    // Extract body text
    const body = this.extractTextBody(msg.data.payload);

    if (!body) {
      logger.debug({ messageId, subject }, 'Skipping email with no text body');
      return;
    }

    const chatJid = `gmail:${threadId}`;

    // Cache thread metadata for replies
    this.threadMeta.set(threadId, {
      sender: senderEmail,
      senderName,
      subject,
      messageId: rfc2822MessageId,
    });

    // Track chat
    this.knownChats.set(chatJid, { name: subject, isGroup: false });

    const content = `[Email from ${senderName} <${senderEmail}>]\nSubject: ${subject}\n\n${body}`;

    this.onMessage({
      id: messageId,
      chat_id: chatJid,
      sender: senderEmail,
      sender_name: senderName,
      content,
      timestamp,
      is_from_me: false,
      is_group: false,
      chat_name: subject,
      metadata: {
        thread_id: threadId,
        subject,
      },
    });

    // Mark as read
    try {
      await this.gmail.users.messages.modify({
        userId: 'me',
        id: messageId,
        requestBody: { removeLabelIds: ['UNREAD'] },
      });
    } catch (err) {
      logger.warn({ messageId, err }, 'Failed to mark email as read');
    }

    logger.info(
      { from: senderName, subject, threadId },
      'Gmail email processed',
    );
  }

  private extractTextBody(
    payload: gmail_v1.Schema$MessagePart | undefined,
  ): string {
    if (!payload) return '';

    // Direct text/plain body
    if (payload.mimeType === 'text/plain' && payload.body?.data) {
      return Buffer.from(payload.body.data, 'base64').toString('utf-8');
    }

    // Multipart: search parts recursively
    if (payload.parts) {
      // Prefer text/plain
      for (const part of payload.parts) {
        if (part.mimeType === 'text/plain' && part.body?.data) {
          return Buffer.from(part.body.data, 'base64').toString('utf-8');
        }
      }
      // Recurse into nested multipart
      for (const part of payload.parts) {
        const text = this.extractTextBody(part);
        if (text) return text;
      }
    }

    return '';
  }
}
