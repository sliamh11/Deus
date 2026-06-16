import path from 'path';

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mockWriteSessionLogAndIndex =
  vi.fn<
    (
      savedPath: string,
      content: string,
      label: string,
      onSettle?: () => void,
    ) => void
  >();
vi.mock('./memory-session-log.js', () => ({
  writeSessionLogAndIndex: mockWriteSessionLogAndIndex,
}));

const mockResolveVaultPath = vi.fn<() => string | null>();
vi.mock('./solutions/index.js', () => ({
  resolveVaultPath: mockResolveVaultPath,
}));

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

const { consolidateWebConversation } = await import('./webui-consolidation.js');

interface Turn {
  role: 'user' | 'assistant' | 'system';
  content: string;
}

/** Build an OpenAI-style request body with the given turns. */
function body(turns: Turn[]): unknown {
  return { messages: turns };
}

/** N user/assistant turn-pairs, first user message customizable for hashing. */
function conversation(
  userCount: number,
  firstUser = 'first question',
): unknown {
  const turns: Turn[] = [];
  for (let i = 0; i < userCount; i++) {
    turns.push({ role: 'user', content: i === 0 ? firstUser : `q${i}` });
    turns.push({ role: 'assistant', content: `a${i}` });
  }
  return body(turns);
}

beforeEach(() => {
  mockWriteSessionLogAndIndex.mockReset();
  mockResolveVaultPath.mockReset();
  mockResolveVaultPath.mockReturnValue('/vault');
  delete process.env.DEUS_WEBUI_CONSOLIDATE;
  delete process.env.DEUS_WEBUI_CONSOLIDATE_MIN_TURNS;
  delete process.env.DEUS_WEBUI_CONSOLIDATE_MIN_CHARS;
});

afterEach(() => {
  // Release any guard a test left held so module-level state doesn't leak.
  const settle = mockWriteSessionLogAndIndex.mock.calls.at(-1)?.[3];
  settle?.();
});

describe('consolidateWebConversation — threshold', () => {
  it('consolidates at the default 3-user-turn boundary (>=)', () => {
    consolidateWebConversation(conversation(3, 'boundary-3-turns'));
    expect(mockWriteSessionLogAndIndex).toHaveBeenCalledTimes(1);
  });

  it('skips a short single-turn conversation (below both thresholds)', () => {
    consolidateWebConversation(body([{ role: 'user', content: 'hi' }]));
    expect(mockWriteSessionLogAndIndex).not.toHaveBeenCalled();
  });

  it('consolidates a 1-turn conversation that exceeds the char threshold', () => {
    const long = 'x'.repeat(250);
    consolidateWebConversation(
      body([
        { role: 'user', content: long },
        { role: 'assistant', content: 'ok' },
      ]),
    );
    expect(mockWriteSessionLogAndIndex).toHaveBeenCalledTimes(1);
  });

  it('honors env-overridden MIN_TURNS', () => {
    process.env.DEUS_WEBUI_CONSOLIDATE_MIN_TURNS = '2';
    consolidateWebConversation(conversation(2, 'two-turn-override'));
    expect(mockWriteSessionLogAndIndex).toHaveBeenCalledTimes(1);
  });
});

describe('consolidateWebConversation — kill switch', () => {
  it('does nothing when DEUS_WEBUI_CONSOLIDATE=0', () => {
    process.env.DEUS_WEBUI_CONSOLIDATE = '0';
    consolidateWebConversation(conversation(5, 'disabled-flag'));
    expect(mockWriteSessionLogAndIndex).not.toHaveBeenCalled();
  });

  it('does nothing when DEUS_WEBUI_CONSOLIDATE=false', () => {
    process.env.DEUS_WEBUI_CONSOLIDATE = 'false';
    consolidateWebConversation(conversation(5, 'disabled-false'));
    expect(mockWriteSessionLogAndIndex).not.toHaveBeenCalled();
  });
});

describe('consolidateWebConversation — vault + content', () => {
  it('skips when no vault is configured', () => {
    mockResolveVaultPath.mockReturnValue(null);
    consolidateWebConversation(conversation(3, 'no-vault'));
    expect(mockWriteSessionLogAndIndex).not.toHaveBeenCalled();
  });

  it('writes a stable per-conversation path keyed by the first user message', () => {
    consolidateWebConversation(conversation(3, 'stable-key-convo'));
    const savedPath = mockWriteSessionLogAndIndex.mock.calls[0][0];
    // Assert on path components, not raw separators — path.join yields '\' on
    // Windows and '/' on POSIX, so a slash-literal regex is not cross-platform.
    const parts = savedPath.split(/[/\\]/);
    expect(path.basename(savedPath)).toMatch(/^webui-[0-9a-f]{16}\.md$/);
    expect(parts).toContain('Session-Logs');
    expect(parts.some((p) => /^\d{4}-\d{2}-\d{2}$/.test(p))).toBe(true);
  });

  it('builds web-session frontmatter with a user/assistant transcript', () => {
    consolidateWebConversation(
      body([
        { role: 'user', content: 'what is X' },
        { role: 'assistant', content: 'X is Y' },
        { role: 'user', content: 'and Z?' },
        { role: 'assistant', content: 'Z too' },
        { role: 'user', content: 'thanks' },
      ]),
    );
    const content = mockWriteSessionLogAndIndex.mock.calls[0][1];
    expect(content).toContain('type: web-session');
    expect(content).toContain('topics: [webui, auto-consolidate]');
    expect(content).toContain('**User**: what is X');
    expect(content).toContain('**Assistant**: X is Y');
    // The spawn label is the 3rd arg, not part of the markdown content.
    expect(mockWriteSessionLogAndIndex.mock.calls[0][2]).toBe(
      'webui-consolidation-index',
    );
  });

  it('emits a safe quoted YAML tldr for adversarial first-message text', () => {
    // Leading spaces + a colon + quotes would corrupt a raw block scalar.
    consolidateWebConversation(
      conversation(3, '  weird: "key" value with: colons'),
    );
    const content = mockWriteSessionLogAndIndex.mock.calls[0][1];
    // tldr is a JSON-stringified (double-quoted, escaped) single-line scalar.
    expect(content).toMatch(/\ntldr: "(?:[^"\\]|\\.)*"\n/);
    expect(content).not.toContain('tldr: |');
  });

  it('excludes system messages from the transcript', () => {
    consolidateWebConversation(
      body([
        { role: 'system', content: 'SECRET SYSTEM PROMPT' },
        { role: 'user', content: 'hello there friend' },
        { role: 'assistant', content: 'hi' },
        { role: 'user', content: 'more' },
        { role: 'user', content: 'and more text to cross threshold' },
      ]),
    );
    const content = mockWriteSessionLogAndIndex.mock.calls[0][1];
    expect(content).not.toContain('SECRET SYSTEM PROMPT');
  });
});

describe('consolidateWebConversation — in-flight idempotency guard', () => {
  it('drops a re-entrant consolidation for the same conversation, then allows it after release', () => {
    const convo = () => conversation(3, 'idempotency-guard-convo');

    // 1st call: writes + holds the guard (mock does NOT invoke onSettle).
    consolidateWebConversation(convo());
    expect(mockWriteSessionLogAndIndex).toHaveBeenCalledTimes(1);

    // 2nd call while in-flight: same first-message hash → no-op.
    consolidateWebConversation(convo());
    expect(mockWriteSessionLogAndIndex).toHaveBeenCalledTimes(1);

    // Release the guard (simulate the indexer spawn settling).
    const onSettle = mockWriteSessionLogAndIndex.mock.calls[0][3];
    expect(onSettle).toBeTypeOf('function');
    onSettle?.();

    // 3rd call after release: allowed again.
    consolidateWebConversation(convo());
    expect(mockWriteSessionLogAndIndex).toHaveBeenCalledTimes(2);
  });
});
