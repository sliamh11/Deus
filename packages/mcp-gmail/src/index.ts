#!/usr/bin/env node

/**
 * Gmail MCP Server
 *
 * Standalone MCP server that provides Gmail messaging tools.
 * Communicates via stdio (JSON-RPC). Can be used by any MCP client.
 *
 * Config (env vars):
 *   GMAIL_CREDENTIALS_DIR — directory containing OAuth keys and tokens (default: ~/.gmail-mcp/)
 *   GMAIL_POLL_INTERVAL_MS — polling interval in ms (default: 60000)
 *   LOG_LEVEL             — pino log level (default: info)
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import pino from 'pino';
import {
  mcpError,
  McpErrorCode,
  mcpResponse,
  registerCommonTools,
} from '@deus-ai/channel-core';
import { z } from 'zod';

import { GmailProvider } from './gmail.js';

const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

const server = new McpServer(
  { name: '@deus-ai/gmail-mcp', version: '1.0.0' },
  { capabilities: { logging: {} } },
);

const provider = new GmailProvider();

// Register common tools (send_message, get_status, etc.)
registerCommonTools(server, provider);

// ── Gmail-specific tools ─────────────────────────────────────────────

server.tool(
  'read_email',
  'Read a full email by message ID. Pass select="from,subject,snippet,date" + compact=true when body is not needed.',
  {
    message_id: z.string(),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => {
    try {
      const email = await provider.readEmail(args.message_id);
      return mcpResponse(email, { compact: args.compact, select: args.select });
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        'gmail.read_email',
      );
    }
  },
);

server.tool(
  'send_email',
  'Send a new email (not a thread reply)',
  {
    to: z.string(),
    subject: z.string(),
    body: z.string(),
  },
  async (args) => {
    await provider.sendEmail(args.to, args.subject, args.body);
    return { content: [{ type: 'text' as const, text: 'Email sent.' }] };
  },
);

server.tool(
  'search_emails',
  'Search emails by Gmail query string (e.g. "from:user@example.com subject:hello"). Pass select="id,from,subject,snippet" + compact=true on search to cut payload.',
  {
    query: z.string(),
    max_results: z.number().optional(),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => {
    try {
      const results = await provider.searchEmails(
        args.query,
        args.max_results ?? 10,
      );
      return mcpResponse(results, {
        compact: args.compact,
        select: args.select,
      });
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        'gmail.search_emails',
      );
    }
  },
);

server.tool(
  'draft_email',
  'Create a draft email',
  {
    to: z.string(),
    subject: z.string(),
    body: z.string(),
  },
  async (args) => {
    try {
      const draftId = await provider.draftEmail(
        args.to,
        args.subject,
        args.body,
      );
      return mcpResponse({ draft_id: draftId });
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        'gmail.draft_email',
      );
    }
  },
);

// ── Auto-connect if credentials exist ────────────────────────────────

if (provider.hasCredentials()) {
  provider.connect().catch((err: unknown) => {
    logger.error(
      { err, source: 'gmail.auto-connect' },
      'provider connect failed at startup',
    );
  });
}

// ── Start MCP transport ──────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
