import { test, expect } from 'vitest';
import { execFileSync } from 'child_process';
import { existsSync, mkdirSync, rmSync } from 'fs';
import { resolve, dirname, join } from 'path';
import { fileURLToPath } from 'url';
import { tmpdir } from 'os';
import { loadTasks, mapRow } from '../task-data.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const DEPTH_TO_STORE = 4;
const REAL_DB_PATH = resolve(
  __dirname,
  ...Array(DEPTH_TO_STORE).fill('..'),
  'store',
  'messages.db',
);

const CREATE_TABLE_SQL = `
  CREATE TABLE scheduled_tasks (
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
    context_mode TEXT DEFAULT 'isolated',
    agent_backend TEXT
  );
`;

function createFixtureDb(dir: string, insertSql: string): string {
  mkdirSync(dir, { recursive: true });
  const dbPath = join(dir, 'messages.db');
  execFileSync('sqlite3', [dbPath], {
    input: CREATE_TABLE_SQL + insertSql,
    encoding: 'utf-8',
  });
  return dbPath;
}

test('loadTasks returns tasks from real DB if available', () => {
  if (!existsSync(REAL_DB_PATH)) {
    expect(loadTasks()).toEqual([]);
    return;
  }

  const tasks = loadTasks();
  expect(Array.isArray(tasks)).toBe(true);
  for (const task of tasks) {
    expect(task).toHaveProperty('id');
    expect(task).toHaveProperty('prompt');
    expect(task).toHaveProperty('status');
    expect(task).toHaveProperty('scheduleType');
    expect(task).toHaveProperty('scheduleValue');
    expect(task).toHaveProperty('groupFolder');
  }
});

test('loadTasks returns empty array for nonexistent DB path', () => {
  const tasks = loadTasks('/tmp/nonexistent-deus-test/messages.db');
  expect(tasks).toEqual([]);
});

test('loadTasks returns empty array for empty table', () => {
  const dir = join(tmpdir(), `deus-task-test-empty-${process.pid}`);
  try {
    const dbPath = createFixtureDb(dir, '');
    const tasks = loadTasks(dbPath);
    expect(tasks).toEqual([]);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('loadTasks loads and maps fixture rows correctly', () => {
  const dir = join(tmpdir(), `deus-task-test-map-${process.pid}`);
  try {
    const dbPath = createFixtureDb(
      dir,
      `
      INSERT INTO scheduled_tasks (id, group_folder, chat_jid, prompt, schedule_type, schedule_value, status, created_at)
      VALUES ('t1', 'telegram_main', 'j1', 'Check weather', 'cron', '0 8 * * *', 'active', '2026-01-01');
      INSERT INTO scheduled_tasks (id, group_folder, chat_jid, prompt, schedule_type, schedule_value, status, created_at)
      VALUES ('t2', 'whatsapp_main', 'j2', 'Send report', 'once', '2026-04-01T10:00:00', 'paused', '2026-01-01');
    `,
    );

    const tasks = loadTasks(dbPath);
    expect(tasks).toHaveLength(2);

    expect(tasks[0]!.id).toBe('t1');
    expect(tasks[0]!.status).toBe('active');
    expect(tasks[0]!.scheduleType).toBe('cron');
    expect(tasks[0]!.groupFolder).toBe('telegram_main');

    expect(tasks[1]!.id).toBe('t2');
    expect(tasks[1]!.status).toBe('paused');
    expect(tasks[1]!.scheduleType).toBe('once');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('loadTasks orders active before paused before completed', () => {
  const dir = join(tmpdir(), `deus-task-test-order-${process.pid}`);
  try {
    const dbPath = createFixtureDb(
      dir,
      `
      INSERT INTO scheduled_tasks (id, group_folder, chat_jid, prompt, schedule_type, schedule_value, status, created_at)
      VALUES ('t1', 'g1', 'j1', 'Completed', 'once', '2026-01-01', 'completed', '2026-01-01');
      INSERT INTO scheduled_tasks (id, group_folder, chat_jid, prompt, schedule_type, schedule_value, status, created_at)
      VALUES ('t2', 'g1', 'j1', 'Active', 'cron', '0 8 * * *', 'active', '2026-01-01');
      INSERT INTO scheduled_tasks (id, group_folder, chat_jid, prompt, schedule_type, schedule_value, status, created_at)
      VALUES ('t3', 'g1', 'j1', 'Paused', 'interval', '1h', 'paused', '2026-01-01');
    `,
    );

    const tasks = loadTasks(dbPath);
    expect(tasks).toHaveLength(3);
    expect(tasks[0]!.status).toBe('active');
    expect(tasks[1]!.status).toBe('paused');
    expect(tasks[2]!.status).toBe('completed');
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test('mapRow validates unknown status/scheduleType to defaults', () => {
  const row = {
    id: 't1',
    prompt: 'Test',
    status: 'cancelled',
    schedule_type: 'webhook',
    schedule_value: '*/5',
    group_folder: 'g1',
    next_run: null,
    last_run: null,
    last_result: null,
  };

  const entry = mapRow(row);
  expect(entry.status).toBe('completed');
  expect(entry.scheduleType).toBe('once');
});

test('mapRow preserves valid status/scheduleType', () => {
  const row = {
    id: 't2',
    prompt: 'Valid',
    status: 'active',
    schedule_type: 'cron',
    schedule_value: '0 8 * * *',
    group_folder: 'g2',
    next_run: '2026-05-06T08:00:00',
    last_run: '2026-05-05T08:00:00',
    last_result: 'OK',
  };

  const entry = mapRow(row);
  expect(entry.status).toBe('active');
  expect(entry.scheduleType).toBe('cron');
  expect(entry.nextRun).toBe('2026-05-06T08:00:00');
  expect(entry.lastRun).toBe('2026-05-05T08:00:00');
  expect(entry.lastResult).toBe('OK');
});
