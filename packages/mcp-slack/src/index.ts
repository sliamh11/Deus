#!/usr/bin/env node

/**
 * Slack MCP Server
 *
 * Standalone MCP server that provides Slack bot messaging tools.
 * Communicates via stdio (JSON-RPC). Can be used by any MCP client.
 *
 * Uses Socket Mode — no public URL needed.
 *
 * Config (env vars):
 *   SLACK_BOT_TOKEN  — Slack bot token (xoxb-...)
 *   SLACK_APP_TOKEN  — Slack app-level token (xapp-...) for Socket Mode
 *   ASSISTANT_NAME   — bot display name (default: Deus)
 *   LOG_LEVEL        — pino log level (default: info)
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { registerCommonTools } from '@deus-ai/channel-core';

import { SlackProvider } from './slack.js';

const server = new McpServer({
  name: '@deus-ai/slack-mcp',
  version: '1.0.0',
});

const provider = new SlackProvider();

// Register common tools (send_message, get_status, etc.)
registerCommonTools(server, provider);

// ── Auto-connect if tokens are configured ────────────────────────────

if (provider.hasTokens()) {
  provider.connect().catch((err) => {
    console.error('[@deus-ai/slack-mcp] Auto-connect failed:', err.message);
  });
}

// ── Start MCP transport ──────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
