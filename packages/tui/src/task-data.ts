import { execFileSync } from 'child_process';
import { existsSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// TODO(liam, 2026-05): cross-package import blocked by tsconfig rootDir — migrate when Phase 2 adds references/composite
const IS_WINDOWS = process.platform === 'win32';

const DEPTH_TO_REPO_ROOT = 3;
const DB_RELATIVE_PATH = 'store/messages.db';
const TASK_QUERY = `SELECT id, prompt, status, schedule_type, schedule_value, group_folder, next_run, last_run, last_result FROM scheduled_tasks ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'paused' THEN 1 ELSE 2 END, next_run ASC`;

const VALID_STATUSES = new Set<TaskEntry['status']>([
  'active',
  'paused',
  'completed',
]);
const VALID_SCHEDULE_TYPES = new Set<TaskEntry['scheduleType']>([
  'cron',
  'interval',
  'once',
]);
const DEFAULT_STATUS: TaskEntry['status'] = 'completed';
const DEFAULT_SCHEDULE_TYPE: TaskEntry['scheduleType'] = 'once';

export interface TaskEntry {
  id: string;
  prompt: string;
  status: 'active' | 'paused' | 'completed';
  scheduleType: 'cron' | 'interval' | 'once';
  scheduleValue: string;
  groupFolder: string;
  nextRun: string | null;
  lastRun: string | null;
  lastResult: string | null;
}

interface RawTaskRow {
  id: string;
  prompt: string;
  status: string;
  schedule_type: string;
  schedule_value: string;
  group_folder: string;
  next_run: string | null;
  last_run: string | null;
  last_result: string | null;
}

function resolveDbPath(): string {
  const segments = [
    __dirname,
    ...Array(DEPTH_TO_REPO_ROOT).fill('..'),
    DB_RELATIVE_PATH,
  ];
  return resolve(...segments);
}

export function mapRow(row: RawTaskRow): TaskEntry {
  return {
    id: row.id,
    prompt: row.prompt,
    status: VALID_STATUSES.has(row.status as TaskEntry['status'])
      ? (row.status as TaskEntry['status'])
      : DEFAULT_STATUS,
    scheduleType: VALID_SCHEDULE_TYPES.has(
      row.schedule_type as TaskEntry['scheduleType'],
    )
      ? (row.schedule_type as TaskEntry['scheduleType'])
      : DEFAULT_SCHEDULE_TYPE,
    scheduleValue: row.schedule_value,
    groupFolder: row.group_folder,
    nextRun: row.next_run ?? null,
    lastRun: row.last_run ?? null,
    lastResult: row.last_result ?? null,
  };
}

export function loadTasks(dbPathOverride?: string): TaskEntry[] {
  if (IS_WINDOWS) return [];

  const dbPath = dbPathOverride ?? resolveDbPath();
  if (!existsSync(dbPath)) return [];

  try {
    const output = execFileSync('sqlite3', [dbPath, '-json', TASK_QUERY], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    const rows: RawTaskRow[] = JSON.parse(output);
    return rows.map(mapRow);
  } catch (e) {
    process.stderr.write(
      `[deus-tui] task-data: ${e instanceof Error ? e.message : e}\n`,
    );
    return [];
  }
}
