import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.hoisted(() => {
  process.env.X_API_KEY = 'test-key';
  process.env.X_API_SECRET = 'test-secret';
  process.env.X_ACCESS_TOKEN = 'test-token';
  process.env.X_ACCESS_TOKEN_SECRET = 'test-token-secret';
});

// Shared mock state
const mockTweet = vi.fn();
const mockMe = vi.fn();
const mockLike = vi.fn();
const mockUnlike = vi.fn();
const mockRetweet = vi.fn();
const mockUnretweet = vi.fn();
const mockHomeTimeline = vi.fn();
const mockSearch = vi.fn();
const mockSingleTweet = vi.fn();

vi.mock('twitter-api-v2', () => {
  return {
    TwitterApi: class MockTwitterApi {
      v2 = {
        tweet: mockTweet,
        me: mockMe,
        like: mockLike,
        unlike: mockUnlike,
        retweet: mockRetweet,
        unretweet: mockUnretweet,
        homeTimeline: mockHomeTimeline,
        search: mockSearch,
        singleTweet: mockSingleTweet,
      };
    },
  };
});

vi.mock('pino', () => {
  const mockLogger = {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  };
  const pinoFn: any = () => mockLogger;
  pinoFn.destination = () => ({});
  return { default: pinoFn };
});

import { XClient } from './x.js';

describe('XClient', () => {
  let client: XClient;

  beforeEach(() => {
    vi.clearAllMocks();
    client = new XClient();

    // Default: me() returns a stable profile
    mockMe.mockResolvedValue({
      data: {
        id: 'user-123',
        username: 'testuser',
        name: 'Test User',
        description: 'Test bio',
        public_metrics: {
          followers_count: 100,
          following_count: 50,
          tweet_count: 200,
        },
      },
    });
  });

  describe('isConfigured', () => {
    it('returns true when all credentials are set', () => {
      expect(client.isConfigured()).toBe(true);
    });

    it('returns false when credentials are missing', () => {
      const saved = process.env.X_API_KEY;
      delete process.env.X_API_KEY;
      const unconfigured = new XClient();
      expect(unconfigured.isConfigured()).toBe(false);
      process.env.X_API_KEY = saved;
    });
  });

  describe('getMyProfile', () => {
    it('returns profile data', async () => {
      const profile = await client.getMyProfile();
      expect(profile.id).toBe('user-123');
      expect(profile.username).toBe('testuser');
      expect(profile.public_metrics?.followers_count).toBe(100);
    });
  });

  describe('postTweet', () => {
    it('posts a tweet and returns result with url', async () => {
      mockTweet.mockResolvedValue({
        data: { id: 'tweet-1', text: 'Hello world' },
      });

      const result = await client.postTweet('Hello world');

      expect(mockTweet).toHaveBeenCalledWith('Hello world');
      expect(result.id).toBe('tweet-1');
      expect(result.text).toBe('Hello world');
      expect(result.url).toBe('https://x.com/testuser/status/tweet-1');
    });
  });

  describe('replyToTweet', () => {
    it('posts a reply with in_reply_to set', async () => {
      mockTweet.mockResolvedValue({
        data: { id: 'reply-1', text: 'Nice tweet!' },
      });

      const result = await client.replyToTweet('original-123', 'Nice tweet!');

      expect(mockTweet).toHaveBeenCalledWith('Nice tweet!', {
        reply: { in_reply_to_tweet_id: 'original-123' },
      });
      expect(result.url).toBe('https://x.com/testuser/status/reply-1');
    });
  });

  describe('quoteTweet', () => {
    it('posts a quote tweet', async () => {
      mockTweet.mockResolvedValue({
        data: { id: 'quote-1', text: 'Quoting this' },
      });

      const result = await client.quoteTweet('original-456', 'Quoting this');

      expect(mockTweet).toHaveBeenCalledWith('Quoting this', {
        quote_tweet_id: 'original-456',
      });
      expect(result.id).toBe('quote-1');
    });
  });

  describe('likeTweet', () => {
    it('calls like with user id and tweet id', async () => {
      mockLike.mockResolvedValue({});

      await client.likeTweet('tweet-789');

      expect(mockLike).toHaveBeenCalledWith('user-123', 'tweet-789');
    });
  });

  describe('unlikeTweet', () => {
    it('calls unlike with user id and tweet id', async () => {
      mockUnlike.mockResolvedValue({});

      await client.unlikeTweet('tweet-789');

      expect(mockUnlike).toHaveBeenCalledWith('user-123', 'tweet-789');
    });
  });

  describe('retweet', () => {
    it('calls retweet with user id and tweet id', async () => {
      mockRetweet.mockResolvedValue({});

      await client.retweet('tweet-101');

      expect(mockRetweet).toHaveBeenCalledWith('user-123', 'tweet-101');
    });
  });

  describe('undoRetweet', () => {
    it('calls unretweet with user id and tweet id', async () => {
      mockUnretweet.mockResolvedValue({});

      await client.undoRetweet('tweet-101');

      expect(mockUnretweet).toHaveBeenCalledWith('user-123', 'tweet-101');
    });
  });

  describe('getTimeline', () => {
    it('returns tweets from home timeline', async () => {
      mockHomeTimeline.mockResolvedValue({
        data: {
          data: [
            { id: 't1', text: 'Tweet one' },
            { id: 't2', text: 'Tweet two' },
          ],
        },
      });

      const tweets = await client.getTimeline(2);

      expect(mockHomeTimeline).toHaveBeenCalledWith(
        expect.objectContaining({ max_results: 2 }),
      );
      expect(tweets).toHaveLength(2);
      expect(tweets[0].url).toBe('https://x.com/testuser/status/t1');
    });

    it('returns empty array when timeline has no data', async () => {
      mockHomeTimeline.mockResolvedValue({ data: {} });
      const tweets = await client.getTimeline();
      expect(tweets).toEqual([]);
    });
  });

  describe('searchTweets', () => {
    it('returns matching tweets with author usernames in urls', async () => {
      mockSearch.mockResolvedValue({
        data: {
          data: [{ id: 's1', text: 'Search result', author_id: 'author-1' }],
          includes: { users: [{ id: 'author-1', username: 'authoruser' }] },
        },
      });

      const tweets = await client.searchTweets('vitest', 10);

      expect(tweets[0].url).toBe('https://x.com/authoruser/status/s1');
    });

    it('returns empty array when no results', async () => {
      mockSearch.mockResolvedValue({ data: {} });
      const tweets = await client.searchTweets('noresults');
      expect(tweets).toEqual([]);
    });
  });

  describe('getTweet', () => {
    it('returns a single tweet with author url', async () => {
      mockSingleTweet.mockResolvedValue({
        data: { id: 'single-1', text: 'Single tweet' },
        includes: { users: [{ id: 'author-1', username: 'authoruser' }] },
      });

      const tweet = await client.getTweet('single-1');

      expect(tweet.id).toBe('single-1');
      expect(tweet.url).toBe('https://x.com/authoruser/status/single-1');
    });
  });

  describe('profile caching', () => {
    it('fetches profile only once across multiple operations', async () => {
      mockTweet.mockResolvedValue({ data: { id: 'a', text: 'a' } });
      mockLike.mockResolvedValue({});

      await client.postTweet('a');
      await client.likeTweet('tweet-x');

      // me() should only be called once (cached after first call)
      expect(mockMe).toHaveBeenCalledTimes(1);
    });
  });
});
