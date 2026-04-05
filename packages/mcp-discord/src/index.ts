#!/usr/bin/env node

/**
 * Discord MCP Server
 *
 * Standalone MCP server that provides Discord bot messaging tools.
 * Communicates via stdio (JSON-RPC). Can be used by any MCP client.
 *
 * Config (env vars):
 *   DISCORD_BOT_TOKEN — Discord bot token from the Developer Portal
 *   ASSISTANT_NAME    — bot display name (default: Deus)
 *   LOG_LEVEL         — pino log level (default: info)
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { registerCommonTools } from '@deus-ai/channel-core';

import { DiscordProvider } from './discord.js';

const server = new McpServer({
  name: '@deus-ai/discord-mcp',
  version: '1.0.0',
});

const provider = new DiscordProvider();

// Register common tools (send_message, get_status, etc.)
registerCommonTools(server, provider);

// ── Auto-connect if token is configured ───────────────────────────────

if (provider.hasToken()) {
  provider.connect().catch((err) => {
    console.error('[@deus-ai/discord-mcp] Auto-connect failed:', err.message);
  });
}

// ── Start MCP transport ───────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
