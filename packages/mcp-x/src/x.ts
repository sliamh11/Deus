/**
 * X (Twitter) API v2 client.
 * Wraps twitter-api-v2 for use as MCP tools.
 * All credentials come from env vars — never hardcoded.
 */

import pino from 'pino';
import { TwitterApi } from 'twitter-api-v2';

const logger = pino(
  { level: process.env.LOG_LEVEL || 'info' },
  pino.destination(2),
);

export interface TweetResult {
  id: string;
  text: string;
  url: string;
}

export interface UserProfile {
  id: string;
  username: string;
  name: string;
  description?: string;
  public_metrics?: {
    followers_count: number;
    following_count: number;
    tweet_count: number;
  };
}

export class XClient {
  private client: TwitterApi | null = null;
  private myUsername: string | null = null;
  private myId: string | null = null;

  constructor() {
    const apiKey = process.env.X_API_KEY;
    const apiSecret = process.env.X_API_SECRET;
    const accessToken = process.env.X_ACCESS_TOKEN;
    const accessTokenSecret = process.env.X_ACCESS_TOKEN_SECRET;

    if (!apiKey || !apiSecret || !accessToken || !accessTokenSecret) {
      logger.warn(
        'X credentials not configured. Set X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET.',
      );
      return;
    }

    this.client = new TwitterApi({
      appKey: apiKey,
      appSecret: apiSecret,
      accessToken,
      accessSecret: accessTokenSecret,
    });
  }

  isConfigured(): boolean {
    return this.client !== null;
  }

  private getClient(): TwitterApi {
    if (!this.client) {
      throw new Error(
        'X credentials not configured. Set X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET.',
      );
    }
    return this.client;
  }

  private tweetUrl(username: string, tweetId: string): string {
    return `https://x.com/${username}/status/${tweetId}`;
  }

  async getMyProfile(): Promise<UserProfile> {
    const client = this.getClient();
    const me = await client.v2.me({
      'user.fields': ['description', 'public_metrics'],
    });
    this.myUsername = me.data.username;
    this.myId = me.data.id;
    return {
      id: me.data.id,
      username: me.data.username,
      name: me.data.name,
      description: me.data.description,
      public_metrics: me.data.public_metrics as UserProfile['public_metrics'],
    };
  }

  async postTweet(text: string): Promise<TweetResult> {
    const client = this.getClient();
    const tweet = await client.v2.tweet(text);
    const username = await this.resolveUsername();
    logger.info({ tweetId: tweet.data.id }, 'Tweet posted');
    return {
      id: tweet.data.id,
      text: tweet.data.text,
      url: this.tweetUrl(username, tweet.data.id),
    };
  }

  async replyToTweet(tweetId: string, text: string): Promise<TweetResult> {
    const client = this.getClient();
    const tweet = await client.v2.tweet(text, {
      reply: { in_reply_to_tweet_id: tweetId },
    });
    const username = await this.resolveUsername();
    logger.info({ tweetId: tweet.data.id, replyTo: tweetId }, 'Reply posted');
    return {
      id: tweet.data.id,
      text: tweet.data.text,
      url: this.tweetUrl(username, tweet.data.id),
    };
  }

  async quoteTweet(tweetId: string, text: string): Promise<TweetResult> {
    const client = this.getClient();
    const tweet = await client.v2.tweet(text, { quote_tweet_id: tweetId });
    const username = await this.resolveUsername();
    logger.info(
      { tweetId: tweet.data.id, quotedId: tweetId },
      'Quote tweet posted',
    );
    return {
      id: tweet.data.id,
      text: tweet.data.text,
      url: this.tweetUrl(username, tweet.data.id),
    };
  }

  async likeTweet(tweetId: string): Promise<void> {
    const client = this.getClient();
    const userId = await this.resolveUserId();
    await client.v2.like(userId, tweetId);
    logger.info({ tweetId }, 'Tweet liked');
  }

  async unlikeTweet(tweetId: string): Promise<void> {
    const client = this.getClient();
    const userId = await this.resolveUserId();
    await client.v2.unlike(userId, tweetId);
    logger.info({ tweetId }, 'Tweet unliked');
  }

  async retweet(tweetId: string): Promise<void> {
    const client = this.getClient();
    const userId = await this.resolveUserId();
    await client.v2.retweet(userId, tweetId);
    logger.info({ tweetId }, 'Retweeted');
  }

  async undoRetweet(tweetId: string): Promise<void> {
    const client = this.getClient();
    const userId = await this.resolveUserId();
    await client.v2.unretweet(userId, tweetId);
    logger.info({ tweetId }, 'Retweet undone');
  }

  async getTimeline(count = 10): Promise<TweetResult[]> {
    const client = this.getClient();
    const userId = await this.resolveUserId();
    const username = await this.resolveUsername();
    const timeline = await client.v2.homeTimeline({
      max_results: Math.min(count, 100),
      'tweet.fields': ['text', 'created_at', 'author_id'],
    });
    return (timeline.data.data ?? []).map((t) => ({
      id: t.id,
      text: t.text,
      url: this.tweetUrl(username, t.id),
    }));
  }

  async searchTweets(query: string, count = 10): Promise<TweetResult[]> {
    const client = this.getClient();
    const results = await client.v2.search(query, {
      max_results: Math.min(Math.max(count, 10), 100),
      'tweet.fields': ['text', 'created_at', 'author_id'],
      expansions: ['author_id'],
      'user.fields': ['username'],
    });

    const users = new Map<string, string>(
      (results.data.includes?.users ?? []).map((u) => [u.id, u.username]),
    );

    return (results.data.data ?? []).map((t) => ({
      id: t.id,
      text: t.text,
      url: this.tweetUrl(users.get(t.author_id ?? '') ?? 'i', t.id),
    }));
  }

  async getTweet(tweetId: string): Promise<TweetResult> {
    const client = this.getClient();
    const tweet = await client.v2.singleTweet(tweetId, {
      'tweet.fields': ['text', 'created_at', 'author_id'],
      expansions: ['author_id'],
      'user.fields': ['username'],
    });
    const username = tweet.includes?.users?.[0]?.username ?? 'i';
    return {
      id: tweet.data.id,
      text: tweet.data.text,
      url: this.tweetUrl(username, tweet.data.id),
    };
  }

  /** Resolve username, fetching from API if not cached. */
  private async resolveUsername(): Promise<string> {
    if (!this.myUsername) {
      await this.getMyProfile();
    }
    return this.myUsername!;
  }

  /** Resolve user ID, fetching from API if not cached. */
  private async resolveUserId(): Promise<string> {
    if (!this.myId) {
      await this.getMyProfile();
    }
    return this.myId!;
  }
}
