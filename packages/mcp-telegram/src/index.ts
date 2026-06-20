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
import pino from 'pino';
import { registerCommonTools } from '@deus-ai/channel-core';

import { TelegramProvider } from './telegram.js';

const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

// Process-level safety net: a stray rejection (e.g. grammy's "Aborted delay"
// from bot.start() during a stop mid-backoff) must not silently kill this MCP
// channel child. Mirror the host process policy (src/index.ts): log unhandled
// rejections without exiting, but exit on a genuine uncaught exception so the
// host orchestrator can respawn a clean process.
process.on('unhandledRejection', (reason) => {
  logger.error({ reason }, 'Unhandled rejection');
});
process.on('uncaughtException', (err) => {
  logger.error({ err }, 'Uncaught exception');
  // eslint-disable-next-line no-restricted-syntax -- MCP server has no upstream catcher for an uncaught exception; exit so the host orchestrator respawns a clean stdio process (same rationale as the resetPolling suicide in telegram.ts).
  process.exit(1);
});

const server = new McpServer(
  { name: '@deus-ai/telegram-mcp', version: '1.0.0' },
  { capabilities: { logging: {} } },
);

const provider = new TelegramProvider();

// Register common tools (send_message, get_status, etc.)
registerCommonTools(server, provider);

// ── Auto-connect if token is configured ───────────────────────────────

if (provider.hasToken()) {
  provider.connect().catch((err: unknown) => {
    logger.error(
      { err, source: 'telegram.auto-connect' },
      'provider connect failed at startup',
    );
  });
}

// ── Start MCP transport ───────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
