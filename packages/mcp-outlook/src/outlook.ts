/**
 * Standalone Outlook (Microsoft 365) provider.
 * Adapted from the Deus Gmail channel — no Deus-specific dependencies.
 * Config comes from a credentials directory; incoming emails are forwarded to onMessage.
 *
 * Uses polling (60s default) against the Microsoft Graph API for unread Inbox
 * emails. Each conversation maps to a chat with JID format: outlook:<conversationId>.
 *
 * Auth: Azure AD via @azure/msal-node. The device-code flow (run once via the
 * `auth` subcommand in index.ts) seeds a persisted token cache (token.json) that
 * holds a refresh token; connect() acquires access tokens silently from it.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';

import {
  PublicClientApplication,
  type AccountInfo,
  type ICachePlugin,
  type TokenCacheContext,
} from '@azure/msal-node';
import { Client } from '@microsoft/microsoft-graph-client';
import pino from 'pino';

import type {
  ChannelProvider,
  ChannelStatus,
  ChatInfo,
  IncomingMessage,
} from '@deus-ai/channel-core';

const CREDENTIALS_DIR =
  process.env.OUTLOOK_CREDENTIALS_DIR ||
  path.join(os.homedir(), '.outlook-mcp');
const POLL_INTERVAL_MS = parseInt(
  process.env.OUTLOOK_POLL_INTERVAL_MS || '60000',
  10,
);
const MAX_BACKOFF_MS = 30 * 60 * 1000; // 30 minutes

// Delegated Microsoft Graph scopes. offline_access is implicit (MSAL requests a
// refresh token automatically), so it is not listed here.
const SCOPES = ['Mail.Read', 'Mail.ReadWrite', 'Mail.Send', 'User.Read'];

// Use stderr for logging (stdout is reserved for MCP JSON-RPC)
const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

interface AppCredentials {
  clientId: string;
  tenantId: string;
}

interface ConversationMeta {
  /** A message id within the conversation, used as the reply target. */
  messageId: string;
  sender: string;
  senderName: string;
  subject: string;
}

const appCredentialsPath = () =>
  path.join(CREDENTIALS_DIR, 'app-credentials.json');
const tokenCachePath = () => path.join(CREDENTIALS_DIR, 'token.json');

/** MSAL cache plugin that persists the token cache to token.json. */
function buildCachePlugin(): ICachePlugin {
  const tokenPath = tokenCachePath();
  return {
    beforeCacheAccess: async (ctx: TokenCacheContext) => {
      if (fs.existsSync(tokenPath)) {
        ctx.tokenCache.deserialize(fs.readFileSync(tokenPath, 'utf-8'));
      }
    },
    afterCacheAccess: async (ctx: TokenCacheContext) => {
      if (ctx.cacheHasChanged) {
        fs.mkdirSync(CREDENTIALS_DIR, { recursive: true });
        fs.writeFileSync(tokenPath, ctx.tokenCache.serialize());
      }
    },
  };
}

function loadAppCredentials(): AppCredentials {
  const raw = JSON.parse(fs.readFileSync(appCredentialsPath(), 'utf-8'));
  if (!raw.clientId || !raw.tenantId) {
    throw new Error('app-credentials.json must contain clientId and tenantId.');
  }
  return raw as AppCredentials;
}

/**
 * Build a public-client MSAL app wired to the persisted token cache.
 *
 * Delegated access only: a user signs in once via the device-code flow
 * (`node dist/index.js auth`), seeding a refresh token in the cache from which
 * access tokens are acquired silently. The confidential-client (clientSecret /
 * app-only) flow is intentionally NOT supported — it uses client-credentials
 * (no user account, no `/me`, tenant-admin application permissions) which does
 * not fit a personal mailbox. Exported for the auth subcommand in index.ts.
 */
export function buildMsalClient(
  creds: AppCredentials,
): PublicClientApplication {
  return new PublicClientApplication({
    auth: {
      clientId: creds.clientId,
      authority: `https://login.microsoftonline.com/${creds.tenantId}`,
    },
    cache: { cachePlugin: buildCachePlugin() },
  });
}

// Re-exported for the auth subcommand + token-path checks in index.ts.
export { CREDENTIALS_DIR, SCOPES, appCredentialsPath, tokenCachePath };

export class OutlookProvider implements ChannelProvider {
  readonly name = 'outlook';

  private msal: PublicClientApplication | null = null;
  private account: AccountInfo | null = null;
  private graph: Client | null = null;
  private connectTime = 0;
  private pollTimer: ReturnType<typeof setTimeout> | null = null;
  private processedIds = new Set<string>();
  private convMeta = new Map<string, ConversationMeta>();
  private knownChats = new Map<string, { name: string; isGroup: boolean }>();
  private consecutiveErrors = 0;
  private userEmail = '';

  // Set by server-base.ts
  onMessage: (msg: IncomingMessage) => void = () => {};

  /** Check whether app credentials and a token cache both exist. */
  hasCredentials(): boolean {
    return (
      fs.existsSync(appCredentialsPath()) && fs.existsSync(tokenCachePath())
    );
  }

  /** Acquire a fresh access token silently from the cached account. */
  private async getToken(): Promise<string> {
    if (!this.msal || !this.account) {
      throw new Error('Outlook not connected');
    }
    const result = await this.msal.acquireTokenSilent({
      account: this.account,
      scopes: SCOPES,
    });
    if (!result?.accessToken) {
      throw new Error('Failed to acquire Microsoft Graph access token');
    }
    return result.accessToken;
  }

  async connect(): Promise<void> {
    if (!this.hasCredentials()) {
      throw new Error(
        `Outlook credentials not found in ${CREDENTIALS_DIR}. ` +
          'Place app-credentials.json there and run `node dist/index.js auth`, ' +
          'or set OUTLOOK_CREDENTIALS_DIR.',
      );
    }

    const creds = loadAppCredentials();
    this.msal = buildMsalClient(creds);

    const accounts = await this.msal.getTokenCache().getAllAccounts();
    if (accounts.length === 0) {
      throw new Error(
        'No cached Outlook account. Run `node dist/index.js auth` to sign in first.',
      );
    }
    this.account = accounts[0];

    // Build a Graph client that fetches a fresh token per request.
    this.graph = Client.init({
      authProvider: (done) => {
        this.getToken().then(
          (token) => done(null, token),
          (err) => done(err as Error, null),
        );
      },
    });

    // Verify connection + capture identity.
    const me = await this.graph.api('/me').get();
    this.userEmail = me.userPrincipalName || me.mail || '';
    this.connectTime = Date.now();
    logger.info({ email: this.userEmail }, 'Outlook channel connected');

    // Start polling with error backoff (mirrors the Gmail channel).
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
          .catch((err) => logger.error({ err }, 'Outlook poll error'))
          .finally(() => {
            if (this.graph) schedulePoll();
          });
      }, backoffMs);
    };

    await this.pollForMessages();
    schedulePoll();
  }

  async sendMessage(chatId: string, text: string): Promise<void> {
    if (!this.graph) {
      logger.warn('Outlook not initialized');
      return;
    }

    const conversationId = chatId.replace(/^outlook:/, '');
    const meta = this.convMeta.get(conversationId);

    // KNOWN LIMITATION: the reply target (a messageId in the conversation) is
    // populated only after a poll cycle has seen that conversation. A reply to a
    // thread received before this process started has no stored messageId, so we
    // log and skip rather than send into the void.
    if (!meta) {
      logger.warn(
        { chatId },
        'No conversation metadata for reply, cannot send (no prior inbound seen)',
      );
      return;
    }

    try {
      await this.graph
        .api(`/me/messages/${meta.messageId}/reply`)
        .post({ comment: text });
      logger.info({ to: meta.sender, conversationId }, 'Outlook reply sent');
    } catch (err) {
      logger.error({ chatId, err }, 'Failed to send Outlook reply');
    }
  }

  isConnected(): boolean {
    return this.graph !== null;
  }

  getStatus(): ChannelStatus {
    return {
      connected: this.graph !== null,
      channel: 'outlook',
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
    this.graph = null;
    this.msal = null;
    this.account = null;
    this.consecutiveErrors = 0;
    logger.info('Outlook channel stopped');
  }

  async listChats(): Promise<ChatInfo[]> {
    return Array.from(this.knownChats.entries()).map(([id, info]) => ({
      id,
      name: info.name,
      is_group: info.isGroup,
    }));
  }

  // ── Outlook-specific public methods (exposed as MCP tools) ───────────

  /** Read a full email by message ID. */
  async readEmail(messageId: string): Promise<{
    from: string;
    to: string;
    subject: string;
    date: string;
    body: string;
    conversationId: string;
  }> {
    if (!this.graph) throw new Error('Outlook not connected');

    const msg = await this.graph
      .api(`/me/messages/${messageId}`)
      .select([
        'subject',
        'from',
        'toRecipients',
        'receivedDateTime',
        'body',
        'conversationId',
      ])
      .get();

    return {
      from: formatAddress(msg.from),
      to: (msg.toRecipients || []).map(formatAddress).join(', '),
      subject: msg.subject || '',
      date: msg.receivedDateTime || '',
      body: extractBody(msg.body),
      conversationId: msg.conversationId || '',
    };
  }

  /**
   * Search emails by Graph $search query string.
   * The query is passed verbatim into Graph `$search` (the caller is the trusted
   * agent, mirroring the Gmail channel's verbatim query). It is NOT sanitized, so
   * OData operators in the query widen scope by design; do not feed it raw
   * untrusted user text without the agent intending a broad search.
   */
  async searchEmails(
    query: string,
    maxResults = 10,
  ): Promise<Array<{ id: string; conversationId: string; snippet: string }>> {
    if (!this.graph) throw new Error('Outlook not connected');

    const res = await this.graph
      .api('/me/messages')
      .search(`"${query}"`)
      .top(maxResults)
      .select(['id', 'conversationId', 'bodyPreview'])
      .get();

    return (res.value || []).map(
      (m: { id?: string; conversationId?: string; bodyPreview?: string }) => ({
        id: m.id || '',
        conversationId: m.conversationId || '',
        snippet: m.bodyPreview || '',
      }),
    );
  }

  /** Send a new email (not a thread reply). */
  async sendEmail(to: string, subject: string, body: string): Promise<void> {
    if (!this.graph) throw new Error('Outlook not connected');

    await this.graph.api('/me/sendMail').post({
      message: {
        subject,
        body: { contentType: 'Text', content: body },
        toRecipients: [{ emailAddress: { address: to } }],
      },
      saveToSentItems: true,
    });
    logger.info({ to, subject }, 'Email sent');
  }

  /** Create a draft email. */
  async draftEmail(to: string, subject: string, body: string): Promise<string> {
    if (!this.graph) throw new Error('Outlook not connected');

    const res = await this.graph.api('/me/messages').post({
      subject,
      body: { contentType: 'Text', content: body },
      toRecipients: [{ emailAddress: { address: to } }],
    });
    const draftId = res.id || '';
    logger.info({ to, subject, draftId }, 'Draft created');
    return draftId;
  }

  // ── Private ──────────────────────────────────────────────────────────

  private async pollForMessages(): Promise<void> {
    if (!this.graph) return;

    try {
      const res = await this.graph
        .api('/me/mailFolders/Inbox/messages')
        .filter('isRead eq false')
        .top(10)
        .select([
          'id',
          'conversationId',
          'subject',
          'from',
          'receivedDateTime',
          'body',
          'bodyPreview',
        ])
        .get();

      const messages = res.value || [];

      for (const msg of messages) {
        if (!msg.id || this.processedIds.has(msg.id)) continue;
        this.processedIds.add(msg.id);
        await this.processMessage(msg);
      }

      // Cap processed ID set to prevent unbounded growth. slice keeps the NEWEST
      // 2500 ids (Set preserves insertion order) so recent dedup history survives.
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
        'Outlook poll failed',
      );
    }
  }

  private async processMessage(msg: {
    id: string;
    conversationId?: string;
    subject?: string;
    from?: GraphRecipient;
    receivedDateTime?: string;
    body?: { contentType?: string; content?: string };
    bodyPreview?: string;
  }): Promise<void> {
    if (!this.graph) return;

    const senderEmail = msg.from?.emailAddress?.address || '';
    const senderName = msg.from?.emailAddress?.name || senderEmail;

    // Skip emails from self (our own replies).
    if (
      senderEmail &&
      senderEmail.toLowerCase() === this.userEmail.toLowerCase()
    ) {
      return;
    }

    const subject = msg.subject || '(no subject)';
    const conversationId = msg.conversationId || msg.id;
    const timestamp = msg.receivedDateTime || new Date().toISOString();
    const body = extractBody(msg.body) || msg.bodyPreview || '';

    const chatJid = `outlook:${conversationId}`;

    // Cache conversation metadata for replies (the reply target message id).
    this.convMeta.set(conversationId, {
      messageId: msg.id,
      sender: senderEmail,
      senderName,
      subject,
    });
    this.knownChats.set(chatJid, { name: subject, isGroup: false });

    const content = `[Email from ${senderName} <${senderEmail}>]\nSubject: ${subject}\n\n${body}`;

    this.onMessage({
      id: msg.id,
      chat_id: chatJid,
      sender: senderEmail,
      sender_name: senderName,
      content,
      timestamp,
      is_from_me: false,
      is_group: false,
      chat_name: subject,
      metadata: { conversation_id: conversationId, subject },
    });

    // Mark as read so it is not re-processed.
    try {
      await this.graph.api(`/me/messages/${msg.id}`).patch({ isRead: true });
    } catch (err) {
      logger.warn({ messageId: msg.id, err }, 'Failed to mark email as read');
    }

    logger.info(
      { from: senderName, subject, conversationId },
      'Outlook email processed',
    );
  }
}

// ── Module-level helpers ────────────────────────────────────────────────

interface GraphRecipient {
  emailAddress?: { name?: string; address?: string };
}

function formatAddress(r: GraphRecipient | undefined): string {
  const addr = r?.emailAddress?.address || '';
  const name = r?.emailAddress?.name;
  return name && name !== addr ? `${name} <${addr}>` : addr;
}

/** Extract plain text from a Graph message body (strip HTML tags when needed). */
function extractBody(
  body: { contentType?: string; content?: string } | undefined,
): string {
  if (!body?.content) return '';
  if (body.contentType?.toLowerCase() === 'html') {
    return body.content
      .replace(/<style[\s\S]*?<\/style>/gi, '')
      .replace(/<script[\s\S]*?<\/script>/gi, '')
      .replace(/<[^>]+>/g, ' ')
      .replace(/&nbsp;/g, ' ')
      .replace(/\s+/g, ' ')
      .trim();
  }
  return body.content;
}
