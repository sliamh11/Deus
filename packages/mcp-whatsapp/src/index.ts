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
import pino from 'pino';
import { z } from 'zod';
import { registerCommonTools } from '@deus-ai/channel-core';

import { WhatsAppProvider } from './whatsapp.js';

const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

const server = new McpServer(
  { name: '@deus-ai/whatsapp-mcp', version: '1.0.0' },
  { capabilities: { logging: {} } },
);

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
      try {
        await provider.connect();
      } catch (err: unknown) {
        logger.error(
          { err, source: 'whatsapp.start_auth.connect' },
          'provider connect failed during start_auth',
        );
        return {
          content: [
            {
              type: 'text' as const,
              text: JSON.stringify({
                error: 'connect failed during start_auth',
                detail: err instanceof Error ? err.message : String(err),
                source: 'whatsapp.start_auth.connect',
              }),
            },
          ],
          isError: true,
        };
      }
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
  provider.connect().catch((err: unknown) => {
    // Log to stderr — don't crash, stay available for auth tools
    logger.error(
      { err, source: 'whatsapp.auto-connect' },
      'provider connect failed at startup',
    );
  });
}

// ── Start MCP transport ───────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
