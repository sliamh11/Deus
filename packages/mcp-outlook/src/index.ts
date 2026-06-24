#!/usr/bin/env node

/**
 * Outlook (Microsoft 365) MCP Server
 *
 * Standalone MCP server that provides Outlook messaging tools over Microsoft Graph.
 * Communicates via stdio (JSON-RPC). Can be used by any MCP client.
 *
 * Config (env vars):
 *   OUTLOOK_CREDENTIALS_DIR  — directory with app-credentials.json + token.json (default: ~/.outlook-mcp/)
 *   OUTLOOK_POLL_INTERVAL_MS — polling interval in ms (default: 60000)
 *   LOG_LEVEL                — pino log level (default: info)
 *
 * One-time sign-in (device-code flow):
 *   node dist/index.js auth
 */

import fs from 'fs';

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

import {
  OutlookProvider,
  buildMsalClient,
  CREDENTIALS_DIR,
  SCOPES,
  appCredentialsPath,
} from './outlook.js';

const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

// ── Device-code auth subcommand ──────────────────────────────────────
// `node dist/index.js auth` signs in once and seeds the persisted token cache.
// On error it throws (no upstream catcher → the top-level await rejects and the
// CLI exits non-zero); on success it returns and, since no MCP transport is
// started in this mode, the process ends cleanly. process.exit is avoided per
// the packages/* error-discipline rule (docs/decisions/error-discipline.md).
async function runAuth(): Promise<void> {
  if (!fs.existsSync(appCredentialsPath())) {
    throw new Error(
      `app-credentials.json not found in ${CREDENTIALS_DIR} — create it with clientId + tenantId first`,
    );
  }
  const creds = JSON.parse(fs.readFileSync(appCredentialsPath(), 'utf-8'));
  const msal = buildMsalClient(creds);
  await msal.acquireTokenByDeviceCode({
    scopes: SCOPES,
    deviceCodeCallback: (resp) => {
      // Print to stderr so it is visible without polluting MCP stdout.
      process.stderr.write(`\n${resp.message}\n\n`);
    },
  });
  logger.info(
    { dir: CREDENTIALS_DIR },
    'Outlook sign-in complete; token cache written',
  );
}

const server = new McpServer(
  { name: '@deus-ai/outlook-mcp', version: '1.0.0' },
  { capabilities: { logging: {} } },
);

const provider = new OutlookProvider();

// Register common tools (send_message, get_status, etc.)
registerCommonTools(server, provider);

// ── Outlook-specific tools ───────────────────────────────────────────

server.tool(
  'read_email',
  'Read a full email by message ID. Pass select="from,subject,body" + compact=true when fields are not all needed.',
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
        'outlook.read_email',
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
    try {
      await provider.sendEmail(args.to, args.subject, args.body);
      return { content: [{ type: 'text' as const, text: 'Email sent.' }] };
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        'outlook.send_email',
      );
    }
  },
);

server.tool(
  'search_emails',
  'Search emails by Microsoft Graph query string (e.g. "from:user@example.com hello"). Pass select="id,conversationId,snippet" + compact=true to cut payload on search.',
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
        'outlook.search_emails',
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
        'outlook.draft_email',
      );
    }
  },
);

// ── Entry: auth subcommand, or start the MCP server ──────────────────

if (process.argv[2] === 'auth') {
  await runAuth();
} else {
  // Auto-connect if credentials exist.
  if (provider.hasCredentials()) {
    provider.connect().catch((err: unknown) => {
      logger.error(
        { err, source: 'outlook.auto-connect' },
        'provider connect failed at startup',
      );
    });
  }

  // Start MCP transport.
  const transport = new StdioServerTransport();
  await server.connect(transport);
}
