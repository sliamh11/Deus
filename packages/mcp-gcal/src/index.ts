#!/usr/bin/env node

/**
 * Google Calendar MCP Server
 *
 * Standalone MCP server exposing Google Calendar tools.
 * Communicates via stdio (JSON-RPC). Can be used by any MCP client.
 *
 * Config (env vars):
 *   GCAL_CREDENTIALS_PATH — OAuth client file (default: integrations/gcal/credentials.json)
 *   GCAL_TOKENS_PATH      — OAuth tokens file (default: integrations/gcal/tokens.json)
 *   DEUS_PROJECT_ROOT     — project root for resolving default paths
 *   LOG_LEVEL             — pino log level (default: info)
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import pino from 'pino';
import { z } from 'zod';

import { GCalProvider } from './gcal.js';
import { mcpError, McpErrorCode, mcpResponse } from '@deus-ai/channel-core';

const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

const server = new McpServer(
  { name: '@deus-ai/gcal-mcp', version: '1.0.0' },
  { capabilities: { logging: {} } },
);

const provider = new GCalProvider();

// ── Calendar tools ──────────────────────────────────────────────────

server.tool(
  'list_events',
  'List upcoming calendar events. Pass select="id,start,summary" + compact=true on list ops to cut payload ~60%.',
  {
    days: z
      .number()
      .optional()
      .describe('Number of days to look ahead (default: 7)'),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => {
    try {
      const events = await provider.listEvents(args.days ?? 7);
      return mcpResponse(events, {
        compact: args.compact,
        select: args.select,
      });
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        'gcal.list_events',
      );
    }
  },
);

server.tool(
  'get_event',
  'Get a single calendar event by ID. Pass select="id,start,summary,location" + compact=true to slim the response.',
  {
    event_id: z.string().describe('The event ID'),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => {
    try {
      const event = await provider.getEvent(args.event_id);
      return mcpResponse(event, { compact: args.compact, select: args.select });
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        'gcal.get_event',
      );
    }
  },
);

server.tool(
  'create_event',
  'Create a new calendar event. The returned event accepts select/compact to limit fields in the response payload.',
  {
    title: z.string().describe('Event title'),
    start: z
      .string()
      .describe('Start time (ISO 8601, e.g. "2026-04-07T14:00:00")'),
    end: z.string().optional().describe('End time (default: start + 1 hour)'),
    description: z.string().optional().describe('Event description'),
    location: z.string().optional().describe('Event location'),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => {
    try {
      const event = await provider.createEvent({
        title: args.title,
        start: args.start,
        end: args.end,
        description: args.description,
        location: args.location,
      });
      return mcpResponse(event, { compact: args.compact, select: args.select });
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        'gcal.create_event',
      );
    }
  },
);

server.tool(
  'update_event',
  'Update an existing calendar event. The returned event accepts select/compact to limit fields in the response payload.',
  {
    event_id: z.string().describe('The event ID to update'),
    title: z.string().optional().describe('New title'),
    start: z.string().optional().describe('New start time (ISO 8601)'),
    end: z.string().optional().describe('New end time (ISO 8601)'),
    description: z.string().optional().describe('New description'),
    location: z.string().optional().describe('New location'),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => {
    try {
      const event = await provider.updateEvent(args.event_id, {
        title: args.title,
        start: args.start,
        end: args.end,
        description: args.description,
        location: args.location,
      });
      return mcpResponse(event, { compact: args.compact, select: args.select });
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        'gcal.update_event',
      );
    }
  },
);

server.tool(
  'delete_event',
  'Delete a calendar event',
  { event_id: z.string().describe('The event ID to delete') },
  async (args) => {
    try {
      await provider.deleteEvent(args.event_id);
      return mcpResponse({ deleted: args.event_id });
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        'gcal.delete_event',
      );
    }
  },
);

server.tool(
  'search_events',
  'Search calendar events by text. Pass select="id,start,summary" + compact=true on list-of-matches to cut payload.',
  {
    query: z.string().describe('Search text'),
    days: z
      .number()
      .optional()
      .describe('Number of days to search (default: 30)'),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => {
    try {
      const events = await provider.searchEvents(args.query, args.days ?? 30);
      return mcpResponse(events, {
        compact: args.compact,
        select: args.select,
      });
    } catch (err: unknown) {
      return mcpError(
        McpErrorCode.API_ERROR,
        err instanceof Error ? err.message : String(err),
        'gcal.search_events',
      );
    }
  },
);

// ── Auto-connect if credentials exist ───────────────────────────────

if (provider.hasCredentials()) {
  provider.connect().catch((err: unknown) => {
    logger.error(
      { err, source: 'gcal.auto-connect' },
      'provider connect failed at startup',
    );
  });
}

// ── Start MCP transport ─────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
