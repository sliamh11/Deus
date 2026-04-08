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
  'Post a new tweet',
  { text: z.string().max(280, 'Tweet must be 280 characters or fewer') },
  async (args) => {
    const result = await x.postTweet(args.text);
    return {
      content: [{ type: 'text' as const, text: JSON.stringify(result) }],
    };
  },
);

server.tool(
  'reply_to_tweet',
  'Reply to an existing tweet',
  {
    tweet_id: z.string(),
    text: z.string().max(280, 'Tweet must be 280 characters or fewer'),
  },
  async (args) => {
    const result = await x.replyToTweet(args.tweet_id, args.text);
    return {
      content: [{ type: 'text' as const, text: JSON.stringify(result) }],
    };
  },
);

server.tool(
  'quote_tweet',
  'Quote an existing tweet with added commentary',
  {
    tweet_id: z.string(),
    text: z.string().max(280, 'Tweet must be 280 characters or fewer'),
  },
  async (args) => {
    const result = await x.quoteTweet(args.tweet_id, args.text);
    return {
      content: [{ type: 'text' as const, text: JSON.stringify(result) }],
    };
  },
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
  'Get recent tweets from your home timeline',
  { count: z.number().int().min(1).max(100).optional().default(10) },
  async (args) => {
    const tweets = await x.getTimeline(args.count);
    return {
      content: [{ type: 'text' as const, text: JSON.stringify(tweets) }],
    };
  },
);

server.tool(
  'search_tweets',
  'Search recent tweets by keyword or query',
  {
    query: z.string(),
    count: z.number().int().min(10).max(100).optional().default(10),
  },
  async (args) => {
    const tweets = await x.searchTweets(args.query, args.count);
    return {
      content: [{ type: 'text' as const, text: JSON.stringify(tweets) }],
    };
  },
);

server.tool(
  'get_tweet',
  'Get a single tweet by ID',
  { tweet_id: z.string() },
  async (args) => {
    const tweet = await x.getTweet(args.tweet_id);
    return {
      content: [{ type: 'text' as const, text: JSON.stringify(tweet) }],
    };
  },
);

server.tool('get_my_profile', 'Get your own X profile info', {}, async () => {
  const profile = await x.getMyProfile();
  return {
    content: [{ type: 'text' as const, text: JSON.stringify(profile) }],
  };
});

server.tool(
  'get_status',
  'Check whether X credentials are configured',
  {},
  async () => {
    const configured = x.isConfigured();
    return {
      content: [
        {
          type: 'text' as const,
          text: JSON.stringify({ configured, channel: 'x' }),
        },
      ],
    };
  },
);

// ── Start ─────────────────────────────────────────────────────────────

const transport = new StdioServerTransport();
await server.connect(transport);
