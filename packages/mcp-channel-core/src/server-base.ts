import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';

import { MessageBuffer } from './message-buffer.js';
import { mcpError, McpErrorCode, mcpResponse } from './response.js';
import type {
  ChannelProvider,
  IncomingMessage,
  IncomingReaction,
} from './types.js';

/**
 * Register the common MCP tools that all channel servers share.
 * Channel-specific tools (e.g., WhatsApp auth) are registered separately.
 */
export function registerCommonTools(
  server: McpServer,
  provider: ChannelProvider,
): MessageBuffer {
  const buffer = new MessageBuffer();

  // Wire provider's message handler → buffer + MCP notification
  provider.onMessage = (msg: IncomingMessage) => {
    buffer.push(msg);

    // Push to MCP client via logging notification (real-time path).
    // Fire-and-forget: a failed notification must not break ingestion.
    server.server
      .sendLoggingMessage({
        level: 'info',
        logger: 'incoming_message',
        data: msg,
      })
      .catch((err: unknown) => {
        console.error(
          JSON.stringify({
            level: 'error',
            task: 'channel-core.onMessage.notify',
            err: err instanceof Error ? err.message : err,
            msg: 'floating-promise',
          }),
        );
      });
  };

  // Reactions are ephemeral signals — push to client, don't buffer for polling.
  provider.onReaction = (reaction: IncomingReaction) => {
    server.server
      .sendLoggingMessage({
        level: 'info',
        logger: 'incoming_reaction',
        data: reaction,
      })
      .catch((err: unknown) => {
        console.error(
          JSON.stringify({
            level: 'error',
            task: 'channel-core.onReaction.notify',
            err: err instanceof Error ? err.message : err,
            msg: 'floating-promise',
          }),
        );
      });
  };

  // ── Core messaging ──────────────────────────────────────────────────

  server.tool(
    'send_message',
    'Send a message to a chat or group',
    { chat_id: z.string(), text: z.string() },
    async (args) => {
      if (!provider.isConnected() && provider.waitForReady) {
        await Promise.race([
          provider.waitForReady(),
          new Promise((resolve) => setTimeout(resolve, 15_000)),
        ]);
      }
      await provider.sendMessage(args.chat_id, args.text);
      return { content: [{ type: 'text' as const, text: 'Message sent.' }] };
    },
  );

  server.tool(
    'send_typing',
    'Show or hide typing indicator',
    { chat_id: z.string(), is_typing: z.boolean() },
    async (args) => {
      if (provider.setTyping) {
        await provider.setTyping(args.chat_id, args.is_typing);
      }
      return { content: [{ type: 'text' as const, text: 'OK' }] };
    },
  );

  // ── Status and discovery ────────────────────────────────────────────

  server.tool(
    'get_status',
    'Get connection status and channel info. Pass select="connected,channel" + compact=true for a slimmer response.',
    { compact: z.boolean().optional(), select: z.string().optional() },
    async (args) => {
      // If the provider is still connecting, wait briefly for it to be ready
      if (!provider.isConnected() && provider.waitForReady) {
        await Promise.race([
          provider.waitForReady(),
          new Promise((resolve) => setTimeout(resolve, 15_000)),
        ]);
      }
      const status = provider.getStatus();
      return mcpResponse(status, {
        compact: args.compact,
        select: args.select,
      });
    },
  );

  server.tool(
    'list_chats',
    'List known chats and groups. Pass select="id,name,is_group" + compact=true to cut payload on list ops.',
    { compact: z.boolean().optional(), select: z.string().optional() },
    async (args) => {
      const chats = provider.listChats ? await provider.listChats() : [];
      return mcpResponse(chats, { compact: args.compact, select: args.select });
    },
  );

  server.tool(
    'sync_groups',
    'Refresh group and chat metadata from the platform. Pass select="id,name,is_group" + compact=true to slim the returned groups list.',
    {
      force: z.boolean().optional(),
      compact: z.boolean().optional(),
      select: z.string().optional(),
    },
    async (args) => {
      if (!provider.isConnected() && provider.waitForReady) {
        await Promise.race([
          provider.waitForReady(),
          new Promise((resolve) => setTimeout(resolve, 15_000)),
        ]);
      }
      const groups = provider.syncGroups ? await provider.syncGroups() : [];
      return mcpResponse(groups, {
        compact: args.compact,
        select: args.select,
      });
    },
  );

  // ── Polling fallback ────────────────────────────────────────────────

  server.tool(
    'get_new_messages',
    'Poll for incoming messages since the last call. Use the returned cursor for subsequent calls. Pass select="messages.sender,messages.content,messages.timestamp,cursor" + compact=true to cut polling-loop payload.',
    {
      since_cursor: z.string().optional(),
      compact: z.boolean().optional(),
      select: z.string().optional(),
    },
    async (args) => {
      const result = buffer.getSince(args.since_cursor);
      return mcpResponse(result, {
        compact: args.compact,
        select: args.select,
      });
    },
  );

  // ── Lifecycle ───────────────────────────────────────────────────────

  server.tool('connect', 'Connect to the messaging platform', {}, async () => {
    try {
      await provider.connect();
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        `${provider.name}.connect`,
      );
    }
    return { content: [{ type: 'text' as const, text: 'Connected.' }] };
  });

  server.tool(
    'disconnect',
    'Disconnect from the messaging platform',
    {},
    async () => {
      try {
        await provider.disconnect();
      } catch (err: unknown) {
        return mcpError(
          McpErrorCode.API_ERROR,
          err instanceof Error ? err.message : String(err),
          `${provider.name}.disconnect`,
        );
      }
      return { content: [{ type: 'text' as const, text: 'Disconnected.' }] };
    },
  );

  return buffer;
}
