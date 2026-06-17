/**
 * LIA-302 Phase 0 — @oracle golden capture (regression net for the unification).
 *
 * These snapshots are CAPTURED FROM THE LIVE PRE-REFACTOR code by running the two
 * consolidation surfaces against fixed fixtures, NOT hand-composed. They pin the
 * EXACT bytes each surface hands to writeSessionLogAndIndex (the stable seam below
 * both the current and the future unified core), plus the per-surface throw/await
 * contracts. Phase 1 (extracting consolidateSessionLog) MUST keep every snapshot
 * byte-identical — that is the proof the envelope refactor changed nothing.
 *
 * Determinism: system time is frozen; the auto-compress body time token `(HH:MM)`
 * is the only runtime-TZ-variant fragment, so it is normalized to `(HH:MM)` — the
 * envelope refactor cannot affect it (body rendering stays in each surface), and
 * the surrounding `**sender** (HH:MM): text` structure is still pinned.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { NewMessage, RegisteredGroup } from './types.js';

const mockWrite =
  vi.fn<
    (p: string, content: string, label: string, onSettle?: () => void) => void
  >();
vi.mock('./memory-session-log.js', () => ({
  writeSessionLogAndIndex: mockWrite,
}));

const mockResolveVault = vi.fn<() => string | null>();
vi.mock('./solutions/index.js', () => ({
  resolveVaultPath: mockResolveVault,
}));

const mockGetMessagesSince = vi.fn<() => NewMessage[]>();
vi.mock('./db.js', async (orig) => ({
  ...((await orig()) as Record<string, unknown>),
  getMessagesSince: mockGetMessagesSince,
}));

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

const { autoCompressSession } = await import('./auto-compress.js');
const { consolidateWebConversation } = await import('./webui-consolidation.js');

/** The exact (path, content, label) handed to writeSessionLogAndIndex, normalized. */
function captured(): { savedPath: string; content: string; label: string } {
  const [savedPath, content, label] = mockWrite.mock.calls[0];
  return {
    savedPath: savedPath.replace(/\\/g, '/'), // cross-platform separator
    // Normalize the only runtime-TZ-variant token. It fires solely on
    // auto-compress output; webui bodies carry no `(HH:MM)`, so this is a no-op
    // there. The surrounding `**sender** (HH:MM): text` structure stays pinned.
    content: content.replace(/\((\d{2}):(\d{2})\)/g, '(HH:MM)'),
    label,
  };
}

function msg(o: Partial<NewMessage>): NewMessage {
  return {
    id: '1',
    chat_jid: 'c@jid',
    sender: 'sender',
    sender_name: 'Alice',
    content: 'hi',
    timestamp: '2026-05-12T10:00:00.000Z',
    is_from_me: false,
    ...o,
  } as NewMessage;
}

function makeGroup(): RegisteredGroup {
  return {
    name: 'Test Group',
    folder: 'whatsapp_main',
    channels: [],
    isControlGroup: false,
  } as unknown as RegisteredGroup;
}

const WEBUI_FIXTURE = {
  messages: [
    { role: 'user', content: 'How do I configure X?' },
    { role: 'assistant', content: 'Set X in config.' },
    { role: 'user', content: 'And Y?' },
    { role: 'assistant', content: 'Set Y too.' },
    { role: 'user', content: 'Thanks.' },
  ],
};

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-05-12T10:00:00.000Z'));
  mockWrite.mockReset();
  mockResolveVault.mockReset().mockReturnValue('/vault');
  mockGetMessagesSince.mockReset();
  delete process.env.DEUS_WEBUI_CONSOLIDATE;
  delete process.env.DEUS_WEBUI_CONSOLIDATE_MIN_TURNS;
  delete process.env.DEUS_WEBUI_CONSOLIDATE_MIN_CHARS;
});

afterEach(() => {
  // Release EVERY webui in-flight guard a test left held (not just the last),
  // then restore the clock. Belt-and-suspenders: each webui test also uses a
  // unique first-user message, so keys never collide across tests regardless.
  for (const call of mockWrite.mock.calls) call[3]?.();
  vi.useRealTimers();
});

// ── Byte goldens (the envelope + frontmatter + body each surface emits) ───────

describe('@oracle auto-compress output', () => {
  it('byte-stable session-log envelope handed to writeSessionLogAndIndex', async () => {
    mockGetMessagesSince.mockReturnValue([
      msg({
        sender_name: 'Alice',
        content: 'How do I configure X?',
        is_from_me: false,
        timestamp: '2026-05-12T10:00:00.000Z',
      }),
      msg({
        sender_name: 'Deus',
        content: 'Set X in config.',
        is_from_me: true,
        timestamp: '2026-05-12T10:01:00.000Z',
      }),
    ]);

    await autoCompressSession(makeGroup(), 'c@jid', 8);

    expect(mockWrite).toHaveBeenCalledTimes(1);
    expect(captured()).toMatchInlineSnapshot(`
      {
        "content": "---
      type: session
      date: 2026-05-12
      topics: [auto-compress]
      tldr: |
        How do I configure X?
      ---

      **Alice** (HH:MM): How do I configure X?

      **Deus** (HH:MM): Set X in config.
      ",
        "label": "auto-compress-index",
        "savedPath": "/vault/Session-Logs/2026-05-12/auto-whatsapp_main-1000.md",
      }
    `);
  });
});

describe('@oracle webui-consolidation output', () => {
  it('byte-stable web-session envelope handed to writeSessionLogAndIndex', () => {
    consolidateWebConversation(WEBUI_FIXTURE);

    expect(mockWrite).toHaveBeenCalledTimes(1);
    expect(captured()).toMatchInlineSnapshot(`
      {
        "content": "---
      type: web-session
      date: 2026-05-12
      topics: [webui, auto-consolidate]
      tldr: "How do I configure X?"
      ---

      **User**: How do I configure X?

      **Assistant**: Set X in config.

      **User**: And Y?

      **Assistant**: Set Y too.

      **User**: Thanks.
      ",
        "label": "webui-consolidation-index",
        "savedPath": "/vault/Session-Logs/2026-05-12/webui-db1753a7f3b3f129.md",
      }
    `);
  });
});

// ── Skip contracts (the "must NOT write" guards the core will OWN) ─────────────
// As load-bearing as the byte content: Phase 1 moves the vault-gate + empty-skip
// into the shared core, so the oracle must pin that each still short-circuits.

describe('@oracle skip contracts', () => {
  it('auto-compress does NOT write when no vault is configured', async () => {
    mockResolveVault.mockReturnValue(null);
    mockGetMessagesSince.mockReturnValue([msg({})]);
    await autoCompressSession(makeGroup(), 'c@jid', 8);
    expect(mockWrite).not.toHaveBeenCalled();
  });

  it('auto-compress does NOT write when there are no messages', async () => {
    mockGetMessagesSince.mockReturnValue([]);
    await autoCompressSession(makeGroup(), 'c@jid', 8);
    expect(mockWrite).not.toHaveBeenCalled();
  });

  it('webui does NOT write below the consolidation threshold', () => {
    // Pin thresholds so the skip branch is exercised regardless of default drift.
    process.env.DEUS_WEBUI_CONSOLIDATE_MIN_TURNS = '3';
    process.env.DEUS_WEBUI_CONSOLIDATE_MIN_CHARS = '200';
    consolidateWebConversation({
      messages: [{ role: 'user', content: 'one short turn, skip-threshold' }],
    });
    expect(mockWrite).not.toHaveBeenCalled();
  });

  it('webui does NOT write when no vault is configured AND releases the in-flight key', () => {
    // Phase 1 moves vault resolution into the shared core, so webui now adds its
    // in-flight key BEFORE the core call. The core MUST release that key on the
    // no-vault skip (via onSettle), or this conversation could never consolidate
    // again — a silent, permanent suppression. This pins that contract.
    const fixture = {
      messages: [
        { role: 'user', content: 'no-vault skip-path first message' },
        { role: 'assistant', content: 'a1' },
        { role: 'user', content: 'q2' },
        { role: 'assistant', content: 'a2' },
        { role: 'user', content: 'q3' },
      ],
    };

    mockResolveVault.mockReturnValue(null);
    consolidateWebConversation(fixture);
    expect(mockWrite).not.toHaveBeenCalled();

    // Key released → once a vault is configured, an identical retry writes.
    mockResolveVault.mockReturnValue('/vault');
    consolidateWebConversation(fixture);
    expect(mockWrite).toHaveBeenCalledTimes(1);
  });
});

// ── Behavior goldens (per-surface throw/await contracts the core must preserve) ─

describe('@oracle throw/await contracts', () => {
  it('auto-compress PROPAGATES a synchronous write failure (orchestrator catches it)', async () => {
    mockGetMessagesSince.mockReturnValue([msg({})]);
    mockWrite.mockImplementation(() => {
      throw new Error('EACCES: permission denied');
    });

    await expect(autoCompressSession(makeGroup(), 'c@jid', 8)).rejects.toThrow(
      'EACCES: permission denied',
    );
  });

  it('webui SWALLOWS a synchronous write failure and releases the in-flight key (void contract)', () => {
    // Distinct first-user message → a unique conversation key (no collision with
    // the byte-golden webui test, which holds its own key until afterEach).
    const throwFixture = {
      messages: [
        { role: 'user', content: 'throw-path conversation first message' },
        { role: 'assistant', content: 'a1' },
        { role: 'user', content: 'q2' },
        { role: 'assistant', content: 'a2' },
        { role: 'user', content: 'q3' },
      ],
    };
    mockWrite.mockImplementation(() => {
      throw new Error('EACCES: permission denied');
    });

    // Fire-and-forget contract: must NOT throw out to the sink.
    expect(() => consolidateWebConversation(throwFixture)).not.toThrow();

    // The catch released the conversation key → an identical retry writes again.
    mockWrite.mockReset();
    consolidateWebConversation(throwFixture);
    expect(mockWrite).toHaveBeenCalledTimes(1);
  });
});
