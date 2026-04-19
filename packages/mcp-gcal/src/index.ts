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
  'List upcoming calendar events',
  {
    days: z
      .number()
      .optional()
      .describe('Number of days to look ahead (default: 7)'),
  },
  async (args) => {
    const events = await provider.listEvents(args.days ?? 7);
    return {
      content: [
        { type: 'text' as const, text: JSON.stringify(events, null, 2) },
      ],
    };
  },
);

server.tool(
  'get_event',
  'Get a single calendar event by ID',
  { event_id: z.string().describe('The event ID') },
  async (args) => {
    const event = await provider.getEvent(args.event_id);
    return {
      content: [
        { type: 'text' as const, text: JSON.stringify(event, null, 2) },
      ],
    };
  },
);

server.tool(
  'create_event',
  'Create a new calendar event',
  {
    title: z.string().describe('Event title'),
    start: z
      .string()
      .describe('Start time (ISO 8601, e.g. "2026-04-07T14:00:00")'),
    end: z.string().optional().describe('End time (default: start + 1 hour)'),
    description: z.string().optional().describe('Event description'),
    location: z.string().optional().describe('Event location'),
  },
  async (args) => {
    const event = await provider.createEvent({
      title: args.title,
      start: args.start,
      end: args.end,
      description: args.description,
      location: args.location,
    });
    return {
      content: [
        { type: 'text' as const, text: JSON.stringify(event, null, 2) },
      ],
    };
  },
);

server.tool(
  'update_event',
  'Update an existing calendar event',
  {
    event_id: z.string().describe('The event ID to update'),
    title: z.string().optional().describe('New title'),
    start: z.string().optional().describe('New start time (ISO 8601)'),
    end: z.string().optional().describe('New end time (ISO 8601)'),
    description: z.string().optional().describe('New description'),
    location: z.string().optional().describe('New location'),
  },
  async (args) => {
    const event = await provider.updateEvent(args.event_id, {
      title: args.title,
      start: args.start,
      end: args.end,
      description: args.description,
      location: args.location,
    });
    return {
      content: [
        { type: 'text' as const, text: JSON.stringify(event, null, 2) },
      ],
    };
  },
);

server.tool(
  'delete_event',
  'Delete a calendar event',
  { event_id: z.string().describe('The event ID to delete') },
  async (args) => {
    await provider.deleteEvent(args.event_id);
    return {
      content: [
        { type: 'text' as const, text: `Deleted event ${args.event_id}` },
      ],
    };
  },
);

server.tool(
  'search_events',
  'Search calendar events by text',
  {
    query: z.string().describe('Search text'),
    days: z
      .number()
      .optional()
      .describe('Number of days to search (default: 30)'),
  },
  async (args) => {
    const events = await provider.searchEvents(args.query, args.days ?? 30);
    return {
      content: [
        { type: 'text' as const, text: JSON.stringify(events, null, 2) },
      ],
    };
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
