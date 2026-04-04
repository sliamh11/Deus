import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// vi.hoisted runs before vi.mock hoisting — set env before module evaluation
vi.hoisted(() => {
  process.env.TELEGRAM_BOT_TOKEN = 'test-token-123';
});

// Shared state for the mock bot
let catchHandler: Function | null = null;
const mockStart = vi.fn((options: any) => {
  if (options?.onStart) {
    options.onStart({ username: 'test_bot', id: 123 });
  }
  return Promise.resolve();
});
const mockStop = vi.fn();
const mockCommand = vi.fn();
const mockOn = vi.fn();
const mockCatch = vi.fn((handler: Function) => {
  catchHandler = handler;
});

vi.mock('grammy', () => {
  return {
    Bot: class MockBot {
      command = mockCommand;
      on = mockOn;
      catch = mockCatch;
      start = mockStart;
      stop = mockStop;
      api = {
        sendMessage: vi.fn(),
        sendChatAction: vi.fn(),
      };
    },
    Api: class {},
  };
});

vi.mock('pino', () => {
  const mockLogger = {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
    fatal: vi.fn(),
  };
  const pinoFn: any = () => mockLogger;
  pinoFn.destination = () => ({});
  return { default: pinoFn };
});

import { TelegramProvider } from './telegram.js';

describe('TelegramProvider', () => {
  let provider: TelegramProvider;

  beforeEach(() => {
    catchHandler = null;
    // Reset implementations but keep fns alive
    mockStart.mockImplementation((options: any) => {
      if (options?.onStart) {
        options.onStart({ username: 'test_bot', id: 123 });
      }
      return Promise.resolve();
    });
    mockStop.mockReset();
    mockCommand.mockReset();
    mockOn.mockReset();
    mockCatch.mockImplementation((handler: Function) => {
      catchHandler = handler;
    });
    provider = new TelegramProvider();
  });

  describe('error tracking and polling reset', () => {
    beforeEach(() => {
      vi.useFakeTimers();
    });

    afterEach(() => {
      vi.useRealTimers();
    });

    it('tracks consecutive errors via the catch handler', async () => {
      await provider.connect();
      expect(catchHandler).toBeDefined();

      // Simulate errors below threshold
      for (let i = 0; i < 3; i++) {
        catchHandler!({ message: `error ${i}` });
      }

      // Bot should not have been stopped
      expect(mockStop).not.toHaveBeenCalled();
    });

    it('resets polling after MAX_CONSECUTIVE_ERRORS (5) failures', async () => {
      await provider.connect();

      // Reset call counts from connect()
      mockStop.mockClear();
      mockStart.mockClear();

      // Simulate 5 consecutive errors (the threshold)
      for (let i = 0; i < 5; i++) {
        catchHandler!({ message: `error ${i}` });
      }

      // Stop is called immediately in resetPolling
      expect(mockStop).toHaveBeenCalledTimes(1);

      // Advance past the first backoff delay (1s) to trigger start
      await vi.advanceTimersByTimeAsync(1100);

      expect(mockStart).toHaveBeenCalledTimes(1);
    });

    it('does not reset polling twice while already resetting', async () => {
      await provider.connect();

      // Make start not call onStart (simulating slow restart that times out)
      mockStart.mockImplementationOnce(() => Promise.resolve());
      mockStop.mockClear();
      mockStart.mockClear();

      // Trigger 5 errors to start a reset
      for (let i = 0; i < 5; i++) {
        catchHandler!({ message: `error ${i}` });
      }

      // Now send 5 more errors while resetting
      for (let i = 0; i < 5; i++) {
        catchHandler!({ message: `error ${i}` });
      }

      // Should only have triggered one reset (one stop call)
      expect(mockStop).toHaveBeenCalledTimes(1);

      // Drain pending timers so resetPolling's async loop completes cleanly
      // Mock process.exit to prevent test runner from exiting
      const mockExit = vi
        .spyOn(process, 'exit')
        .mockImplementation(() => undefined as never);
      await vi.runAllTimersAsync();
      mockExit.mockRestore();
    });
  });

  describe('disconnect', () => {
    it('cleans up state on disconnect', async () => {
      await provider.connect();
      mockStop.mockClear();

      await provider.disconnect();

      expect(mockStop).toHaveBeenCalledTimes(1);
      expect(provider.isConnected()).toBe(false);
    });
  });

  describe('basic operations', () => {
    it('reports connected after successful connect', async () => {
      await provider.connect();
      expect(provider.isConnected()).toBe(true);
    });

    it('returns correct status', async () => {
      await provider.connect();
      const status = provider.getStatus();
      expect(status.connected).toBe(true);
      expect(status.channel).toBe('telegram');
      expect(status.identity).toBe('test_bot');
    });

    it('has name "telegram"', () => {
      expect(provider.name).toBe('telegram');
    });
  });
});
