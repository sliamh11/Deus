/**
 * Regression tests for #1163 — the first message in a newly registered chat was
 * silently dropped.
 *
 * Two levels, because either alone is insufficient:
 *
 *  1. ORDERING at the adapter's surface. `messages.chat_jid` has a foreign key
 *     onto `chats(jid)`, so the chat row must exist before the message insert.
 *     The adapter emitted the message first, so the very first message in a
 *     chat violated the constraint and was lost.
 *
 *  2. CONSEQUENCE against a real SQLite database. The ordering assertion alone
 *     would keep passing if someone dropped the foreign key or disabled the
 *     pragma; this proves the constraint is genuinely enforced and that the
 *     wrong order really does lose the row.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import Database from 'better-sqlite3';

const { capturedHandlers } = vi.hoisted(() => ({
  capturedHandlers: [] as Array<(n: unknown) => void>,
}));

vi.mock('../logger.js', () => ({
  logger: { info: vi.fn(), error: vi.fn(), warn: vi.fn() },
}));

vi.mock('@modelcontextprotocol/sdk/client/stdio.js', () => ({
  StdioClientTransport: vi.fn().mockImplementation(function () {}),
}));

vi.mock('@modelcontextprotocol/sdk/client/index.js', () => ({
  Client: vi.fn().mockImplementation(function () {
    return {
      callTool: vi.fn().mockResolvedValue({}),
      connect: vi.fn().mockResolvedValue(undefined),
      close: vi.fn().mockResolvedValue(undefined),
      setNotificationHandler: function (
        _schema: unknown,
        handler: (n: unknown) => void,
      ) {
        capturedHandlers.push(handler);
      },
    };
  }),
}));

const { McpChannelAdapter } = await import('./mcp-adapter.js');

function makeOpts() {
  return {
    name: 'test-channel',
    command: 'node',
    args: ['server.js'],
    onMessage: vi.fn(),
    onReaction: vi.fn(),
    onChatMetadata: vi.fn(),
    ownsJid: vi.fn().mockReturnValue(false),
  };
}

function incomingMessage(chatId: string) {
  return {
    params: {
      logger: 'incoming_message',
      data: {
        id: 'MSG1',
        chat_id: chatId,
        sender: 'someone@c.us',
        sender_name: 'Someone',
        content: 'first message in a brand new chat',
        timestamp: '2026-08-20T00:00:00Z',
        chat_name: 'New Chat',
        is_group: false,
      },
    },
  };
}

beforeEach(() => {
  capturedHandlers.length = 0;
});

describe('mcp-adapter chat-before-message ordering (#1163)', () => {
  it('emits chat metadata BEFORE the message', () => {
    const opts = makeOpts();
    new McpChannelAdapter(opts);
    const handler = capturedHandlers[capturedHandlers.length - 1];

    handler(incomingMessage('brand-new@c.us'));

    expect(opts.onChatMetadata).toHaveBeenCalledTimes(1);
    expect(opts.onMessage).toHaveBeenCalledTimes(1);

    // The defect: on the unfixed adapter onMessage fired first, so the parent
    // chats row did not exist yet and the FK insert failed.
    const metadataOrder = opts.onChatMetadata.mock.invocationCallOrder[0];
    const messageOrder = opts.onMessage.mock.invocationCallOrder[0];
    expect(metadataOrder).toBeLessThan(messageOrder);
  });

  it('still passes chat metadata through unchanged', () => {
    const opts = makeOpts();
    new McpChannelAdapter(opts);
    const handler = capturedHandlers[capturedHandlers.length - 1];

    handler(incomingMessage('brand-new@c.us'));

    // Reordering must change WHEN metadata is recorded, not WHAT.
    expect(opts.onChatMetadata).toHaveBeenCalledWith(
      'brand-new@c.us',
      '2026-08-20T00:00:00Z',
      'New Chat',
      'test-channel',
      false,
    );
  });

  it('still delivers the message itself', () => {
    const opts = makeOpts();
    new McpChannelAdapter(opts);
    const handler = capturedHandlers[capturedHandlers.length - 1];

    handler(incomingMessage('existing@c.us'));

    expect(opts.onMessage).toHaveBeenCalledTimes(1);
    const [jid, msg] = opts.onMessage.mock.calls[0];
    expect(jid).toBe('existing@c.us');
    expect(msg.content).toBe('first message in a brand new chat');
  });
});

describe('the constraint this ordering exists to satisfy (#1163)', () => {
  /** Minimal reproduction of the real schema's parent/child relationship. */
  function freshDb() {
    const db = new Database(':memory:');
    db.exec(`
      CREATE TABLE chats (jid TEXT PRIMARY KEY, name TEXT, last_message_time TEXT);
      CREATE TABLE messages (
        id TEXT, chat_jid TEXT, content TEXT,
        PRIMARY KEY (id, chat_jid),
        FOREIGN KEY (chat_jid) REFERENCES chats(jid)
      );
    `);
    return db;
  }

  it('enforces foreign keys by default', () => {
    const db = freshDb();
    // SQLite's own default is OFF; better-sqlite3 turns it on. If this ever
    // changes, the ordering above stops being load-bearing and this test says so.
    expect(db.pragma('foreign_keys', { simple: true })).toBe(1);
  });

  it('LOSES a message inserted before its chat row exists', () => {
    const db = freshDb();

    expect(() =>
      db
        .prepare(
          'INSERT INTO messages (id, chat_jid, content) VALUES (?, ?, ?)',
        )
        .run('MSG1', 'brand-new@c.us', 'hello'),
    ).toThrow(/FOREIGN KEY constraint failed/);

    expect(db.prepare('SELECT COUNT(*) AS n FROM messages').get()).toEqual({
      n: 0,
    });
  });

  it('stores the message once the chat row exists first', () => {
    const db = freshDb();

    db.prepare('INSERT INTO chats (jid, name) VALUES (?, ?)').run(
      'brand-new@c.us',
      'New Chat',
    );
    db.prepare(
      'INSERT INTO messages (id, chat_jid, content) VALUES (?, ?, ?)',
    ).run('MSG1', 'brand-new@c.us', 'hello');

    expect(db.prepare('SELECT COUNT(*) AS n FROM messages').get()).toEqual({
      n: 1,
    });
  });
});
