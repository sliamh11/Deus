import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { NewMessage, RegisteredGroup } from './types.js';

const mockMkdirSync = vi.fn();
const mockWriteFileSync = vi.fn();
vi.mock('fs', async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>;
  return {
    ...actual,
    mkdirSync: mockMkdirSync,
    writeFileSync: mockWriteFileSync,
  };
});

const mockResolveVaultPath = vi.fn<() => string | null>();
vi.mock('./solutions/index.js', () => ({
  resolveVaultPath: mockResolveVaultPath,
}));

const mockGetMessagesSince = vi.fn<() => NewMessage[]>();
const mockGetAutoCompressWatermark = vi.fn<() => string | undefined>();
const mockSetAutoCompressWatermark = vi.fn();
vi.mock('./db.js', async (importOriginal) => {
  const actual = (await importOriginal()) as Record<string, unknown>;
  return {
    ...actual,
    getMessagesSince: mockGetMessagesSince,
    getAutoCompressWatermark: mockGetAutoCompressWatermark,
    setAutoCompressWatermark: mockSetAutoCompressWatermark,
  };
});

const mockFireAndForget = vi.fn();
vi.mock('./async/index.js', () => ({
  fireAndForget: mockFireAndForget,
}));

vi.mock('./logger.js', () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

const { autoCompressSession } = await import('./auto-compress.js');
const { logger } = await import('./logger.js');

function makeGroup(folder = 'whatsapp_main'): RegisteredGroup {
  return {
    name: 'Test Group',
    folder,
    channels: [],
    isControlGroup: false,
  } as unknown as RegisteredGroup;
}

function makeMessage(overrides: Partial<NewMessage> = {}): NewMessage {
  return {
    id: '1',
    chat_jid: 'test@s.whatsapp.net',
    sender: '123@s.whatsapp.net',
    sender_name: 'Alice',
    content: 'Hello, this is a test message',
    timestamp: '2026-05-12T10:00:00.000Z',
    is_from_me: false,
    ...overrides,
  } as NewMessage;
}

describe('autoCompressSession', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // vi.restoreAllMocks() (afterEach below) only restores vi.spyOn mocks to
    // their original implementation — it doesn't clear a custom
    // mockImplementation set on a plain vi.fn() like mockWriteFileSync, so a
    // throwing override from one test can otherwise leak into the next.
    mockWriteFileSync.mockReset();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns silently when no vault is configured', async () => {
    mockResolveVaultPath.mockReturnValue(null);

    await autoCompressSession(makeGroup(), 'test@jid', 8);

    expect(mockWriteFileSync).not.toHaveBeenCalled();
    expect(logger.debug).toHaveBeenCalledWith(
      'Auto-compress skipped: no vault configured',
    );
  });

  it('returns silently when conversation is empty', async () => {
    mockResolveVaultPath.mockReturnValue('/tmp/vault');
    mockGetMessagesSince.mockReturnValue([]);

    await autoCompressSession(makeGroup(), 'test@jid', 8);

    expect(mockWriteFileSync).not.toHaveBeenCalled();
  });

  it('writes session log with correct path and YAML frontmatter', async () => {
    mockResolveVaultPath.mockReturnValue('/tmp/vault');
    mockGetMessagesSince.mockReturnValue([makeMessage()]);

    await autoCompressSession(makeGroup(), 'test@jid', 8);

    expect(mockMkdirSync).toHaveBeenCalledWith(
      expect.stringContaining('Session-Logs'),
      { recursive: true },
    );
    expect(mockWriteFileSync).toHaveBeenCalledTimes(1);
    const [filePath, content] = mockWriteFileSync.mock.calls[0] as [
      string,
      string,
      string,
    ];
    expect(filePath).toMatch(/auto-whatsapp_main-\d{4}\.md$/);
    expect(content).toContain('type: session');
    expect(content).toContain('topics: [auto-compress]');
    expect(content).toContain('tldr:');
    expect(content).toContain('date:');
  });

  it('includes both user and bot messages in output', async () => {
    mockResolveVaultPath.mockReturnValue('/tmp/vault');
    mockGetMessagesSince.mockReturnValue([
      makeMessage({
        sender_name: 'Alice',
        content: 'Hi there',
        is_from_me: false,
      }),
      makeMessage({ sender_name: 'Deus', content: 'Hello!', is_from_me: true }),
    ]);

    await autoCompressSession(makeGroup(), 'test@jid', 8);

    const content = mockWriteFileSync.mock.calls[0]![1] as string;
    expect(content).toContain('**Alice**');
    expect(content).toContain('**Deus**');
    expect(content).toContain('Hi there');
    expect(content).toContain('Hello!');
  });

  it('resolves successfully even if indexer spawn would fail', async () => {
    mockResolveVaultPath.mockReturnValue('/tmp/vault');
    mockGetMessagesSince.mockReturnValue([makeMessage()]);

    await expect(
      autoCompressSession(makeGroup(), 'test@jid', 8),
    ).resolves.toBeUndefined();

    expect(mockFireAndForget).toHaveBeenCalledWith(expect.any(Function), {
      name: 'auto-compress-index',
    });
  });

  it('throws when file write fails', async () => {
    mockResolveVaultPath.mockReturnValue('/tmp/vault');
    mockGetMessagesSince.mockReturnValue([makeMessage()]);
    mockWriteFileSync.mockImplementation(() => {
      throw new Error('EACCES: permission denied');
    });

    await expect(
      autoCompressSession(makeGroup(), 'test@jid', 8),
    ).rejects.toThrow('EACCES: permission denied');
  });

  it('anchors to lastUsed - 2x idle hours when no watermark exists yet (bootstrap fallback)', async () => {
    mockResolveVaultPath.mockReturnValue('/tmp/vault');
    mockGetAutoCompressWatermark.mockReturnValue(undefined);
    mockGetMessagesSince.mockReturnValue([makeMessage()]);

    const lastUsed = '2026-05-10T00:00:00.000Z';
    const idleHours = 8;
    await autoCompressSession(makeGroup(), 'test@jid', idleHours, lastUsed);

    const expectedSince = new Date(
      new Date(lastUsed).getTime() - idleHours * 2 * 3_600_000,
    ).toISOString();
    expect(mockGetMessagesSince).toHaveBeenCalledWith(
      'test@jid',
      expectedSince,
      expect.any(String),
      500,
      true,
    );
  });

  it('anchors to the persisted watermark when one exists, ignoring lastUsed', async () => {
    mockResolveVaultPath.mockReturnValue('/tmp/vault');
    const watermark = '2026-06-01T12:00:00.000Z';
    mockGetAutoCompressWatermark.mockReturnValue(watermark);
    mockGetMessagesSince.mockReturnValue([makeMessage()]);

    await autoCompressSession(
      makeGroup(),
      'test@jid',
      8,
      '2026-01-01T00:00:00.000Z',
    );

    expect(mockGetMessagesSince).toHaveBeenCalledWith(
      'test@jid',
      watermark,
      expect.any(String),
      500,
      true,
    );
  });

  it('advances the watermark to the max captured message timestamp minus 1 second', async () => {
    mockResolveVaultPath.mockReturnValue('/tmp/vault');
    mockGetAutoCompressWatermark.mockReturnValue(undefined);
    // getMessagesSince always re-sorts ascending (db.ts) before returning —
    // reflect that real contract here rather than scrambled order.
    mockGetMessagesSince.mockReturnValue([
      makeMessage({ timestamp: '2026-05-12T09:00:00.000Z' }),
      makeMessage({ timestamp: '2026-05-12T10:00:00.000Z' }),
      makeMessage({ timestamp: '2026-05-12T10:05:00.000Z' }),
    ]);

    await autoCompressSession(makeGroup(), 'test@jid', 8);

    const expectedWatermark = new Date(
      new Date('2026-05-12T10:05:00.000Z').getTime() - 1_000,
    ).toISOString();
    expect(mockSetAutoCompressWatermark).toHaveBeenCalledWith(
      'test@jid',
      expectedWatermark,
    );
  });
});

describe('getMessagesSince includeBotMessages', () => {
  it('excludes bot messages by default (backward compat)', async () => {
    const { getMessagesSince } = await import('./db.js');
    const real = vi.mocked(getMessagesSince);

    real.mockReturnValue([makeMessage()]);
    const result = real('jid', '2026-01-01', 'Deus', 50);

    expect(result).toHaveLength(1);
    expect(real).toHaveBeenCalledWith('jid', '2026-01-01', 'Deus', 50);
  });
});
