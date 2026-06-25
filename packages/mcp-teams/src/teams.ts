/**
 * Standalone Microsoft Teams bot provider.
 * Adapted from the Deus Slack channel — no Deus-specific dependencies.
 * Config comes from env vars; all messages are forwarded to onMessage.
 *
 * Uses the Azure Bot Service (Bot Framework). Unlike Slack's Socket Mode, the
 * Bot Framework delivers activities to a PUBLIC HTTPS messaging endpoint, so this
 * provider runs its own HTTP server on TEAMS_PORT and serves POST /api/messages.
 * The port must be exposed publicly (a dedicated tunnel) and set as the Azure Bot
 * messaging endpoint — Deus's existing ingress tunnel forwards only the gateway
 * port and does NOT cover TEAMS_PORT.
 *
 * Each conversation maps to a chat with JID format: teams:<conversationId>.
 * Outbound replies use Bot Framework proactive messaging, which requires a stored
 * ConversationReference captured from a prior inbound activity — these are
 * persisted to disk so replies survive a restart.
 */

import fs from 'fs';
import http from 'http';
import os from 'os';
import path from 'path';

import {
  ActivityTypes,
  CloudAdapter,
  ConfigurationBotFrameworkAuthentication,
  ConfigurationServiceClientCredentialFactory,
  TurnContext,
  type Activity,
  type ConversationReference,
} from 'botbuilder';
import express, { type Express } from 'express';
import pino from 'pino';

import type {
  ChannelProvider,
  ChannelStatus,
  ChatInfo,
  IncomingMessage,
} from '@deus-ai/channel-core';

const CREDENTIALS_DIR =
  process.env.TEAMS_CREDENTIALS_DIR || path.join(os.homedir(), '.teams-mcp');

// Read env vars lazily so tests can set them before connect().
function getAppId(): string {
  return process.env.TEAMS_APP_ID || '';
}
function getAppPassword(): string {
  return process.env.TEAMS_APP_PASSWORD || '';
}
function getAppTenantId(): string {
  return process.env.TEAMS_APP_TENANT_ID || '';
}
function getPort(): number {
  return parseInt(process.env.TEAMS_PORT || '3978', 10);
}

// Use stderr for logging (stdout is reserved for MCP JSON-RPC)
const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

const conversationsPath = () =>
  path.join(CREDENTIALS_DIR, 'conversations.json');

export class TeamsProvider implements ChannelProvider {
  readonly name = 'teams';

  private adapter: CloudAdapter | null = null;
  private server: http.Server | null = null;
  private connected = false;
  private connectTime = 0;
  private appId = '';
  private conversationRefs = new Map<string, Partial<ConversationReference>>();
  private knownChats = new Map<string, { name: string; isGroup: boolean }>();

  // Set by server-base.ts
  onMessage: (msg: IncomingMessage) => void = () => {};

  /** Check whether the bot credentials are configured. */
  hasCredentials(): boolean {
    return !!getAppId() && !!getAppPassword();
  }

  async connect(): Promise<void> {
    const appId = getAppId();
    const appPassword = getAppPassword();
    if (!appId || !appPassword) {
      throw new Error('TEAMS_APP_ID and TEAMS_APP_PASSWORD must be set');
    }
    this.appId = appId;
    const tenantId = getAppTenantId();

    const credentialsFactory = new ConfigurationServiceClientCredentialFactory({
      MicrosoftAppId: appId,
      MicrosoftAppPassword: appPassword,
      MicrosoftAppType: tenantId ? 'SingleTenant' : 'MultiTenant',
      MicrosoftAppTenantId: tenantId || undefined,
    });
    const botFrameworkAuthentication =
      new ConfigurationBotFrameworkAuthentication({}, credentialsFactory);
    this.adapter = new CloudAdapter(botFrameworkAuthentication);
    this.adapter.onTurnError = async (context, error) => {
      logger.error({ err: error }, 'Teams turn error');
    };

    this.loadConversationRefs();

    const app: Express = express();
    app.use(express.json());
    app.post('/api/messages', (req, res) => {
      this.adapter
        ?.process(req, res, (context) => this.handleTurn(context))
        .catch((err) => logger.error({ err }, 'Teams process error'));
    });

    await new Promise<void>((resolve) => {
      this.server = app.listen(getPort(), () => {
        logger.info(
          { port: getPort(), appId: this.appId },
          'Teams bot listening on /api/messages',
        );
        resolve();
      });
    });

    this.connected = true;
    this.connectTime = Date.now();
  }

  private async handleTurn(context: TurnContext): Promise<void> {
    if (context.activity.type !== ActivityTypes.Message) return;

    const activity = context.activity;
    const convId = activity.conversation?.id;
    if (!convId) return;

    // Save the conversation reference for proactive replies (persist to disk so
    // replies survive a restart).
    const ref = TurnContext.getConversationReference(activity);
    this.conversationRefs.set(convId, ref);
    await this.persistConversationRefs();

    const isGroup = activity.conversation?.isGroup ?? false;
    const senderName = activity.from?.name || activity.from?.id || 'unknown';
    const chatJid = `teams:${convId}`;
    this.knownChats.set(chatJid, {
      name: activity.conversation?.name || senderName,
      isGroup,
    });

    this.onMessage({
      id: activity.id || '',
      chat_id: chatJid,
      sender: activity.from?.id || '',
      sender_name: senderName,
      content: activity.text || '',
      timestamp: (activity.timestamp
        ? new Date(activity.timestamp)
        : new Date()
      ).toISOString(),
      is_from_me: false,
      is_group: isGroup,
      chat_name: activity.conversation?.name,
      metadata: { conversation_id: convId },
    });
  }

  async sendMessage(chatId: string, text: string): Promise<void> {
    const convId = chatId.replace(/^teams:/, '');
    const ref = this.conversationRefs.get(convId);

    // KNOWN LIMITATION: Bot Framework proactive messaging needs a stored
    // conversation reference, captured only from a prior inbound activity. With
    // no reference (no prior inbound seen) there is no way to address the
    // conversation, so we log and skip rather than fail loudly.
    if (!this.adapter || !ref) {
      logger.warn(
        { chatId },
        'No conversation reference for reply, cannot send (no prior inbound seen)',
      );
      return;
    }

    try {
      await this.adapter.continueConversationAsync(
        this.appId,
        ref,
        async (context) => {
          await context.sendActivity(text);
        },
      );
      logger.info({ chatId }, 'Teams message sent');
    } catch (err) {
      logger.error({ chatId, err }, 'Failed to send Teams message');
    }
  }

  isConnected(): boolean {
    return this.connected;
  }

  getStatus(): ChannelStatus {
    return {
      connected: this.connected,
      channel: 'teams',
      identity: this.appId || undefined,
      uptime_seconds: this.connectTime
        ? Math.floor((Date.now() - this.connectTime) / 1000)
        : 0,
    };
  }

  async disconnect(): Promise<void> {
    this.connected = false;
    if (this.server) {
      await new Promise<void>((resolve) => this.server?.close(() => resolve()));
      this.server = null;
    }
    this.adapter = null;
    logger.info('Teams bot stopped');
  }

  async listChats(): Promise<ChatInfo[]> {
    return Array.from(this.knownChats.entries()).map(([id, info]) => ({
      id,
      name: info.name,
      is_group: info.isGroup,
    }));
  }

  // ── Private: conversation-reference persistence ──────────────────────

  private loadConversationRefs(): void {
    try {
      if (fs.existsSync(conversationsPath())) {
        const raw = JSON.parse(fs.readFileSync(conversationsPath(), 'utf-8'));
        for (const [id, ref] of Object.entries(raw)) {
          // Trusted shape: each value was serialized by getConversationReference
          // (Bot Framework's own structure) on a prior inbound activity.
          this.conversationRefs.set(id, ref as Partial<ConversationReference>);
        }
        logger.info(
          { count: this.conversationRefs.size },
          'Loaded Teams conversation references',
        );
      }
    } catch (err) {
      logger.warn({ err }, 'Failed to load Teams conversation references');
    }
  }

  private async persistConversationRefs(): Promise<void> {
    // Async write: handleTurn runs inside the /api/messages request handler, so a
    // synchronous write would block the event loop (and delay Bot Framework 200s)
    // on every inbound activity.
    try {
      await fs.promises.mkdir(CREDENTIALS_DIR, { recursive: true });
      const obj = Object.fromEntries(this.conversationRefs.entries());
      await fs.promises.writeFile(
        conversationsPath(),
        JSON.stringify(obj, null, 2),
      );
    } catch (err) {
      logger.warn({ err }, 'Failed to persist Teams conversation references');
    }
  }
}

export type { Activity };
