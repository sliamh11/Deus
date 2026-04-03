#!/usr/bin/env node

/**
 * WhatsApp MCP Server
 *
 * Standalone MCP server that provides WhatsApp messaging tools.
 * Communicates via stdio (JSON-RPC). Can be used by any MCP client.
 *
 * Config (env vars):
 *   WHATSAPP_AUTH_DIR  — path to auth credentials (default: ./store/auth)
 *   ASSISTANT_NAME     — bot display name (default: Deus)
 *   ASSISTANT_HAS_OWN_NUMBER — "true" if bot has a dedicated phone number
 *   LOG_LEVEL          — pino log level (default: info)
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';
import { registerCommonTools } from 'deus-mcp-channel-core';

import { WhatsAppProvider } from './whatsapp.js';

const server = new McpServer({
  name: 'deus-mcp-whatsapp',
  version: '1.0.0',
});

const provider = new WhatsAppProvider();

// Register common tools (send_message, get_status, etc.)
registerCommonTools(server, provider);

// ── WhatsApp-specific tools ───────────────────────────────────────────

server.tool(
  'get_auth_status',
  'Check whether WhatsApp credentials exist and the connection is authenticated',
  {},
  async () => {
    const hasAuth = provider.hasAuth();
    const connected = provider.isConnected();
    return {
      content: [
        {
          type: 'text' as const,
          text: JSON.stringify({ has_credentials: hasAuth, connected }),
        },
      ],
    };
  },
);

server.tool(
  'start_auth',
  'Begin WhatsApp authentication. Returns QR code data or pairing code.',
  {
    method: z.enum(['qr', 'pairing-code']).describe('Authentication method'),
    phone: z
      .string()
      .optional()
      .describe(
        'Phone number (required for pairing-code method, e.g. 14155551234)',
      ),
  },
  async (args) => {
    if (args.method === 'pairing-code' && !args.phone) {
      return {
        content: [
          {
            type: 'text' as const,
            text: JSON.stringify({
              error: 'Phone number required for pairing-code method',
            }),
          },
        ],
        isError: true,
      };
    }
    // Auth is handled by the connect flow — this tool triggers it.
    // The provider writes QR data to disk; the client reads it.
    if (!provider.isConnected()) {
      await provider.connect();
    }
    const status = provider.getStatus();
    return {
      content: [
        {
          type: 'text' as const,
          text: JSON.stringify({
            status: status.connected ? 'connected' : 'authenticating',
            identity: status.identity,
          }),
        },
      ],
    };
  },
);

// ── Auto-connect if credentials exist ─────────────────────────────────

if (provider.hasAuth()) {
  provider.connect().catch((err) => {
    // Log to stderr — don't crash, stay available for auth tools
    console.error('[deus-mcp-whatsapp] Auto-connect failed:', err.message);
  });
}

// ── Start MCP transport ───────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
