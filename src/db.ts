import Database from 'better-sqlite3';
import { createHash } from 'node:crypto';
import fs from 'fs';
import path from 'path';

import { ASSISTANT_NAME, DATA_DIR, STORE_DIR } from './config.js';
import { isValidGroupFolder } from './group-folder.js';
import { logger } from './logger.js';
import { getBus } from './events/bus.js';
import {
  AgentRuntimeId,
  defaultSession,
  parseAgentBackend,
} from './agent-runtimes/types.js';
import {
  RuntimeSession,
  NewMessage,
  ProjectConfig,
  RegisteredGroup,
  ScheduledTask,
  TaskRunLog,
} from './types.js';

let db: Database.Database;

function createSchema(database: Database.Database): void {
  database.exec(`
    CREATE TABLE IF NOT EXISTS chats (
      jid TEXT PRIMARY KEY,
      name TEXT,
      last_message_time TEXT,
      channel TEXT,
      is_group INTEGER DEFAULT 0
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
      PRIMARY KEY (id, chat_jid),
      FOREIGN KEY (chat_jid) REFERENCES chats(jid)
    );
    CREATE INDEX IF NOT EXISTS idx_timestamp ON messages(timestamp);

    CREATE TABLE IF NOT EXISTS scheduled_tasks (
      id TEXT PRIMARY KEY,
      group_folder TEXT NOT NULL,
      chat_jid TEXT NOT NULL,
      prompt TEXT NOT NULL,
      schedule_type TEXT NOT NULL,
      schedule_value TEXT NOT NULL,
      next_run TEXT,
      last_run TEXT,
      last_result TEXT,
      status TEXT DEFAULT 'active',
      created_at TEXT NOT NULL,
      agent_backend TEXT,
      context_mode TEXT DEFAULT 'isolated'
    );
    CREATE INDEX IF NOT EXISTS idx_next_run ON scheduled_tasks(next_run);
    CREATE INDEX IF NOT EXISTS idx_status ON scheduled_tasks(status);

    CREATE TABLE IF NOT EXISTS task_run_logs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      task_id TEXT NOT NULL,
      run_at TEXT NOT NULL,
      duration_ms INTEGER NOT NULL,
      status TEXT NOT NULL,
      result TEXT,
      error TEXT,
      FOREIGN KEY (task_id) REFERENCES scheduled_tasks(id)
    );
    CREATE INDEX IF NOT EXISTS idx_task_run_logs ON task_run_logs(task_id, run_at);

    CREATE TABLE IF NOT EXISTS router_state (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      group_folder TEXT NOT NULL,
      session_id TEXT NOT NULL,
      backend TEXT DEFAULT 'claude',
      resume_cursor TEXT,
      metadata_json TEXT,
      last_used_at TEXT,
      orphaned_at TEXT,
      orphan_reason TEXT,
      last_compacted_at TEXT
    );
    CREATE TABLE IF NOT EXISTS registered_groups (
      jid TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      folder TEXT NOT NULL UNIQUE,
      trigger_pattern TEXT NOT NULL,
      added_at TEXT NOT NULL,
      container_config TEXT,
      requires_trigger INTEGER DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS projects (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      path TEXT NOT NULL UNIQUE,
      type TEXT,
      readonly INTEGER DEFAULT 0,
      allow_external_push INTEGER DEFAULT 0,
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS linear_webhook_events (
      event_key TEXT PRIMARY KEY,
      issue_id TEXT NOT NULL,
      gate_to TEXT NOT NULL,
      from_state_id TEXT NOT NULL,
      to_state_id TEXT NOT NULL,
      webhook_ts TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      verdict TEXT,
      started_at TEXT,
      finished_at TEXT,
      error TEXT
    );

    CREATE TABLE IF NOT EXISTS linear_gate_comments (
      issue_id TEXT NOT NULL,
      gate_to TEXT NOT NULL,
      comment_id TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      PRIMARY KEY (issue_id, gate_to)
    );

    CREATE TABLE IF NOT EXISTS linear_issue_prs (
      issue_id TEXT PRIMARY KEY,
      pr_url TEXT NOT NULL,
      branch TEXT,
      identifier TEXT,
      auto_merge_state TEXT DEFAULT 'none',
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS linear_pipeline_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      issue_id TEXT NOT NULL,
      identifier TEXT NOT NULL,
      event_type TEXT NOT NULL,
      detail TEXT,
      created_at TEXT NOT NULL,
      status_summary TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_pipeline_events_issue
      ON linear_pipeline_events(issue_id);
    CREATE INDEX IF NOT EXISTS idx_pipeline_events_type
      ON linear_pipeline_events(event_type);
    CREATE INDEX IF NOT EXISTS idx_pipeline_events_created
      ON linear_pipeline_events(created_at);

    CREATE TABLE IF NOT EXISTS linear_pipeline_comments (
      issue_id TEXT PRIMARY KEY,
      comment_id TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS linear_gate_meta (
      issue_id TEXT NOT NULL,
      gate_name TEXT NOT NULL,
      enrichment_hash TEXT,
      enrichment_snapshot TEXT,
      attempt_count INTEGER DEFAULT 0,
      revise_history TEXT,
      shipped_at TEXT,
      PRIMARY KEY (issue_id, gate_name)
    );

    CREATE TABLE IF NOT EXISTS linear_issue_cache (
      issue_id    TEXT PRIMARY KEY,
      identifier  TEXT NOT NULL,
      title       TEXT NOT NULL,
      state_name  TEXT NOT NULL,
      team_id     TEXT NOT NULL,
      priority    INTEGER NOT NULL DEFAULT 0,
      created_at  TEXT NOT NULL,
      updated_at  TEXT NOT NULL,
      cached_at   TEXT NOT NULL,
      deleted_at  TEXT DEFAULT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_issue_cache_state
      ON linear_issue_cache(state_name);
  `);

  // Add context_mode column if it doesn't exist (migration for existing DBs)
  try {
    database.exec(
      `ALTER TABLE scheduled_tasks ADD COLUMN context_mode TEXT DEFAULT 'isolated'`,
    );
  } catch {
    /* column already exists */
  }

  // Add status_summary column to pipeline events (migration for existing DBs)
  try {
    database.exec(
      `ALTER TABLE linear_pipeline_events ADD COLUMN status_summary TEXT`,
    );
  } catch {
    /* column already exists */
  }

  // Add identifier column to linear_issue_prs (migration for existing DBs)
  try {
    database.exec(`ALTER TABLE linear_issue_prs ADD COLUMN identifier TEXT`);
  } catch {
    /* column already exists */
  }

  // Add is_bot_message column if it doesn't exist (migration for existing DBs)
  try {
    database.exec(
      `ALTER TABLE messages ADD COLUMN is_bot_message INTEGER DEFAULT 0`,
    );
    // Backfill: mark existing bot messages that used the content prefix pattern
    database
      .prepare(`UPDATE messages SET is_bot_message = 1 WHERE content LIKE ?`)
      .run(`${ASSISTANT_NAME}:%`);
  } catch {
    /* column already exists */
  }

  // Add is_main column if it doesn't exist (migration for existing DBs)
  try {
    database.exec(
      `ALTER TABLE registered_groups ADD COLUMN is_main INTEGER DEFAULT 0`,
    );
    // Backfill: existing rows with folder = 'main' are the main group
    database.exec(
      `UPDATE registered_groups SET is_main = 1 WHERE folder = 'main'`,
    );
  } catch {
    /* column already exists */
  }

  // Add project_id column to registered_groups (migration for existing DBs)
  try {
    database.exec(
      `ALTER TABLE registered_groups ADD COLUMN project_id TEXT REFERENCES projects(id)`,
    );
  } catch {
    /* column already exists */
  }

  try {
    database.exec(`ALTER TABLE scheduled_tasks ADD COLUMN agent_backend TEXT`);
  } catch {
    /* column already exists */
  }

  ensureAuditableBackendSessions(database);

  try {
    database.exec(`ALTER TABLE sessions ADD COLUMN last_compacted_at TEXT`);
  } catch {
    /* column already exists */
  }

  // Create projects table if it doesn't exist (migration for existing DBs)
  database.exec(`
    CREATE TABLE IF NOT EXISTS projects (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      path TEXT NOT NULL UNIQUE,
      type TEXT,
      readonly INTEGER DEFAULT 0,
      allow_external_push INTEGER DEFAULT 0,
      created_at TEXT NOT NULL
    );
  `);

  // Add allow_external_push column to projects (migration for existing DBs).
  // Default 0 = external projects cannot push/merge unless explicitly allowlisted.
  try {
    database.exec(
      `ALTER TABLE projects ADD COLUMN allow_external_push INTEGER DEFAULT 0`,
    );
  } catch {
    /* column already exists */
  }

  // Add channel and is_group columns if they don't exist (migration for existing DBs)
  try {
    database.exec(`ALTER TABLE chats ADD COLUMN channel TEXT`);
    database.exec(`ALTER TABLE chats ADD COLUMN is_group INTEGER DEFAULT 0`);
    // Backfill from JID patterns
    database.exec(
      `UPDATE chats SET channel = 'whatsapp', is_group = 1 WHERE jid LIKE '%@g.us'`,
    );
    database.exec(
      `UPDATE chats SET channel = 'whatsapp', is_group = 0 WHERE jid LIKE '%@s.whatsapp.net'`,
    );
    database.exec(
      `UPDATE chats SET channel = 'discord', is_group = 1 WHERE jid LIKE 'dc:%'`,
    );
    database.exec(
      `UPDATE chats SET channel = 'telegram', is_group = 1 WHERE jid LIKE 'tg:%'`,
    );
  } catch {
    /* columns already exist */
  }
}

function ensureAuditableBackendSessions(database: Database.Database): void {
  const columns = database
    .prepare(`PRAGMA table_info(sessions)`)
    .all() as Array<{ name: string; pk: number }>;
  const columnNames = new Set(columns.map((col) => col.name));
  const hasAuditableSchema =
    columnNames.has('id') &&
    columnNames.has('backend') &&
    columnNames.has('resume_cursor') &&
    columnNames.has('metadata_json') &&
    columnNames.has('last_used_at') &&
    columnNames.has('orphaned_at') &&
    columnNames.has('orphan_reason');

  if (hasAuditableSchema) {
    database.exec(`
      CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_active_backend
        ON sessions(group_folder, backend)
        WHERE orphaned_at IS NULL;
    `);
    return;
  }

  let legacyTable = 'sessions_legacy_backend_migration';
  const legacyExists = database
    .prepare(`SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?`)
    .get(legacyTable);
  if (legacyExists) {
    legacyTable = `sessions_legacy_backend_migration_${Date.now()}`;
  }

  const backendExpr = columnNames.has('backend')
    ? `COALESCE(backend, 'claude')`
    : `'claude'`;
  const resumeCursorExpr = columnNames.has('resume_cursor')
    ? 'resume_cursor'
    : 'NULL';
  const metadataExpr = columnNames.has('metadata_json')
    ? 'metadata_json'
    : 'NULL';
  const lastUsedExpr = columnNames.has('last_used_at')
    ? 'last_used_at'
    : 'NULL';
  const orphanedAtExpr = columnNames.has('orphaned_at')
    ? 'orphaned_at'
    : 'NULL';
  const orphanReasonExpr = columnNames.has('orphan_reason')
    ? 'orphan_reason'
    : 'NULL';

  database.exec(`ALTER TABLE sessions RENAME TO ${legacyTable};`);
  database.exec(`
    CREATE TABLE sessions (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      group_folder TEXT NOT NULL,
      session_id TEXT NOT NULL,
      backend TEXT DEFAULT 'claude',
      resume_cursor TEXT,
      metadata_json TEXT,
      last_used_at TEXT,
      orphaned_at TEXT,
      orphan_reason TEXT
    );
  `);
  database.exec(`
    INSERT INTO sessions (
      group_folder,
      session_id,
      backend,
      resume_cursor,
      metadata_json,
      last_used_at,
      orphaned_at,
      orphan_reason
    )
    SELECT
      group_folder,
      session_id,
      ${backendExpr},
      ${resumeCursorExpr},
      ${metadataExpr},
      ${lastUsedExpr},
      ${orphanedAtExpr},
      ${orphanReasonExpr}
    FROM ${legacyTable};
  `);
  database.exec(`
    CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_active_backend
      ON sessions(group_folder, backend)
      WHERE orphaned_at IS NULL;
  `);
}

export function initDatabase(): void {
  const dbPath = path.join(STORE_DIR, 'messages.db');
  fs.mkdirSync(path.dirname(dbPath), { recursive: true });

  db = new Database(dbPath);
  // WAL: allows concurrent reader (CLI dashboard) + writer (webhook server) without blocking
  db.pragma('journal_mode = WAL');
  createSchema(db);

  // Migrate from JSON files if they exist
  migrateJsonState();
}

/** @internal - for tests only. Creates a fresh in-memory database. */
export function _initTestDatabase(): void {
  db = new Database(':memory:');
  createSchema(db);
}

/**
 * Store chat metadata only (no message content).
 * Used for all chats to enable group discovery without storing sensitive content.
 */
export function storeChatMetadata(
  chatJid: string,
  timestamp: string,
  name?: string,
  channel?: string,
  isGroup?: boolean,
): void {
  const ch = channel ?? null;
  const group = isGroup === undefined ? null : isGroup ? 1 : 0;

  if (name) {
    // Update with name, preserving existing timestamp if newer
    db.prepare(
      `
      INSERT INTO chats (jid, name, last_message_time, channel, is_group) VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(jid) DO UPDATE SET
        name = excluded.name,
        last_message_time = MAX(last_message_time, excluded.last_message_time),
        channel = COALESCE(excluded.channel, channel),
        is_group = COALESCE(excluded.is_group, is_group)
    `,
    ).run(chatJid, name, timestamp, ch, group);
  } else {
    // Update timestamp only, preserve existing name if any
    db.prepare(
      `
      INSERT INTO chats (jid, name, last_message_time, channel, is_group) VALUES (?, ?, ?, ?, ?)
      ON CONFLICT(jid) DO UPDATE SET
        last_message_time = MAX(last_message_time, excluded.last_message_time),
        channel = COALESCE(excluded.channel, channel),
        is_group = COALESCE(excluded.is_group, is_group)
    `,
    ).run(chatJid, chatJid, timestamp, ch, group);
  }
}

/**
 * Update chat name without changing timestamp for existing chats.
 * New chats get the current time as their initial timestamp.
 * Used during group metadata sync.
 */
export function updateChatName(chatJid: string, name: string): void {
  db.prepare(
    `
    INSERT INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)
    ON CONFLICT(jid) DO UPDATE SET name = excluded.name
  `,
  ).run(chatJid, name, new Date().toISOString());
}

export interface ChatInfo {
  jid: string;
  name: string;
  last_message_time: string;
  channel: string;
  is_group: number;
}

/**
 * Get all known chats, ordered by most recent activity.
 */
export function getAllChats(): ChatInfo[] {
  return db
    .prepare(
      `
    SELECT jid, name, last_message_time, channel, is_group
    FROM chats
    ORDER BY last_message_time DESC
  `,
    )
    .all() as ChatInfo[];
}

/**
 * Get timestamp of last group metadata sync.
 */
export function getLastGroupSync(): string | null {
  // Store sync time in a special chat entry
  const row = db
    .prepare(`SELECT last_message_time FROM chats WHERE jid = '__group_sync__'`)
    .get() as { last_message_time: string } | undefined;
  return row?.last_message_time || null;
}

/**
 * Record that group metadata was synced.
 */
export function setLastGroupSync(): void {
  const now = new Date().toISOString();
  db.prepare(
    `INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES ('__group_sync__', '__group_sync__', ?)`,
  ).run(now);
}

/**
 * Store a message with full content.
 * Only call this for registered groups where message history is needed.
 */
export function storeMessage(msg: NewMessage): void {
  db.prepare(
    `INSERT OR REPLACE INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    msg.id,
    msg.chat_jid,
    msg.sender,
    msg.sender_name,
    msg.content,
    msg.timestamp,
    msg.is_from_me ? 1 : 0,
    msg.is_bot_message ? 1 : 0,
  );
}

/**
 * Store a message directly.
 */
export function storeMessageDirect(msg: {
  id: string;
  chat_jid: string;
  sender: string;
  sender_name: string;
  content: string;
  timestamp: string;
  is_from_me: boolean;
  is_bot_message?: boolean;
}): void {
  db.prepare(
    `INSERT OR REPLACE INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message) VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    msg.id,
    msg.chat_jid,
    msg.sender,
    msg.sender_name,
    msg.content,
    msg.timestamp,
    msg.is_from_me ? 1 : 0,
    msg.is_bot_message ? 1 : 0,
  );
}

export function getNewMessages(
  jids: string[],
  lastTimestamp: string,
  botPrefix: string,
  limit: number = 50,
): { messages: NewMessage[]; newTimestamp: string } {
  if (jids.length === 0) return { messages: [], newTimestamp: lastTimestamp };

  const placeholders = jids.map(() => '?').join(',');
  // Filter bot messages using both the is_bot_message flag AND the content
  // prefix as a backstop for messages written before the migration ran.
  // Subquery takes the N most recent, outer query re-sorts chronologically.
  const sql = `
    SELECT * FROM (
      SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me
      FROM messages
      WHERE timestamp > ? AND chat_jid IN (${placeholders})
        AND is_bot_message = 0 AND content NOT LIKE ?
        AND content != '' AND content IS NOT NULL
      ORDER BY timestamp DESC
      LIMIT ?
    ) ORDER BY timestamp
  `;

  const rows = db
    .prepare(sql)
    .all(lastTimestamp, ...jids, `${botPrefix}:%`, limit) as NewMessage[];

  let newTimestamp = lastTimestamp;
  for (const row of rows) {
    if (row.timestamp > newTimestamp) newTimestamp = row.timestamp;
  }

  return { messages: rows, newTimestamp };
}

export function getMessagesSince(
  chatJid: string,
  sinceTimestamp: string,
  botPrefix: string,
  limit: number = 50,
  includeBotMessages: boolean = false,
): NewMessage[] {
  // Subquery takes the N most recent, outer query re-sorts chronologically.
  const sql = includeBotMessages
    ? `SELECT * FROM (
         SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me
         FROM messages
         WHERE chat_jid = ? AND timestamp > ?
           AND content != '' AND content IS NOT NULL
         ORDER BY timestamp DESC
         LIMIT ?
       ) ORDER BY timestamp`
    : `SELECT * FROM (
         SELECT id, chat_jid, sender, sender_name, content, timestamp, is_from_me
         FROM messages
         WHERE chat_jid = ? AND timestamp > ?
           AND is_bot_message = 0 AND content NOT LIKE ?
           AND content != '' AND content IS NOT NULL
         ORDER BY timestamp DESC
         LIMIT ?
       ) ORDER BY timestamp`;

  const params = includeBotMessages
    ? [chatJid, sinceTimestamp, limit]
    : [chatJid, sinceTimestamp, `${botPrefix}:%`, limit];

  return db.prepare(sql).all(...params) as NewMessage[];
}

export function createTask(
  task: Omit<ScheduledTask, 'last_run' | 'last_result'>,
): void {
  db.prepare(
    `
    INSERT INTO scheduled_tasks (id, group_folder, chat_jid, prompt, schedule_type, schedule_value, context_mode, next_run, status, created_at, agent_backend)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `,
  ).run(
    task.id,
    task.group_folder,
    task.chat_jid,
    task.prompt,
    task.schedule_type,
    task.schedule_value,
    task.context_mode || 'isolated',
    task.next_run,
    task.status,
    task.created_at,
    task.agent_backend ?? null,
  );
}

export function getTaskById(id: string): ScheduledTask | undefined {
  return db.prepare('SELECT * FROM scheduled_tasks WHERE id = ?').get(id) as
    | ScheduledTask
    | undefined;
}

export function getTasksForGroup(groupFolder: string): ScheduledTask[] {
  return db
    .prepare(
      'SELECT * FROM scheduled_tasks WHERE group_folder = ? ORDER BY created_at DESC',
    )
    .all(groupFolder) as ScheduledTask[];
}

export function getAllTasks(): ScheduledTask[] {
  return db
    .prepare('SELECT * FROM scheduled_tasks ORDER BY created_at DESC')
    .all() as ScheduledTask[];
}

export function updateTask(
  id: string,
  updates: Partial<
    Pick<
      ScheduledTask,
      | 'prompt'
      | 'schedule_type'
      | 'schedule_value'
      | 'next_run'
      | 'status'
      | 'agent_backend'
    >
  >,
): void {
  const fields: string[] = [];
  const values: unknown[] = [];

  if (updates.prompt !== undefined) {
    fields.push('prompt = ?');
    values.push(updates.prompt);
  }
  if (updates.schedule_type !== undefined) {
    fields.push('schedule_type = ?');
    values.push(updates.schedule_type);
  }
  if (updates.schedule_value !== undefined) {
    fields.push('schedule_value = ?');
    values.push(updates.schedule_value);
  }
  if (updates.next_run !== undefined) {
    fields.push('next_run = ?');
    values.push(updates.next_run);
  }
  if (updates.status !== undefined) {
    fields.push('status = ?');
    values.push(updates.status);
  }
  if (updates.agent_backend !== undefined) {
    fields.push('agent_backend = ?');
    values.push(updates.agent_backend);
  }

  if (fields.length === 0) return;

  values.push(id);
  db.prepare(
    `UPDATE scheduled_tasks SET ${fields.join(', ')} WHERE id = ?`,
  ).run(...values);
}

export function deleteTask(id: string): void {
  // Delete child records first (FK constraint)
  db.prepare('DELETE FROM task_run_logs WHERE task_id = ?').run(id);
  db.prepare('DELETE FROM scheduled_tasks WHERE id = ?').run(id);
}

export function getDueTasks(): ScheduledTask[] {
  const now = new Date().toISOString();
  return db
    .prepare(
      `
    SELECT * FROM scheduled_tasks
    WHERE status = 'active' AND next_run IS NOT NULL AND next_run <= ?
    ORDER BY next_run
  `,
    )
    .all(now) as ScheduledTask[];
}

export function updateTaskAfterRun(
  id: string,
  nextRun: string | null,
  lastResult: string,
): void {
  const now = new Date().toISOString();
  db.prepare(
    `
    UPDATE scheduled_tasks
    SET next_run = ?, last_run = ?, last_result = ?, status = CASE WHEN ? IS NULL THEN 'completed' ELSE status END
    WHERE id = ?
  `,
  ).run(nextRun, now, lastResult, nextRun, id);
}

export function logTaskRun(log: TaskRunLog): void {
  db.prepare(
    `
    INSERT INTO task_run_logs (task_id, run_at, duration_ms, status, result, error)
    VALUES (?, ?, ?, ?, ?, ?)
  `,
  ).run(
    log.task_id,
    log.run_at,
    log.duration_ms,
    log.status,
    log.result,
    log.error,
  );
}

// --- Router state accessors ---

export function getRouterState(key: string): string | undefined {
  const row = db
    .prepare('SELECT value FROM router_state WHERE key = ?')
    .get(key) as { value: string } | undefined;
  return row?.value;
}

export function setRouterState(key: string, value: string): void {
  db.prepare(
    'INSERT OR REPLACE INTO router_state (key, value) VALUES (?, ?)',
  ).run(key, value);
}

// --- Session accessors ---

function rowToSessionRef(row: {
  session_id: string;
  backend: string | null;
  resume_cursor: string | null;
  metadata_json: string | null;
}): RuntimeSession {
  // Use exported parseAgentBackend as the canonical accepted-value gate.
  // Falls back to 'claude' for null or unrecognized values (e.g., a typo or
  // a stored backend ID from a future Deus version we don't yet recognize).
  return {
    backend: parseAgentBackend(row.backend) ?? 'claude',
    session_id: row.session_id,
    resume_cursor: row.resume_cursor ?? undefined,
    metadata_json: row.metadata_json ?? undefined,
  };
}

export function getSession(
  groupFolder: string,
  backend?: AgentRuntimeId,
): RuntimeSession | undefined {
  const row = (
    backend
      ? db
          .prepare(
            `SELECT session_id, backend, resume_cursor, metadata_json
           FROM sessions
           WHERE group_folder = ? AND backend = ? AND orphaned_at IS NULL`,
          )
          .get(groupFolder, backend)
      : db
          .prepare(
            `SELECT session_id, backend, resume_cursor, metadata_json
           FROM sessions
           WHERE group_folder = ? AND orphaned_at IS NULL
           ORDER BY last_used_at DESC
           LIMIT 1`,
          )
          .get(groupFolder)
  ) as
    | {
        session_id: string;
        backend: string | null;
        resume_cursor: string | null;
        metadata_json: string | null;
      }
    | undefined;
  if (!row) return undefined;
  return rowToSessionRef(row);
}

export function setSession(
  groupFolder: string,
  session: string | RuntimeSession,
): void {
  const ref =
    typeof session === 'string' ? defaultSession(session, 'claude') : session;
  const now = new Date().toISOString();
  const resumeCursor = ref.resume_cursor ?? null;
  const metadataJson = ref.metadata_json ?? null;

  db.transaction(() => {
    const active = db
      .prepare(
        `SELECT id, session_id, resume_cursor, metadata_json
         FROM sessions
         WHERE group_folder = ? AND backend = ? AND orphaned_at IS NULL
         ORDER BY last_used_at DESC
         LIMIT 1`,
      )
      .get(groupFolder, ref.backend) as
      | {
          id: number;
          session_id: string;
          resume_cursor: string | null;
          metadata_json: string | null;
        }
      | undefined;

    if (
      active &&
      active.session_id === ref.session_id &&
      active.resume_cursor === resumeCursor &&
      active.metadata_json === metadataJson
    ) {
      db.prepare(`UPDATE sessions SET last_used_at = ? WHERE id = ?`).run(
        now,
        active.id,
      );
      return;
    }

    if (active) {
      db.prepare(
        `UPDATE sessions
         SET orphaned_at = ?, orphan_reason = ?
         WHERE id = ?`,
      ).run(now, 'superseded', active.id);
    }

    db.prepare(
      `INSERT INTO sessions (
        group_folder,
        session_id,
        backend,
        resume_cursor,
        metadata_json,
        last_used_at,
        orphaned_at,
        orphan_reason
      ) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)`,
    ).run(
      groupFolder,
      ref.session_id,
      ref.backend,
      resumeCursor,
      metadataJson,
      now,
    );
  })();
}

export function clearSession(
  groupFolder: string,
  backend?: AgentRuntimeId,
): void {
  const now = new Date().toISOString();
  if (backend) {
    db.prepare(
      `UPDATE sessions
       SET orphaned_at = ?, orphan_reason = ?
       WHERE group_folder = ? AND backend = ? AND orphaned_at IS NULL`,
    ).run(now, 'cleared', groupFolder, backend);
    return;
  }
  db.prepare(
    `UPDATE sessions
     SET orphaned_at = ?, orphan_reason = ?
     WHERE group_folder = ? AND orphaned_at IS NULL`,
  ).run(now, 'cleared', groupFolder);
}

export function getSessionLastUsedAt(
  groupFolder: string,
  backend?: AgentRuntimeId,
): string | undefined {
  const row = (
    backend
      ? db
          .prepare(
            `SELECT last_used_at
           FROM sessions
           WHERE group_folder = ? AND backend = ? AND orphaned_at IS NULL`,
          )
          .get(groupFolder, backend)
      : db
          .prepare(
            `SELECT last_used_at
           FROM sessions
           WHERE group_folder = ? AND orphaned_at IS NULL
           ORDER BY last_used_at DESC
           LIMIT 1`,
          )
          .get(groupFolder)
  ) as { last_used_at: string | null } | undefined;
  return row?.last_used_at ?? undefined;
}

export function setLastCompactedAt(groupFolder: string, backend: string): void {
  const now = new Date().toISOString();
  db.prepare(
    `UPDATE sessions SET last_compacted_at = ?
     WHERE group_folder = ? AND backend = ? AND orphaned_at IS NULL`,
  ).run(now, groupFolder, backend);
}

export function getLastCompactedAt(
  groupFolder: string,
  backend?: string,
): string | undefined {
  const row = (
    backend
      ? db
          .prepare(
            `SELECT last_compacted_at FROM sessions
             WHERE group_folder = ? AND backend = ? AND orphaned_at IS NULL`,
          )
          .get(groupFolder, backend)
      : db
          .prepare(
            `SELECT last_compacted_at FROM sessions
             WHERE group_folder = ? AND orphaned_at IS NULL
             ORDER BY last_used_at DESC LIMIT 1`,
          )
          .get(groupFolder)
  ) as { last_compacted_at: string | null } | undefined;
  return row?.last_compacted_at ?? undefined;
}

export function getAllSessions(): Record<string, RuntimeSession> {
  const rows = db
    .prepare(
      `SELECT group_folder, session_id, backend, resume_cursor, metadata_json
       FROM sessions
       WHERE orphaned_at IS NULL
       ORDER BY group_folder, last_used_at DESC`,
    )
    .all() as Array<{
    group_folder: string;
    session_id: string;
    backend: string | null;
    resume_cursor: string | null;
    metadata_json: string | null;
  }>;
  const result: Record<string, RuntimeSession> = {};
  for (const row of rows) {
    if (!result[row.group_folder]) {
      result[row.group_folder] = rowToSessionRef(row);
    }
  }
  return result;
}

export function getAllBackendSessions(): Record<
  string,
  Partial<Record<AgentRuntimeId, RuntimeSession>>
> {
  const rows = db
    .prepare(
      `SELECT group_folder, session_id, backend, resume_cursor, metadata_json
       FROM sessions
       WHERE orphaned_at IS NULL`,
    )
    .all() as Array<{
    group_folder: string;
    session_id: string;
    backend: string | null;
    resume_cursor: string | null;
    metadata_json: string | null;
  }>;
  const result: Record<
    string,
    Partial<Record<AgentRuntimeId, RuntimeSession>>
  > = {};
  for (const row of rows) {
    const ref = rowToSessionRef(row);
    result[row.group_folder] ??= {};
    result[row.group_folder][ref.backend] = ref;
  }
  return result;
}

// --- Registered group accessors ---

export function getRegisteredGroup(
  jid: string,
): (RegisteredGroup & { jid: string }) | undefined {
  const row = db
    .prepare('SELECT * FROM registered_groups WHERE jid = ?')
    .get(jid) as
    | {
        jid: string;
        name: string;
        folder: string;
        trigger_pattern: string;
        added_at: string;
        container_config: string | null;
        requires_trigger: number | null;
        is_main: number | null;
        project_id: string | null;
      }
    | undefined;
  if (!row) return undefined;
  if (!isValidGroupFolder(row.folder)) {
    logger.warn(
      { jid: row.jid, folder: row.folder },
      'Skipping registered group with invalid folder',
    );
    return undefined;
  }
  return {
    jid: row.jid,
    name: row.name,
    folder: row.folder,
    trigger: row.trigger_pattern,
    added_at: row.added_at,
    containerConfig: row.container_config
      ? JSON.parse(row.container_config)
      : undefined,
    requiresTrigger:
      row.requires_trigger === null ? undefined : row.requires_trigger === 1,
    isControlGroup: row.is_main === 1 ? true : undefined,
    projectId: row.project_id ?? undefined,
  };
}

export function getRegisteredGroupByFolder(
  folder: string,
): (RegisteredGroup & { jid: string }) | undefined {
  // registered_groups.folder is UNIQUE, so this resolves at most one row.
  // Used by the tool-proxy push/merge gate to map a request's group folder
  // (from validateGroupToken) to its project association.
  const row = db
    .prepare('SELECT * FROM registered_groups WHERE folder = ?')
    .get(folder) as
    | {
        jid: string;
        name: string;
        folder: string;
        trigger_pattern: string;
        added_at: string;
        container_config: string | null;
        requires_trigger: number | null;
        is_main: number | null;
        project_id: string | null;
      }
    | undefined;
  if (!row) return undefined;
  if (!isValidGroupFolder(row.folder)) {
    logger.warn(
      { jid: row.jid, folder: row.folder },
      'Skipping registered group with invalid folder',
    );
    return undefined;
  }
  return {
    jid: row.jid,
    name: row.name,
    folder: row.folder,
    trigger: row.trigger_pattern,
    added_at: row.added_at,
    containerConfig: row.container_config
      ? JSON.parse(row.container_config)
      : undefined,
    requiresTrigger:
      row.requires_trigger === null ? undefined : row.requires_trigger === 1,
    isControlGroup: row.is_main === 1 ? true : undefined,
    projectId: row.project_id ?? undefined,
  };
}

export function setRegisteredGroup(jid: string, group: RegisteredGroup): void {
  if (!isValidGroupFolder(group.folder)) {
    throw new Error(`Invalid group folder "${group.folder}" for JID ${jid}`);
  }
  db.prepare(
    `INSERT OR REPLACE INTO registered_groups (jid, name, folder, trigger_pattern, added_at, container_config, requires_trigger, is_main, project_id)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    jid,
    group.name,
    group.folder,
    group.trigger,
    group.added_at,
    group.containerConfig ? JSON.stringify(group.containerConfig) : null,
    group.requiresTrigger === undefined ? 1 : group.requiresTrigger ? 1 : 0,
    group.isControlGroup ? 1 : 0,
    group.projectId ?? null,
  );
}

export function getAllRegisteredGroups(): Record<string, RegisteredGroup> {
  const rows = db.prepare('SELECT * FROM registered_groups').all() as Array<{
    jid: string;
    name: string;
    folder: string;
    trigger_pattern: string;
    added_at: string;
    container_config: string | null;
    requires_trigger: number | null;
    is_main: number | null;
    project_id: string | null;
  }>;
  const result: Record<string, RegisteredGroup> = {};
  for (const row of rows) {
    if (!isValidGroupFolder(row.folder)) {
      logger.warn(
        { jid: row.jid, folder: row.folder },
        'Skipping registered group with invalid folder',
      );
      continue;
    }
    result[row.jid] = {
      name: row.name,
      folder: row.folder,
      trigger: row.trigger_pattern,
      added_at: row.added_at,
      containerConfig: row.container_config
        ? JSON.parse(row.container_config)
        : undefined,
      requiresTrigger:
        row.requires_trigger === null ? undefined : row.requires_trigger === 1,
      isControlGroup: row.is_main === 1 ? true : undefined,
      projectId: row.project_id ?? undefined,
    };
  }
  return result;
}

// --- Project accessors ---

export function createProject(project: ProjectConfig): void {
  db.prepare(
    `INSERT INTO projects (id, name, path, type, readonly, allow_external_push, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)`,
  ).run(
    project.id,
    project.name,
    project.path,
    project.type ? JSON.stringify(project.type) : null,
    project.readonly ? 1 : 0,
    project.allow_external_push ? 1 : 0,
    project.created_at,
  );
}

/**
 * Allowlist (or revoke) an external project for git push/merge through the
 * tool-proxy. Default is blocked; flip to true to let a trusted external
 * project push/merge. (No general updateProject exists; this is the dedicated
 * writer for the one mutable policy flag.) The operator-facing command/skill
 * that wraps this is deferred to LIA-180; until then flip it via this writer.
 */
export function setProjectAllowExternalPush(
  id: string,
  allowed: boolean,
): void {
  db.prepare(`UPDATE projects SET allow_external_push = ? WHERE id = ?`).run(
    allowed ? 1 : 0,
    id,
  );
}

export function getProjectById(id: string): ProjectConfig | undefined {
  const row = db.prepare('SELECT * FROM projects WHERE id = ?').get(id) as
    | {
        id: string;
        name: string;
        path: string;
        type: string | null;
        readonly: number;
        allow_external_push: number;
        created_at: string;
      }
    | undefined;
  if (!row) return undefined;
  return {
    id: row.id,
    name: row.name,
    path: row.path,
    type: row.type ? JSON.parse(row.type) : null,
    readonly: row.readonly === 1,
    allow_external_push: row.allow_external_push === 1,
    created_at: row.created_at,
  };
}

export function getProjectByPath(hostPath: string): ProjectConfig | undefined {
  const row = db
    .prepare('SELECT * FROM projects WHERE path = ?')
    .get(hostPath) as
    | {
        id: string;
        name: string;
        path: string;
        type: string | null;
        readonly: number;
        allow_external_push: number;
        created_at: string;
      }
    | undefined;
  if (!row) return undefined;
  return {
    id: row.id,
    name: row.name,
    path: row.path,
    type: row.type ? JSON.parse(row.type) : null,
    readonly: row.readonly === 1,
    allow_external_push: row.allow_external_push === 1,
    created_at: row.created_at,
  };
}

export function getAllProjects(): ProjectConfig[] {
  const rows = db
    .prepare('SELECT * FROM projects ORDER BY created_at DESC')
    .all() as Array<{
    id: string;
    name: string;
    path: string;
    type: string | null;
    readonly: number;
    allow_external_push: number;
    created_at: string;
  }>;
  return rows.map((row) => ({
    id: row.id,
    name: row.name,
    path: row.path,
    type: row.type ? JSON.parse(row.type) : null,
    readonly: row.readonly === 1,
    allow_external_push: row.allow_external_push === 1,
    created_at: row.created_at,
  }));
}

export function deleteProject(id: string): void {
  // Dissociate all groups first
  db.prepare(
    `UPDATE registered_groups SET project_id = NULL WHERE project_id = ?`,
  ).run(id);
  db.prepare('DELETE FROM projects WHERE id = ?').run(id);
}

export function setGroupProject(
  groupFolder: string,
  projectId: string | null,
): void {
  db.prepare(
    `UPDATE registered_groups SET project_id = ? WHERE folder = ?`,
  ).run(projectId, groupFolder);
}

// --- Linear webhook event accessors ---

export function insertWebhookEvent(event: {
  event_key: string;
  issue_id: string;
  gate_to: string;
  from_state_id: string;
  to_state_id: string;
  webhook_ts: string;
}): boolean {
  const result = db
    .prepare(
      `INSERT OR IGNORE INTO linear_webhook_events
       (event_key, issue_id, gate_to, from_state_id, to_state_id, webhook_ts, status)
       VALUES (?, ?, ?, ?, ?, ?, 'pending')`,
    )
    .run(
      event.event_key,
      event.issue_id,
      event.gate_to,
      event.from_state_id,
      event.to_state_id,
      event.webhook_ts,
    );
  return result.changes > 0;
}

export function updateWebhookEventStatus(
  eventKey: string,
  status: 'running' | 'done' | 'error',
  extra?: { verdict?: string; error?: string },
): void {
  const now = new Date().toISOString();
  if (status === 'running') {
    db.prepare(
      `UPDATE linear_webhook_events SET status = ?, started_at = ? WHERE event_key = ?`,
    ).run(status, now, eventKey);
  } else {
    db.prepare(
      `UPDATE linear_webhook_events
       SET status = ?, finished_at = ?, verdict = COALESCE(?, verdict), error = COALESCE(?, error)
       WHERE event_key = ?`,
    ).run(status, now, extra?.verdict ?? null, extra?.error ?? null, eventKey);
  }
}

export function getLastCompletedGateRun(
  issueId: string,
  gateTo: string,
): { finished_at: string; verdict: string } | undefined {
  return db
    .prepare(
      `SELECT finished_at, verdict FROM linear_webhook_events
       WHERE issue_id = ? AND gate_to = ? AND status = 'done'
       ORDER BY finished_at DESC, rowid DESC LIMIT 1`,
    )
    .get(issueId, gateTo) as
    | { finished_at: string; verdict: string }
    | undefined;
}

export function upsertGateComment(
  issueId: string,
  gateTo: string,
  commentId: string,
): void {
  db.prepare(
    `INSERT INTO linear_gate_comments (issue_id, gate_to, comment_id, updated_at)
     VALUES (?, ?, ?, ?)
     ON CONFLICT(issue_id, gate_to) DO UPDATE SET
       comment_id = excluded.comment_id,
       updated_at = excluded.updated_at`,
  ).run(issueId, gateTo, commentId, new Date().toISOString());
}

export function getGateCommentId(
  issueId: string,
  gateTo: string,
): string | undefined {
  const row = db
    .prepare(
      `SELECT comment_id FROM linear_gate_comments WHERE issue_id = ? AND gate_to = ?`,
    )
    .get(issueId, gateTo) as { comment_id: string } | undefined;
  return row?.comment_id;
}

// --- Linear gate meta accessors ---

export interface GateMeta {
  issueId: string;
  gateName: string;
  enrichmentHash: string | null;
  enrichmentSnapshot: string | null;
  attemptCount: number;
  reviseHistory: string[];
  shippedAt: string | null;
}

export function upsertGateMeta(
  issueId: string,
  gateName: string,
  fields: {
    enrichmentHash?: string;
    enrichmentSnapshot?: string;
    shippedAt?: string;
    resetReviseHistory?: boolean;
  },
): void {
  const reviseHistory = fields.resetReviseHistory ? '[]' : null;
  db.prepare(
    `INSERT INTO linear_gate_meta (issue_id, gate_name, enrichment_hash, enrichment_snapshot, shipped_at, revise_history, attempt_count)
     VALUES (?, ?, ?, ?, ?, ?, 0)
     ON CONFLICT(issue_id, gate_name) DO UPDATE SET
       enrichment_hash = COALESCE(excluded.enrichment_hash, linear_gate_meta.enrichment_hash),
       enrichment_snapshot = COALESCE(excluded.enrichment_snapshot, linear_gate_meta.enrichment_snapshot),
       shipped_at = COALESCE(excluded.shipped_at, linear_gate_meta.shipped_at),
       revise_history = COALESCE(excluded.revise_history, linear_gate_meta.revise_history),
       attempt_count = CASE WHEN excluded.shipped_at IS NOT NULL THEN 0 ELSE linear_gate_meta.attempt_count END`,
  ).run(
    issueId,
    gateName,
    fields.enrichmentHash ?? null,
    fields.enrichmentSnapshot ?? null,
    fields.shippedAt ?? null,
    reviseHistory,
  );
}

export function getGateMeta(
  issueId: string,
  gateName: string,
): GateMeta | null {
  const row = db
    .prepare(
      `SELECT issue_id, gate_name, enrichment_hash, enrichment_snapshot, attempt_count, revise_history, shipped_at
       FROM linear_gate_meta WHERE issue_id = ? AND gate_name = ?`,
    )
    .get(issueId, gateName) as
    | {
        issue_id: string;
        gate_name: string;
        enrichment_hash: string | null;
        enrichment_snapshot: string | null;
        attempt_count: number;
        revise_history: string | null;
        shipped_at: string | null;
      }
    | undefined;
  if (!row) return null;
  let history: string[];
  try {
    history = row.revise_history ? JSON.parse(row.revise_history) : [];
  } catch {
    history = [];
  }
  return {
    issueId: row.issue_id,
    gateName: row.gate_name,
    enrichmentHash: row.enrichment_hash,
    enrichmentSnapshot: row.enrichment_snapshot,
    attemptCount: row.attempt_count,
    reviseHistory: history,
    shippedAt: row.shipped_at,
  };
}

export function incrementAttemptCount(
  issueId: string,
  gateName: string,
): number {
  db.prepare(
    `INSERT INTO linear_gate_meta (issue_id, gate_name, attempt_count)
     VALUES (?, ?, 1)
     ON CONFLICT(issue_id, gate_name) DO UPDATE SET
       attempt_count = linear_gate_meta.attempt_count + 1`,
  ).run(issueId, gateName);
  const row = db
    .prepare(
      `SELECT attempt_count FROM linear_gate_meta WHERE issue_id = ? AND gate_name = ?`,
    )
    .get(issueId, gateName) as { attempt_count: number } | undefined;
  return row?.attempt_count ?? 1;
}

export function appendReviseHistory(
  issueId: string,
  gateName: string,
  summary: string,
): void {
  const meta = getGateMeta(issueId, gateName);
  const history = meta?.reviseHistory ?? [];
  history.push(summary);
  db.prepare(
    `INSERT INTO linear_gate_meta (issue_id, gate_name, revise_history)
     VALUES (?, ?, ?)
     ON CONFLICT(issue_id, gate_name) DO UPDATE SET
       revise_history = excluded.revise_history`,
  ).run(issueId, gateName, JSON.stringify(history));
}

export function computeEnrichmentHash(content: string): string {
  const normalized = content.replace(/\s+/g, ' ').trim();
  return createHash('sha256').update(normalized).digest('hex');
}

// --- Linear issue PR accessors ---

export function upsertIssuePr(
  issueId: string,
  prUrl: string,
  branch?: string,
  identifier?: string,
): void {
  const now = new Date().toISOString();
  db.prepare(
    `INSERT INTO linear_issue_prs (issue_id, pr_url, branch, identifier, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?)
     ON CONFLICT(issue_id) DO UPDATE SET
       pr_url = excluded.pr_url,
       branch = COALESCE(excluded.branch, linear_issue_prs.branch),
       identifier = COALESCE(excluded.identifier, linear_issue_prs.identifier),
       updated_at = excluded.updated_at`,
  ).run(issueId, prUrl, branch ?? null, identifier ?? null, now, now);
}

export function getIssuePr(
  issueId: string,
):
  | { pr_url: string; branch: string | null; auto_merge_state: string }
  | undefined {
  return db
    .prepare(
      `SELECT pr_url, branch, auto_merge_state FROM linear_issue_prs WHERE issue_id = ?`,
    )
    .get(issueId) as
    | { pr_url: string; branch: string | null; auto_merge_state: string }
    | undefined;
}

export function updatePrAutoMergeState(
  issueId: string,
  state: 'none' | 'pending' | 'merged' | 'failed',
): void {
  db.prepare(
    `UPDATE linear_issue_prs SET auto_merge_state = ?, updated_at = ? WHERE issue_id = ?`,
  ).run(state, new Date().toISOString(), issueId);
}

export function getPendingAutoMerges(): Array<{
  issue_id: string;
  pr_url: string;
  branch: string | null;
  identifier: string | null;
}> {
  return db
    .prepare(
      `SELECT issue_id, pr_url, branch, identifier FROM linear_issue_prs WHERE auto_merge_state = 'pending'`,
    )
    .all() as Array<{
    issue_id: string;
    pr_url: string;
    branch: string | null;
    identifier: string | null;
  }>;
}

export function getOpenPrsForActiveIssues(): Array<{
  issue_id: string;
  pr_url: string;
  identifier: string | null;
}> {
  return db
    .prepare(
      `SELECT p.issue_id, p.pr_url, p.identifier
       FROM linear_issue_prs p
       JOIN linear_issue_cache c ON p.issue_id = c.issue_id
       WHERE c.state_name IN ('Agent Working', 'In Review')
         AND c.deleted_at IS NULL
         AND p.auto_merge_state != 'merged'`,
    )
    .all() as Array<{
    issue_id: string;
    pr_url: string;
    identifier: string | null;
  }>;
}

// --- Pipeline event accessors ---

/**
 * Raw INSERT of one pipeline-event row — the NON-emitting counterpart to
 * `logPipelineEvent`, used by the ObservabilitySink for loop-safe mirroring
 * (see `events/listeners/observability-sink.ts`). `createdAt` defaults to now.
 */
export function insertPipelineEventRow(
  issueId: string,
  identifier: string,
  eventType: string,
  detail?: string,
  createdAt?: string,
): number | undefined {
  try {
    const result = db
      .prepare(
        `INSERT INTO linear_pipeline_events (issue_id, identifier, event_type, detail, created_at)
       VALUES (?, ?, ?, ?, ?)`,
      )
      .run(
        issueId,
        identifier,
        eventType,
        detail ?? null,
        createdAt ?? new Date().toISOString(),
      );
    return Number(result.lastInsertRowid);
  } catch (err) {
    logger.debug({ issueId, eventType, err }, 'pipeline-event: insert failed');
    return undefined;
  }
}

export function logPipelineEvent(
  issueId: string,
  identifier: string,
  eventType: string,
  detail?: string,
): void {
  // Emit-only (Phase-3 cutover, LIA-166): the ObservabilitySink owns the durable
  // write off this `pipeline.transition`. notifyPipelineStep does NOT route here —
  // it inserts synchronously (it needs the rowid + the row present before its
  // updateUnifiedComment read). Emit is unconditional: the sink is the row-landing.
  // Never-throw is load-bearing (callers rely on it): the outer try/catch is a
  // structural no-throw guarantee; the inner .catch swallows async listener rejections.
  try {
    void getBus()
      .emit({
        type: 'pipeline.transition',
        source: 'db.logPipelineEvent',
        actor: 'system',
        correlationId: { kind: 'issue', id: issueId, identifier },
        ts: new Date().toISOString(),
        payload: { eventType, detail },
      })
      .catch(() => {});
  } catch {
    /* emit must never throw into the write path */
  }
}

export function updatePipelineEventStatusSummary(
  rowId: number,
  summary: string,
): void {
  try {
    db.prepare(
      `UPDATE linear_pipeline_events SET status_summary = ? WHERE id = ?`,
    ).run(summary, rowId);
  } catch (err) {
    logger.debug(
      { rowId, err },
      'pipeline-event: status summary update failed',
    );
  }
}

export function getLatestStatusSummary(issueId: string): string | null {
  const row = db
    .prepare(
      `SELECT status_summary FROM linear_pipeline_events
       WHERE issue_id = ? AND status_summary IS NOT NULL
       ORDER BY id DESC LIMIT 1`,
    )
    .get(issueId) as { status_summary: string } | undefined;
  return row?.status_summary ?? null;
}

export function getReviseCount(issueId: string): number {
  const row = db
    .prepare(
      `SELECT COUNT(*) as cnt FROM linear_pipeline_events
       WHERE issue_id = ? AND event_type = 'gate_revise'`,
    )
    .get(issueId) as { cnt: number };
  return row.cnt;
}

export const CIRCUIT_BREAKER_THRESHOLD = 3;

export function getConsecutiveFailCount(
  issueId: string,
  eventType: string,
): number {
  const row = db
    .prepare(
      `SELECT COUNT(*) as cnt FROM linear_pipeline_events
       WHERE issue_id = ? AND event_type = ?
       AND rowid > COALESCE(
         (SELECT MAX(rowid) FROM linear_pipeline_events
          WHERE issue_id = ? AND event_type IN ('agent_completed', 'circuit_breaker_reset')),
         0
       )`,
    )
    .get(issueId, eventType, issueId) as { cnt: number };
  return row.cnt;
}

export function getLastFailTime(
  issueId: string,
  eventType: string,
): string | null {
  const row = db
    .prepare(
      `SELECT created_at FROM linear_pipeline_events
       WHERE issue_id = ? AND event_type = ?
       AND rowid > COALESCE(
         (SELECT MAX(rowid) FROM linear_pipeline_events
          WHERE issue_id = ? AND event_type IN ('agent_completed', 'circuit_breaker_reset')),
         0
       )
       ORDER BY rowid DESC LIMIT 1`,
    )
    .get(issueId, eventType, issueId) as { created_at: string } | undefined;
  return row?.created_at ?? null;
}

export interface PipelineEventFilter {
  issueId?: string;
  identifier?: string;
  eventType?: string;
  since?: string;
}

export function getPipelineEvents(filters?: PipelineEventFilter): Array<{
  id: number;
  issue_id: string;
  identifier: string;
  event_type: string;
  detail: string | null;
  created_at: string;
}> {
  const conditions: string[] = [];
  const params: unknown[] = [];

  if (filters?.issueId) {
    conditions.push('issue_id = ?');
    params.push(filters.issueId);
  }
  if (filters?.identifier) {
    conditions.push('identifier = ?');
    params.push(filters.identifier);
  }
  if (filters?.eventType) {
    conditions.push('event_type = ?');
    params.push(filters.eventType);
  }
  if (filters?.since) {
    conditions.push('created_at >= ?');
    params.push(filters.since);
  }

  const where =
    conditions.length > 0 ? `WHERE ${conditions.join(' AND ')}` : '';
  return db
    .prepare(
      `SELECT id, issue_id, identifier, event_type, detail, created_at FROM linear_pipeline_events ${where} ORDER BY id ASC`,
    )
    .all(...params) as Array<{
    id: number;
    issue_id: string;
    identifier: string;
    event_type: string;
    detail: string | null;
    created_at: string;
  }>;
}

/**
 * Returns the ISO timestamp at which the given issue entered `stage`.
 *
 * - 'Agent Working'    → created_at of the first `agent_started` event
 * - 'In Review'        → created_at of the first `pr_created` or `agent_completed` event
 * - 'Ready for Agent'  → created_at of the last `agent_failed` event (retry anchor),
 *                        or null if there are none (caller should fall back to Linear API)
 *
 * Returns null when no anchor event is found.
 */
export function getStageEntryTime(
  issueId: string,
  stage: string,
): string | null {
  if (stage === 'Agent Working') {
    const row = db
      .prepare(
        `SELECT created_at FROM linear_pipeline_events
         WHERE issue_id = ? AND event_type = 'agent_started'
         ORDER BY id ASC LIMIT 1`,
      )
      .get(issueId) as { created_at: string } | undefined;
    return row?.created_at ?? null;
  }

  if (stage === 'In Review') {
    const row = db
      .prepare(
        `SELECT created_at FROM linear_pipeline_events
         WHERE issue_id = ? AND event_type IN ('pr_created', 'agent_completed')
         ORDER BY id ASC LIMIT 1`,
      )
      .get(issueId) as { created_at: string } | undefined;
    return row?.created_at ?? null;
  }

  if (stage === 'Ready for Agent') {
    const row = db
      .prepare(
        `SELECT created_at FROM linear_pipeline_events
         WHERE issue_id = ? AND event_type = 'agent_failed'
         ORDER BY id DESC LIMIT 1`,
      )
      .get(issueId) as { created_at: string } | undefined;
    return row?.created_at ?? null;
  }

  return null;
}

/**
 * Computes per-stage median durations from completed first-attempt runs in the local DB.
 *
 * Only "clean" (non-retried) runs are included: an issue with an `agent_failed` event
 * between its first `agent_started` and the subsequent `pr_created`/`agent_completed`
 * is excluded from the Agent Working sample.
 *
 * Stages computed:
 *   - 'Agent Working' : first agent_started → first pr_created/agent_completed (no agent_failed in between)
 *   - 'In Review'     : first pr_created/agent_completed → gate_ship
 *
 * Returns a Map keyed by stage name; stages with zero complete runs are omitted.
 */
export function computeStageMedians(): Map<
  string,
  { medianMs: number; sampleSize: number }
> {
  const result = new Map<string, { medianMs: number; sampleSize: number }>();

  type EventRow = {
    id: number;
    issue_id: string;
    event_type: string;
    created_at: string;
  };

  const events = db
    .prepare(
      `SELECT id, issue_id, event_type, created_at
       FROM linear_pipeline_events
       WHERE event_type IN ('agent_started','pr_created','agent_completed','agent_failed','gate_ship')
       ORDER BY id ASC`,
    )
    .all() as EventRow[];

  // Group events by issue
  const byIssue = new Map<string, EventRow[]>();
  for (const e of events) {
    if (!byIssue.has(e.issue_id)) byIssue.set(e.issue_id, []);
    byIssue.get(e.issue_id)!.push(e);
  }

  const agentWorkingDurations: number[] = [];
  const inReviewDurations: number[] = [];

  for (const issueEvents of byIssue.values()) {
    // First agent_started for this issue
    const firstStart = issueEvents.find(
      (e) => e.event_type === 'agent_started',
    );
    if (!firstStart) continue;

    // First pr_created or agent_completed after first agent_started
    const firstEnd = issueEvents.find(
      (e) =>
        (e.event_type === 'pr_created' || e.event_type === 'agent_completed') &&
        e.id > firstStart.id,
    );
    if (!firstEnd) continue;

    // Exclude retried issues: any agent_failed between firstStart and firstEnd
    const hadRetry = issueEvents.some(
      (e) =>
        e.event_type === 'agent_failed' &&
        e.id > firstStart.id &&
        e.id < firstEnd.id,
    );
    if (hadRetry) continue;

    const agentMs =
      new Date(firstEnd.created_at).getTime() -
      new Date(firstStart.created_at).getTime();
    if (agentMs > 0) agentWorkingDurations.push(agentMs);

    // In Review: firstEnd → gate_ship
    const shipEvent = issueEvents.find(
      (e) => e.event_type === 'gate_ship' && e.id > firstEnd.id,
    );
    if (shipEvent) {
      const reviewMs =
        new Date(shipEvent.created_at).getTime() -
        new Date(firstEnd.created_at).getTime();
      if (reviewMs > 0) inReviewDurations.push(reviewMs);
    }
  }

  const median = (durations: number[]): number => {
    durations.sort((a, b) => a - b);
    const mid = Math.floor(durations.length / 2);
    return durations.length % 2 !== 0
      ? durations[mid]
      : Math.floor((durations[mid - 1] + durations[mid]) / 2);
  };

  if (agentWorkingDurations.length > 0) {
    result.set('Agent Working', {
      medianMs: median(agentWorkingDurations),
      sampleSize: agentWorkingDurations.length,
    });
  }

  if (inReviewDurations.length > 0) {
    result.set('In Review', {
      medianMs: median(inReviewDurations),
      sampleSize: inReviewDurations.length,
    });
  }

  return result;
}

export function upsertPipelineComment(
  issueId: string,
  commentId: string,
): void {
  db.prepare(
    `INSERT INTO linear_pipeline_comments (issue_id, comment_id, updated_at)
     VALUES (?, ?, ?)
     ON CONFLICT(issue_id) DO UPDATE SET
       comment_id = excluded.comment_id,
       updated_at = excluded.updated_at`,
  ).run(issueId, commentId, new Date().toISOString());
}

export function getPipelineCommentId(issueId: string): string | undefined {
  const row = db
    .prepare(
      `SELECT comment_id FROM linear_pipeline_comments WHERE issue_id = ?`,
    )
    .get(issueId) as { comment_id: string } | undefined;
  return row?.comment_id;
}

// --- Issue cache ---

export interface IssueCacheRow {
  issue_id: string;
  identifier: string;
  title: string;
  state_name: string;
  team_id: string;
  priority: number;
  created_at: string;
  updated_at: string;
  cached_at: string;
}

interface IssueCacheInput {
  issue_id: string;
  identifier: string;
  title: string;
  state_name: string;
  team_id: string;
  priority: number;
  created_at: string;
  updated_at: string;
}

function doUpsertIssueCache(issue: IssueCacheInput): void {
  const now = new Date().toISOString();
  db.prepare(
    `INSERT INTO linear_issue_cache
       (issue_id, identifier, title, state_name, team_id, priority, created_at, updated_at, cached_at, deleted_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
     ON CONFLICT(issue_id) DO UPDATE SET
       identifier = excluded.identifier,
       title      = excluded.title,
       state_name = excluded.state_name,
       team_id    = excluded.team_id,
       priority   = excluded.priority,
       created_at = excluded.created_at,
       updated_at = excluded.updated_at,
       cached_at  = excluded.cached_at,
       deleted_at = NULL`,
  ).run(
    issue.issue_id,
    issue.identifier,
    issue.title,
    issue.state_name,
    issue.team_id,
    issue.priority,
    issue.created_at,
    issue.updated_at,
    now,
  );
}

export function upsertIssueCache(issue: IssueCacheInput): void {
  try {
    doUpsertIssueCache(issue);
  } catch (err) {
    logger.warn({ err, issueId: issue.issue_id }, 'issue-cache: upsert failed');
  }
}

export function softDeleteIssueCache(issueId: string): void {
  try {
    db.prepare(
      `UPDATE linear_issue_cache SET deleted_at = ? WHERE issue_id = ?`,
    ).run(new Date().toISOString(), issueId);
  } catch (err) {
    logger.warn({ err, issueId }, 'issue-cache: soft-delete failed');
  }
}

export function getIssueCacheCount(): number {
  const row = db
    .prepare(
      `SELECT COUNT(*) as cnt FROM linear_issue_cache WHERE deleted_at IS NULL`,
    )
    .get() as { cnt: number };
  return row.cnt;
}

export function getMaxCachedAt(): string | null {
  const row = db
    .prepare(
      `SELECT MAX(cached_at) as max_cached FROM linear_issue_cache WHERE deleted_at IS NULL`,
    )
    .get() as { max_cached: string | null };
  return row?.max_cached ?? null;
}

export function getIssuesFromCache(stateNames: string[]): IssueCacheRow[] {
  if (stateNames.length === 0) return [];
  const placeholders = stateNames.map(() => '?').join(', ');
  return db
    .prepare(
      `SELECT issue_id, identifier, title, state_name, team_id, priority,
              created_at, updated_at, cached_at
       FROM linear_issue_cache
       WHERE state_name IN (${placeholders}) AND deleted_at IS NULL
       ORDER BY state_name, updated_at DESC`,
    )
    .all(...stateNames) as IssueCacheRow[];
}

export function reconcileIssueCache(
  liveIssueIds: Set<string>,
  upserts: IssueCacheInput[],
): void {
  const run = db.transaction(() => {
    for (const issue of upserts) {
      doUpsertIssueCache(issue);
    }
    if (liveIssueIds.size > 0) {
      const now = new Date().toISOString();
      db.exec(
        'CREATE TEMP TABLE IF NOT EXISTS _reconcile_live (id TEXT PRIMARY KEY)',
      );
      db.exec('DELETE FROM _reconcile_live');
      const ins = db.prepare(
        'INSERT OR IGNORE INTO _reconcile_live (id) VALUES (?)',
      );
      for (const id of liveIssueIds) {
        ins.run(id);
      }
      db.prepare(
        `UPDATE linear_issue_cache SET deleted_at = ?
         WHERE issue_id NOT IN (SELECT id FROM _reconcile_live)
           AND deleted_at IS NULL`,
      ).run(now);
      db.exec('DROP TABLE IF EXISTS _reconcile_live');
    }
  });
  run();
}

// --- JSON migration ---

function migrateJsonState(): void {
  const migrateFile = (filename: string) => {
    const filePath = path.join(DATA_DIR, filename);
    if (!fs.existsSync(filePath)) return null;
    try {
      const data = JSON.parse(fs.readFileSync(filePath, 'utf-8'));
      fs.renameSync(filePath, `${filePath}.migrated`);
      return data;
    } catch {
      return null;
    }
  };

  // Migrate router_state.json
  const routerState = migrateFile('router_state.json') as {
    last_timestamp?: string;
    last_agent_timestamp?: Record<string, string>;
  } | null;
  if (routerState) {
    if (routerState.last_timestamp) {
      setRouterState('last_timestamp', routerState.last_timestamp);
    }
    if (routerState.last_agent_timestamp) {
      setRouterState(
        'last_agent_timestamp',
        JSON.stringify(routerState.last_agent_timestamp),
      );
    }
  }

  // Migrate sessions.json
  const sessions = migrateFile('sessions.json') as Record<
    string,
    string
  > | null;
  if (sessions) {
    for (const [folder, sessionId] of Object.entries(sessions)) {
      setSession(folder, sessionId);
    }
  }

  // Migrate registered_groups.json
  const groups = migrateFile('registered_groups.json') as Record<
    string,
    RegisteredGroup
  > | null;
  if (groups) {
    for (const [jid, group] of Object.entries(groups)) {
      try {
        setRegisteredGroup(jid, group);
      } catch (err) {
        logger.warn(
          { jid, folder: group.folder, err },
          'Skipping migrated registered group with invalid folder',
        );
      }
    }
  }
}
