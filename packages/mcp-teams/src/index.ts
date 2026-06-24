#!/usr/bin/env node

/**
 * Microsoft Teams MCP Server
 *
 * Standalone MCP server that provides Microsoft Teams bot messaging tools via the
 * Azure Bot Service (Bot Framework). Communicates via stdio (JSON-RPC). Can be
 * used by any MCP client.
 *
 * The bot serves POST /api/messages on TEAMS_PORT; that port must be exposed
 * publicly (a dedicated tunnel) and set as the Azure Bot messaging endpoint.
 *
 * Config (env vars):
 *   TEAMS_APP_ID         — Azure Bot / Entra app (client) ID
 *   TEAMS_APP_PASSWORD   — client secret value
 *   TEAMS_APP_TENANT_ID  — directory (tenant) ID (single-tenant); omit for multi-tenant
 *   TEAMS_PORT           — messaging-endpoint port (default: 3978)
 *   TEAMS_CREDENTIALS_DIR — where conversation references persist (default: ~/.teams-mcp/)
 *   LOG_LEVEL            — pino log level (default: info)
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import pino from 'pino';
import { registerCommonTools } from '@deus-ai/channel-core';

import { TeamsProvider } from './teams.js';

const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

const server = new McpServer(
  { name: '@deus-ai/teams-mcp', version: '1.0.0' },
  { capabilities: { logging: {} } },
);

const provider = new TeamsProvider();

// Register common tools (send_message, get_status, etc.)
registerCommonTools(server, provider);

// ── Auto-connect if credentials are configured ───────────────────────

if (provider.hasCredentials()) {
  provider.connect().catch((err: unknown) => {
    logger.error(
      { err, source: 'teams.auto-connect' },
      'provider connect failed at startup',
    );
  });
}

// ── Start MCP transport ──────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
