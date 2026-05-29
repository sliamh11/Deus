/**
 * Google Calendar sync daemon — Phase 3 of printing-press-adoption.md.
 *
 * Polls Google Calendar API on a configurable interval (default 5 min) and
 * writes events into ~/.deus/cache-gcal.db (SQLite + FTS5).
 *
 * Design: incremental sync via syncToken, journal_mode=DELETE (not WAL) to
 * avoid .db-wal/.db-shm with read-only container mounts, soft-delete via
 * orphaned_at per no-db-deletion.md, per-service DB per evolution-db-split.md.
 */

import fs from 'fs';
import path from 'path';
import Database from 'better-sqlite3';
import { google, calendar_v3 } from 'googleapis';
import { OAuth2Client } from 'google-auth-library';
import { logger } from '../logger.js';
import { HOME_DIR } from '../config.js';
import { RetryableError, UserError, FatalError } from '../errors/index.js';
import { fireAndForget } from '../async/index.js';

const DEFAULT_POLL_MS = 5 * 60 * 1000;
const DEFAULT_CALENDAR_ID = 'primary';
const PROJECT_ROOT = process.env.DEUS_PROJECT_ROOT || process.cwd();
const DEFAULT_CREDS = path.join(
  PROJECT_ROOT,
  'integrations',
  'gcal',
  'credentials.json',
);
const DEFAULT_TOKENS = path.join(
  PROJECT_ROOT,
  'integrations',
  'gcal',
  'tokens.json',
);

export const CACHE_GCAL_DB_PATH =
  process.env.DEUS_CACHE_GCAL_DB ??
  path.join(HOME_DIR, '.deus', 'cache-gcal.db');

export interface GcalSyncOptions {
  pollIntervalMs?: number;
  calendarId?: string;
  credentialsPath?: string;
  tokensPath?: string;
  dbPath?: string;
}

/* ── Schema ─────────────────────────────────────────────────────────────── */

const SCHEMA = `
PRAGMA journal_mode = DELETE;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY, summary TEXT,
  start_time TEXT NOT NULL, end_time TEXT NOT NULL,
  location TEXT, description TEXT, html_link TEXT, organizer TEXT,
  status TEXT DEFAULT 'confirmed',
  updated_at TEXT NOT NULL, synced_at TEXT NOT NULL, orphaned_at TEXT
);
CREATE TABLE IF NOT EXISTS sync_state (
  calendar_id TEXT PRIMARY KEY, sync_token TEXT,
  last_synced_at TEXT NOT NULL, total_events INTEGER DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  summary, description, location, organizer,
  content=events, content_rowid=rowid, tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS events_ai AFTER INSERT ON events BEGIN
  INSERT INTO events_fts(rowid, summary, description, location, organizer)
  VALUES (new.rowid, new.summary, new.description, new.location, new.organizer);
END;
CREATE TRIGGER IF NOT EXISTS events_au AFTER UPDATE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, summary, description, location, organizer)
  VALUES ('delete', old.rowid, old.summary, old.description, old.location, old.organizer);
  INSERT INTO events_fts(rowid, summary, description, location, organizer)
  VALUES (new.rowid, new.summary, new.description, new.location, new.organizer);
END;
CREATE TRIGGER IF NOT EXISTS events_ad AFTER DELETE ON events BEGIN
  INSERT INTO events_fts(events_fts, rowid, summary, description, location, organizer)
  VALUES ('delete', old.rowid, old.summary, old.description, old.location, old.organizer);
END;
`;

export function openCacheDb(dbPath: string): Database.Database {
  try {
    fs.mkdirSync(path.dirname(dbPath), { recursive: true });
    const db = new Database(dbPath);
    db.exec(SCHEMA);
    return db;
  } catch (err) {
    throw new FatalError('Failed to open gcal cache DB', {
      cause: err,
      context: { dbPath },
    });
  }
}

/* ── Auth ────────────────────────────────────────────────────────────────── */

function buildAuth(credentialsPath: string, tokensPath: string): OAuth2Client {
  if (!fs.existsSync(credentialsPath) || !fs.existsSync(tokensPath)) {
    throw new UserError('Google Calendar credentials not found', {
      context: { credentialsPath, tokensPath },
    });
  }
  const creds = JSON.parse(fs.readFileSync(credentialsPath, 'utf8'));
  const { client_id, client_secret, redirect_uris } =
    creds.installed || creds.web;
  const auth = new google.auth.OAuth2(
    client_id,
    client_secret,
    redirect_uris[0],
  );
  const tokens = JSON.parse(fs.readFileSync(tokensPath, 'utf8'));
  auth.setCredentials(tokens);
  // expiry_date is optional; when present and in the past, Google's OAuth2Client
  // will attempt automatic refresh on the first API call
  if (tokens.expiry_date && tokens.expiry_date < Date.now()) {
    logger.warn(
      { expiryDate: new Date(tokens.expiry_date).toISOString() },
      'gcal-sync: token appears expired at startup, refresh will be attempted on first API call',
    );
  }
  auth.on('tokens', (newTokens) => {
    try {
      fs.writeFileSync(
        tokensPath,
        JSON.stringify({ ...tokens, ...newTokens }, null, 2),
      );
    } catch (err) {
      // tokensPath may be read-only in some environments — token refresh still works in-process
      logger.debug(
        { err },
        'gcal-sync: could not persist refreshed tokens (non-fatal)',
      );
    }
  });
  return auth;
}

/* ── DB helpers ─────────────────────────────────────────────────────────── */

function mapEvent(
  e: calendar_v3.Schema$Event,
  syncedAt: string,
): Record<string, string | null> {
  return {
    id: e.id ?? '',
    summary: e.summary ?? null,
    start_time: e.start?.dateTime ?? e.start?.date ?? '',
    end_time: e.end?.dateTime ?? e.end?.date ?? '',
    location: e.location ?? null,
    description: e.description ?? null,
    html_link: e.htmlLink ?? null,
    organizer: e.organizer?.email ?? e.organizer?.displayName ?? null,
    status: e.status ?? 'confirmed',
    updated_at: e.updated ?? new Date().toISOString(),
    synced_at: syncedAt,
  };
}

const UPSERT_EVENT = `
  INSERT INTO events (id,summary,start_time,end_time,location,description,
                      html_link,organizer,status,updated_at,synced_at,orphaned_at)
  VALUES (@id,@summary,@start_time,@end_time,@location,@description,
          @html_link,@organizer,@status,@updated_at,@synced_at,NULL)
  ON CONFLICT(id) DO UPDATE SET
    summary=excluded.summary, start_time=excluded.start_time,
    end_time=excluded.end_time, location=excluded.location,
    description=excluded.description, html_link=excluded.html_link,
    organizer=excluded.organizer, status=excluded.status,
    updated_at=excluded.updated_at, synced_at=excluded.synced_at,
    orphaned_at=NULL`;

const SOFT_DELETE = `UPDATE events SET orphaned_at=@orphaned_at WHERE id=@id AND orphaned_at IS NULL`;
const GET_STATE = `SELECT sync_token, total_events FROM sync_state WHERE calendar_id=?`;
const UPSERT_STATE = `
  INSERT INTO sync_state (calendar_id,sync_token,last_synced_at,total_events)
  VALUES (@calendar_id,@sync_token,@last_synced_at,@total_events)
  ON CONFLICT(calendar_id) DO UPDATE SET
    sync_token=excluded.sync_token, last_synced_at=excluded.last_synced_at,
    total_events=excluded.total_events`;

/* ── Auth failure tracking (singleton daemon — one instance per process) ── */

let consecutiveAuthFailures = 0;
const AUTH_FAILURE_THRESHOLD = 3;

/** @internal exposed for testing only */
export function _resetConsecutiveAuthFailuresForTest(): void {
  consecutiveAuthFailures = 0;
}

/* ── Core sync ───────────────────────────────────────────────────────────── */

async function runSync(
  db: Database.Database,
  cal: calendar_v3.Calendar,
  calendarId: string,
): Promise<void> {
  const syncedAt = new Date().toISOString();
  type SyncRow = { sync_token: string | null; total_events: number };
  const state = db.prepare(GET_STATE).get(calendarId) as SyncRow | undefined;
  let syncToken = state?.sync_token ?? null;

  const upsert = db.prepare(UPSERT_EVENT);
  const softDelete = db.prepare(SOFT_DELETE);
  const upsertState = db.prepare(UPSERT_STATE);

  let pageToken: string | undefined;
  let upserted = 0;
  let deleted = 0;

  do {
    let res: calendar_v3.Schema$Events;
    try {
      const params: calendar_v3.Params$Resource$Events$List = {
        calendarId,
        singleEvents: true,
        maxResults: 250,
        ...(syncToken ? { syncToken } : { timeMin: new Date().toISOString() }),
        ...(pageToken ? { pageToken } : {}),
      };
      res = (await cal.events.list(params)).data;
    } catch (err: unknown) {
      const status =
        typeof err === 'object' && err !== null
          ? 'status' in err &&
            typeof (err as { status: unknown }).status === 'number'
            ? (err as { status: number }).status
            : 'code' in err &&
                typeof (err as { code: unknown }).code === 'number'
              ? (err as { code: number }).code
              : undefined
          : undefined;
      if (status === 410) {
        // syncToken expired — reset for full sync next run (expected lifecycle)
        logger.info({ calendarId }, 'gcal-sync: syncToken expired, resetting');
        upsertState.run({
          calendar_id: calendarId,
          sync_token: null,
          last_synced_at: syncedAt,
          total_events: state?.total_events ?? 0,
        });
        return;
      }
      if (status === 401) {
        consecutiveAuthFailures++;
        logger.error(
          {
            event: 'gcal_auth_failure',
            consecutiveFailures: consecutiveAuthFailures,
            status,
          },
          consecutiveAuthFailures >= AUTH_FAILURE_THRESHOLD
            ? `gcal-sync: auth failure ${consecutiveAuthFailures}/${AUTH_FAILURE_THRESHOLD} — manual token refresh required (node scripts/setup-gcal-auth.mjs)`
            : `gcal-sync: auth failure ${consecutiveAuthFailures}/${AUTH_FAILURE_THRESHOLD} — will retry next cycle`,
        );
        return;
      }
      if (status === 429 || (status && status >= 500)) {
        throw new RetryableError('Google Calendar API error', {
          cause: err,
          context: { status },
        });
      }
      throw err;
    }

    db.transaction(() => {
      for (const event of res.items ?? []) {
        if (!event.id) continue;
        if (event.status === 'cancelled') {
          softDelete.run({ id: event.id, orphaned_at: syncedAt });
          deleted++;
        } else {
          upsert.run(mapEvent(event, syncedAt));
          upserted++;
        }
      }
    })();

    syncToken = res.nextSyncToken ?? syncToken;
    pageToken = res.nextPageToken ?? undefined;
  } while (pageToken);

  upsertState.run({
    calendar_id: calendarId,
    sync_token: syncToken,
    last_synced_at: syncedAt,
    total_events: Math.max(0, (state?.total_events ?? 0) + upserted - deleted),
  });
  consecutiveAuthFailures = 0;
  logger.info(
    { calendarId, upserted, softDeleted: deleted },
    'gcal-sync: sync complete',
  );
}

/* ── Public API ─────────────────────────────────────────────────────────── */

let _timer: ReturnType<typeof setInterval> | null = null;
let _db: Database.Database | null = null;

export function startGcalSync(options: GcalSyncOptions = {}): void {
  if (_timer) {
    logger.warn('gcal-sync: already running');
    return;
  }
  const {
    pollIntervalMs = DEFAULT_POLL_MS,
    calendarId = DEFAULT_CALENDAR_ID,
    credentialsPath = DEFAULT_CREDS,
    tokensPath = DEFAULT_TOKENS,
    dbPath = CACHE_GCAL_DB_PATH,
  } = options;

  let auth: OAuth2Client;
  try {
    auth = buildAuth(credentialsPath, tokensPath);
  } catch (err) {
    if (err instanceof UserError) {
      logger.warn({ err }, 'gcal-sync: credentials missing — daemon dormant');
      return;
    }
    throw err;
  }

  _db = openCacheDb(dbPath);
  const cal = google.calendar({ version: 'v3', auth });

  const tick = () => {
    fireAndForget(() => runSync(_db!, cal, calendarId), {
      name: 'gcal.sync',
      onError: (err) => {
        if (err instanceof RetryableError) {
          logger.warn({ err }, 'gcal-sync: transient error, will retry');
        } else {
          logger.error({ err }, 'gcal-sync: unexpected error');
        }
      },
    });
  };

  tick(); // Run immediately on start
  _timer = setInterval(tick, pollIntervalMs);
  _timer.unref();
  logger.info(
    { calendarId, pollIntervalMs, dbPath },
    'gcal-sync: daemon started',
  );
}

export function stopGcalSync(): void {
  if (_timer) {
    clearInterval(_timer);
    _timer = null;
  }
  if (_db) {
    _db.close();
    _db = null;
  }
  consecutiveAuthFailures = 0;
  logger.info('gcal-sync: daemon stopped');
}
