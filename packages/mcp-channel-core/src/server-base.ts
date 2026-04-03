import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { z } from 'zod';

import { MessageBuffer } from './message-buffer.js';
import type { ChannelProvider, IncomingMessage } from './types.js';

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

    // Push to MCP client via logging notification (real-time path)
    server.server.sendLoggingMessage({
      level: 'info',
      logger: 'incoming_message',
      data: msg,
    });
  };

  // ── Core messaging ──────────────────────────────────────────────────

  server.tool(
    'send_message',
    'Send a message to a chat or group',
    { chat_id: z.string(), text: z.string() },
    async (args) => {
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
    'Get connection status and channel info',
    {},
    async () => {
      const status = provider.getStatus();
      return {
        content: [{ type: 'text' as const, text: JSON.stringify(status) }],
      };
    },
  );

  server.tool('list_chats', 'List known chats and groups', {}, async () => {
    const chats = provider.listChats ? await provider.listChats() : [];
    return {
      content: [{ type: 'text' as const, text: JSON.stringify(chats) }],
    };
  });

  server.tool(
    'sync_groups',
    'Refresh group and chat metadata from the platform',
    { force: z.boolean().optional() },
    async () => {
      const groups = provider.syncGroups ? await provider.syncGroups() : [];
      return {
        content: [{ type: 'text' as const, text: JSON.stringify(groups) }],
      };
    },
  );

  // ── Polling fallback ────────────────────────────────────────────────

  server.tool(
    'get_new_messages',
    'Poll for incoming messages since the last call. Use the returned cursor for subsequent calls.',
    { since_cursor: z.string().optional() },
    async (args) => {
      const result = buffer.getSince(args.since_cursor);
      return {
        content: [{ type: 'text' as const, text: JSON.stringify(result) }],
      };
    },
  );

  // ── Lifecycle ───────────────────────────────────────────────────────

  server.tool('connect', 'Connect to the messaging platform', {}, async () => {
    await provider.connect();
    return { content: [{ type: 'text' as const, text: 'Connected.' }] };
  });

  server.tool(
    'disconnect',
    'Disconnect from the messaging platform',
    {},
    async () => {
      await provider.disconnect();
      return { content: [{ type: 'text' as const, text: 'Disconnected.' }] };
    },
  );

  return buffer;
}
