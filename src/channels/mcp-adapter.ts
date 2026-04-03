/**
 * Generic MCP-to-Channel adapter.
 *
 * Spawns an MCP channel server as a child process (stdio transport),
 * bridges it to the Deus Channel interface. Incoming messages arrive
 * via MCP logging notifications; outbound messages go through callTool.
 */

import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';
import { LoggingMessageNotificationSchema } from '@modelcontextprotocol/sdk/types.js';

import { logger } from '../logger.js';
import type {
  Channel,
  NewMessage,
  OnChatMetadata,
  OnInboundMessage,
} from '../types.js';

export interface McpChannelAdapterOpts {
  /** Channel name (e.g., 'whatsapp', 'telegram'). */
  name: string;
  /** Command to spawn the MCP server. */
  command: string;
  /** Arguments for the command. */
  args: string[];
  /** Environment variables passed to the child process. */
  env?: Record<string, string>;
  /** Callback for incoming messages. */
  onMessage: OnInboundMessage;
  /** Callback for chat metadata discovery. */
  onChatMetadata: OnChatMetadata;
  /** JID ownership check — return true if this channel owns the JID. */
  ownsJid: (jid: string) => boolean;
}

export class McpChannelAdapter implements Channel {
  readonly name: string;

  private client: Client;
  private transport: StdioClientTransport;
  private connected = false;
  private opts: McpChannelAdapterOpts;

  constructor(opts: McpChannelAdapterOpts) {
    this.opts = opts;
    this.name = opts.name;

    this.transport = new StdioClientTransport({
      command: opts.command,
      args: opts.args,
      env: Object.fromEntries(
        Object.entries({ ...process.env, ...opts.env }).filter(
          (entry): entry is [string, string] => entry[1] !== undefined,
        ),
      ),
    });

    this.client = new Client({ name: 'deus-host', version: '1.0.0' });

    // Listen for incoming message notifications from the MCP server
    this.client.setNotificationHandler(
      LoggingMessageNotificationSchema,
      (notification) => {
        const params = notification.params;
        if (params.logger !== 'incoming_message') return;

        const data = params.data as Record<string, unknown> | undefined;
        if (!data) return;

        const chatJid = data.chat_id as string;
        const msg: NewMessage = {
          id: data.id as string,
          chat_jid: chatJid,
          sender: data.sender as string,
          sender_name: data.sender_name as string,
          content: data.content as string,
          timestamp: data.timestamp as string,
          is_from_me: data.is_from_me as boolean | undefined,
          is_bot_message: (data.metadata as Record<string, unknown>)
            ?.is_bot_message as boolean | undefined,
        };

        opts.onMessage(chatJid, msg);

        // Also emit chat metadata
        opts.onChatMetadata(
          chatJid,
          msg.timestamp,
          data.chat_name as string | undefined,
          opts.name,
          data.is_group as boolean | undefined,
        );
      },
    );
  }

  async connect(): Promise<void> {
    logger.info({ channel: this.name }, 'Connecting MCP channel server');

    await this.client.connect(this.transport);

    // The MCP server auto-connects if credentials exist.
    // Call connect tool to ensure it's ready.
    try {
      await this.client.callTool({ name: 'get_status', arguments: {} });
      this.connected = true;
      logger.info({ channel: this.name }, 'MCP channel server connected');
    } catch (err) {
      logger.error(
        { channel: this.name, err },
        'MCP channel server status check failed',
      );
      this.connected = true; // Server is running, connection may be pending
    }
  }

  async sendMessage(jid: string, text: string): Promise<void> {
    await this.client.callTool({
      name: 'send_message',
      arguments: { chat_id: jid, text },
    });
  }

  isConnected(): boolean {
    return this.connected;
  }

  ownsJid(jid: string): boolean {
    return this.opts.ownsJid(jid);
  }

  async disconnect(): Promise<void> {
    try {
      await this.client.callTool({ name: 'disconnect', arguments: {} });
    } catch {
      // Server may already be stopped
    }
    await this.client.close();
    this.connected = false;
  }

  async setTyping(jid: string, isTyping: boolean): Promise<void> {
    try {
      await this.client.callTool({
        name: 'send_typing',
        arguments: { chat_id: jid, is_typing: isTyping },
      });
    } catch {
      // Best effort
    }
  }

  async syncGroups(): Promise<void> {
    try {
      await this.client.callTool({
        name: 'sync_groups',
        arguments: { force: true },
      });
    } catch {
      // Best effort
    }
  }
}
