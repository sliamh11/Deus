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
import pino from 'pino';
import { registerCommonTools } from '@deus-ai/channel-core';

import { DiscordProvider } from './discord.js';

const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

const server = new McpServer(
  { name: '@deus-ai/discord-mcp', version: '1.0.0' },
  { capabilities: { logging: {} } },
);

const provider = new DiscordProvider();

// Register common tools (send_message, get_status, etc.)
registerCommonTools(server, provider);

// ── Auto-connect if token is configured ───────────────────────────────

if (provider.hasToken()) {
  provider.connect().catch((err: unknown) => {
    logger.error(
      { err, source: 'discord.auto-connect' },
      'provider connect failed at startup',
    );
  });
}

// ── Start MCP transport ───────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
