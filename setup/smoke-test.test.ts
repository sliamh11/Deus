import { describe, it, expect, beforeEach, afterEach } from 'vitest';

import Database from 'better-sqlite3';

/**
 * Tests for the smoke-test setup step.
 *
 * Verifies: folder-based channel lookup, DB store/read/cleanup cycle,
 * log parsing for connection indicators.
 */

function createTestDb(): Database.Database {
  const db = new Database(':memory:');
  db.exec(`
    CREATE TABLE IF NOT EXISTS registered_groups (
      jid TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      folder TEXT NOT NULL UNIQUE,
      trigger_pattern TEXT NOT NULL,
      added_at TEXT NOT NULL,
      container_config TEXT,
      requires_trigger INTEGER DEFAULT 1,
      is_main INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS messages (
      id TEXT,
      chat_jid TEXT,
      sender TEXT,
      sender_name TEXT,
      content TEXT,
      timestamp TEXT,
      is_from_me INTEGER,
      is_bot_message INTEGER DEFAULT 0,
      PRIMARY KEY (id, chat_jid)
    );
  `);
  return db;
}

function registerGroup(
  db: Database.Database,
  jid: string,
  name: string,
  folder: string,
): void {
  db.prepare(
    `INSERT INTO registered_groups (jid, name, folder, trigger_pattern, added_at)
     VALUES (?, ?, ?, '@Deus', datetime('now'))`,
  ).run(jid, name, folder);
}

describe('folder-based channel lookup', () => {
  it('finds whatsapp group by folder prefix', () => {
    const db = createTestDb();
    registerGroup(db, '123@s.whatsapp.net', 'Self', 'whatsapp_main');
    registerGroup(db, 'tg:456', 'Telegram', 'telegram_main');

    const row = db
      .prepare('SELECT jid, folder FROM registered_groups WHERE folder LIKE ?')
      .get('whatsapp_%') as { jid: string; folder: string } | undefined;

    expect(row).toBeDefined();
    expect(row!.jid).toBe('123@s.whatsapp.net');
    expect(row!.folder).toBe('whatsapp_main');
    db.close();
  });

  it('finds telegram group by folder prefix', () => {
    const db = createTestDb();
    registerGroup(db, '123@s.whatsapp.net', 'Self', 'whatsapp_main');
    registerGroup(db, 'tg:456', 'Telegram', 'telegram_main');

    const row = db
      .prepare('SELECT jid, folder FROM registered_groups WHERE folder LIKE ?')
      .get('telegram_%') as { jid: string; folder: string } | undefined;

    expect(row).toBeDefined();
    expect(row!.jid).toBe('tg:456');
    db.close();
  });

  it('finds slack group by folder prefix', () => {
    const db = createTestDb();
    registerGroup(db, 'slack:C12345', 'General', 'slack_general');

    const row = db
      .prepare('SELECT jid, folder FROM registered_groups WHERE folder LIKE ?')
      .get('slack_%') as { jid: string; folder: string } | undefined;

    expect(row).toBeDefined();
    expect(row!.jid).toBe('slack:C12345');
    db.close();
  });

  it('finds discord group by folder prefix', () => {
    const db = createTestDb();
    registerGroup(db, 'dc:789', 'Server', 'discord_main');

    const row = db
      .prepare('SELECT jid, folder FROM registered_groups WHERE folder LIKE ?')
      .get('discord_%') as { jid: string; folder: string } | undefined;

    expect(row).toBeDefined();
    expect(row!.jid).toBe('dc:789');
    db.close();
  });

  it('finds gmail group by folder prefix', () => {
    const db = createTestDb();
    registerGroup(db, 'gmail:inbox', 'Inbox', 'gmail_inbox');

    const row = db
      .prepare('SELECT jid, folder FROM registered_groups WHERE folder LIKE ?')
      .get('gmail_%') as { jid: string; folder: string } | undefined;

    expect(row).toBeDefined();
    expect(row!.jid).toBe('gmail:inbox');
    db.close();
  });

  it('works for any future channel without code changes', () => {
    const db = createTestDb();
    registerGroup(db, 'matrix:room123', 'Matrix Room', 'matrix_general');

    const row = db
      .prepare('SELECT jid, folder FROM registered_groups WHERE folder LIKE ?')
      .get('matrix_%') as { jid: string; folder: string } | undefined;

    expect(row).toBeDefined();
    expect(row!.jid).toBe('matrix:room123');
    db.close();
  });

  it('returns any group when no channel specified', () => {
    const db = createTestDb();
    registerGroup(db, 'tg:456', 'Telegram', 'telegram_main');

    const row = db
      .prepare('SELECT jid FROM registered_groups LIMIT 1')
      .get() as { jid: string } | undefined;

    expect(row).toBeDefined();
    expect(row!.jid).toBe('tg:456');
    db.close();
  });

  it('returns undefined when no matching channel group exists', () => {
    const db = createTestDb();
    registerGroup(db, '123@s.whatsapp.net', 'Self', 'whatsapp_main');

    const row = db
      .prepare('SELECT jid FROM registered_groups WHERE folder LIKE ?')
      .get('telegram_%') as { jid: string } | undefined;

    expect(row).toBeUndefined();
    db.close();
  });
});

describe('smoke test DB write/read/cleanup cycle', () => {
  let db: Database.Database;

  beforeEach(() => {
    db = createTestDb();
    registerGroup(db, '123@s.whatsapp.net', 'Self', 'whatsapp_main');
  });

  afterEach(() => {
    db.close();
  });

  it('stores a test message and retrieves it', () => {
    const testId = `smoke-test-${Date.now()}`;
    const timestamp = new Date().toISOString();

    db.prepare(
      `INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      testId,
      '123@s.whatsapp.net',
      'smoke-test@internal',
      'SmokeTest',
      '[Deus smoke test — ignore this message]',
      timestamp,
      1,
      1,
    );

    const stored = db
      .prepare('SELECT id, is_bot_message FROM messages WHERE id = ?')
      .get(testId) as { id: string; is_bot_message: number } | undefined;

    expect(stored).toBeDefined();
    expect(stored!.id).toBe(testId);
    expect(stored!.is_bot_message).toBe(1);
  });

  it('cleans up the test message after verification', () => {
    const testId = `smoke-test-${Date.now()}`;
    const timestamp = new Date().toISOString();

    db.prepare(
      `INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      testId,
      '123@s.whatsapp.net',
      'smoke-test@internal',
      'SmokeTest',
      'test',
      timestamp,
      1,
      1,
    );

    expect(
      db.prepare('SELECT id FROM messages WHERE id = ?').get(testId),
    ).toBeDefined();
    db.prepare('DELETE FROM messages WHERE id = ?').run(testId);
    expect(
      db.prepare('SELECT id FROM messages WHERE id = ?').get(testId),
    ).toBeUndefined();
  });

  it('does not affect other messages during cleanup', () => {
    const testId = `smoke-test-${Date.now()}`;
    const realId = 'real-message-123';
    const timestamp = new Date().toISOString();

    db.prepare(
      `INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      realId,
      '123@s.whatsapp.net',
      'user@wa',
      'User',
      'Hello!',
      timestamp,
      0,
      0,
    );

    db.prepare(
      `INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      testId,
      '123@s.whatsapp.net',
      'smoke-test@internal',
      'SmokeTest',
      'test',
      timestamp,
      1,
      1,
    );

    db.prepare('DELETE FROM messages WHERE id = ?').run(testId);

    const real = db
      .prepare('SELECT id FROM messages WHERE id = ?')
      .get(realId) as { id: string } | undefined;
    expect(real).toBeDefined();
    expect(real!.id).toBe(realId);
  });
});

describe('smoke test argument parsing', () => {
  it('parses --channel flag', () => {
    const args = ['--channel', 'whatsapp'];
    let channel: string | undefined;
    for (let i = 0; i < args.length; i++) {
      if (args[i] === '--channel') channel = (args[++i] || '').toLowerCase();
    }
    expect(channel).toBe('whatsapp');
  });

  it('parses --timeout flag', () => {
    const args = ['--timeout', '5000'];
    let timeout = 10_000;
    for (let i = 0; i < args.length; i++) {
      if (args[i] === '--timeout') timeout = Number(args[++i]) || 10_000;
    }
    expect(timeout).toBe(5000);
  });

  it('defaults timeout to 10000 when not specified', () => {
    const args = ['--channel', 'telegram'];
    let timeout = 10_000;
    for (let i = 0; i < args.length; i++) {
      if (args[i] === '--timeout') timeout = Number(args[++i]) || 10_000;
    }
    expect(timeout).toBe(10_000);
  });

  it('normalizes channel name to lowercase', () => {
    const args = ['--channel', 'WhatsApp'];
    let channel: string | undefined;
    for (let i = 0; i < args.length; i++) {
      if (args[i] === '--channel') channel = (args[++i] || '').toLowerCase();
    }
    expect(channel).toBe('whatsapp');
  });
});
