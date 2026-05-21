#!/usr/bin/env node

/**
 * X (Twitter) MCP Server
 *
 * Standalone MCP server that provides X (Twitter) action tools.
 * Communicates via stdio (JSON-RPC). Can be used by any MCP client.
 *
 * Config (env vars):
 *   X_API_KEY            — Twitter API v2 consumer key
 *   X_API_SECRET         — Twitter API v2 consumer secret
 *   X_ACCESS_TOKEN       — OAuth 1.0a user access token
 *   X_ACCESS_TOKEN_SECRET — OAuth 1.0a user access token secret
 *   LOG_LEVEL            — pino log level (default: info)
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { mcpResponse, withMcpError } from '@deus-ai/channel-core';
import { z } from 'zod';

import { XClient } from './x.js';

const server = new McpServer(
  { name: '@deus-ai/x-mcp', version: '1.0.0' },
  { capabilities: { logging: {} } },
);

const x = new XClient();

// ── Post ──────────────────────────────────────────────────────────────

server.tool(
  'post_tweet',
  'Post a new tweet. Pass select="id,url" + compact=true to slim the returned TweetResult.',
  {
    text: z.string().max(280, 'Tweet must be 280 characters or fewer'),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) =>
    withMcpError('x.post_tweet', () => x.postTweet(args.text), {
      compact: args.compact,
      select: args.select,
    }),
);

server.tool(
  'reply_to_tweet',
  'Reply to an existing tweet. Pass select="id,url" + compact=true to slim the returned TweetResult.',
  {
    tweet_id: z.string(),
    text: z.string().max(280, 'Tweet must be 280 characters or fewer'),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) =>
    withMcpError(
      'x.reply_to_tweet',
      () => x.replyToTweet(args.tweet_id, args.text),
      { compact: args.compact, select: args.select },
    ),
);

server.tool(
  'quote_tweet',
  'Quote an existing tweet with added commentary. Pass select="id,url" + compact=true to slim the returned TweetResult.',
  {
    tweet_id: z.string(),
    text: z.string().max(280, 'Tweet must be 280 characters or fewer'),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) =>
    withMcpError(
      'x.quote_tweet',
      () => x.quoteTweet(args.tweet_id, args.text),
      { compact: args.compact, select: args.select },
    ),
);

// ── Engage ────────────────────────────────────────────────────────────

server.tool(
  'like_tweet',
  'Like a tweet',
  { tweet_id: z.string() },
  async (args) => {
    await x.likeTweet(args.tweet_id);
    return { content: [{ type: 'text' as const, text: 'Liked.' }] };
  },
);

server.tool(
  'unlike_tweet',
  'Remove a like from a tweet',
  { tweet_id: z.string() },
  async (args) => {
    await x.unlikeTweet(args.tweet_id);
    return { content: [{ type: 'text' as const, text: 'Like removed.' }] };
  },
);

server.tool(
  'retweet',
  'Retweet a tweet',
  { tweet_id: z.string() },
  async (args) => {
    await x.retweet(args.tweet_id);
    return { content: [{ type: 'text' as const, text: 'Retweeted.' }] };
  },
);

server.tool(
  'undo_retweet',
  'Undo a retweet',
  { tweet_id: z.string() },
  async (args) => {
    await x.undoRetweet(args.tweet_id);
    return { content: [{ type: 'text' as const, text: 'Retweet removed.' }] };
  },
);

// ── Read ──────────────────────────────────────────────────────────────

server.tool(
  'get_timeline',
  'Get recent tweets from your home timeline. Pass select="id,url" + compact=true to cut payload on list ops.',
  {
    count: z.number().int().min(1).max(100).optional().default(10),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) =>
    withMcpError('x.get_timeline', () => x.getTimeline(args.count), {
      compact: args.compact,
      select: args.select,
    }),
);

server.tool(
  'search_tweets',
  'Search recent tweets by keyword or query. Pass select="id,url" + compact=true to cut payload on list ops.',
  {
    query: z.string(),
    count: z.number().int().min(10).max(100).optional().default(10),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) =>
    withMcpError(
      'x.search_tweets',
      () => x.searchTweets(args.query, args.count),
      { compact: args.compact, select: args.select },
    ),
);

server.tool(
  'get_tweet',
  'Get a single tweet by ID. Pass select="id,url" + compact=true to slim the response.',
  {
    tweet_id: z.string(),
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) =>
    withMcpError('x.get_tweet', () => x.getTweet(args.tweet_id), {
      compact: args.compact,
      select: args.select,
    }),
);

server.tool(
  'get_my_profile',
  'Get your own X profile info. Pass select="id,username,name" + compact=true for a slimmer response.',
  {
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) =>
    withMcpError('x.get_my_profile', () => x.getMyProfile(), {
      compact: args.compact,
      select: args.select,
    }),
);

server.tool(
  'get_status',
  'Check whether X credentials are configured. Pass select="configured" + compact=true for a minimal response.',
  {
    compact: z.boolean().optional(),
    select: z.string().optional(),
  },
  async (args) => {
    // No try/catch needed — x.isConfigured() is pure (no API call).
    const configured = x.isConfigured();
    return mcpResponse(
      { configured, channel: 'x' },
      { compact: args.compact, select: args.select },
    );
  },
);

// ── Start ─────────────────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
