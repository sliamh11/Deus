#!/usr/bin/env node

/**
 * Telegram MCP Server
 *
 * Standalone MCP server that provides Telegram bot messaging tools.
 * Communicates via stdio (JSON-RPC). Can be used by any MCP client.
 *
 * Config (env vars):
 *   TELEGRAM_BOT_TOKEN — Telegram bot token from @BotFather
 *   ASSISTANT_NAME     — bot display name (default: Deus)
 *   LOG_LEVEL          — pino log level (default: info)
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { registerCommonTools } from 'deus-mcp-channel-core';

import { TelegramProvider } from './telegram.js';

const server = new McpServer({
  name: 'deus-mcp-telegram',
  version: '1.0.0',
});

const provider = new TelegramProvider();

// Register common tools (send_message, get_status, etc.)
registerCommonTools(server, provider);

// ── Auto-connect if token is configured ───────────────────────────────

if (provider.hasToken()) {
  provider.connect().catch((err) => {
    console.error('[deus-mcp-telegram] Auto-connect failed:', err.message);
  });
}

// ── Start MCP transport ───────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
