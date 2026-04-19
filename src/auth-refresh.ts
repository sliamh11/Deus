// Scheduling is macOS-only via launchd. Linux: systemd timer. Windows: Task Scheduler. Future work.
/**
 * deus auth refresh — Proactive Claude OAuth token refresh.
 *
 * Why this exists: idle container agents (no proxy traffic for 8h) never
 * trigger the in-process refresh in credential-proxy.ts, so the token
 * silently expires and the next chat message hits a /login prompt.
 *
 * This CLI is scheduled (macOS launchd, every 30 min) to refresh the token
 * pre-emptively when `expiresAt - now < 45 min`. Single-flight via a file
 * lock at ~/.claude/.credentials.refresh.lock to avoid racing with the
 * in-process refresh or another CLI invocation.
 *
 * CLI: `node dist/auth-refresh.js [--dry-run]`
 *
 * Exit codes:
 *   0 — no-op (not yet expiring, lock held, or refresh succeeded)
 *   1 — refresh failed (message dropped to control-group IPC + macOS notif)
 */
import { execFileSync } from 'child_process';
import fs from 'fs';
import path from 'path';

import {
  OAuthCredentials,
  readCredentialsFile,
  refreshOAuthToken,
  writeCredentialsFile,
} from './auth-providers/anthropic.js';
import { DATA_DIR, STORE_DIR } from './config.js';
import { homeDir, IS_MACOS } from './platform.js';

// ── Constants ──────────────────────────────────────────────────────────────

/**
 * Gate for proactive refresh. Longer than the in-process proxy window
 * (30 min) so the CLI always wakes first on a scheduled tick.
 */
const REFRESH_GATE_MS = 45 * 60 * 1000;

/**
 * A lock file older than this is treated as stale (process likely crashed
 * before releasing). Must be larger than the longest reasonable
 * fetch-to-rename round-trip but small enough that a stuck lock unblocks
 * the next scheduled run.
 */
const STALE_LOCK_MS = 90 * 1000;

function getLockPath(): string {
  return path.join(homeDir, '.claude', '.credentials.refresh.lock');
}

// ── Logging (structured, stdout-only) ──────────────────────────────────────

function logLine(obj: Record<string, unknown>): void {
  process.stdout.write(JSON.stringify({ ts: Date.now(), ...obj }) + '\n');
}

// ── Control-group discovery ────────────────────────────────────────────────

/**
 * Find the control group's folder + jid without initializing the full db
 * module. We open the SQLite store read-only and run a single query.
 * Returns undefined if the DB, table, or row doesn't exist (fresh install
 * or pre-migration state).
 */
async function findControlGroup(): Promise<
  { folder: string; jid: string } | undefined
> {
  const dbPath = path.join(STORE_DIR, 'messages.db');
  if (!fs.existsSync(dbPath)) return undefined;
  try {
    // Lazy-load the native addon only when the failure path is hit,
    // so fresh installs without better-sqlite3 built don't crash.
    const { default: Database } = await import('better-sqlite3');
    const db = new Database(dbPath, { readonly: true, fileMustExist: true });
    try {
      const row = db
        .prepare(
          `SELECT folder, jid FROM registered_groups WHERE is_main = 1 LIMIT 1`,
        )
        .get() as { folder?: string; jid?: string } | undefined;
      if (!row?.folder || !row?.jid) return undefined;
      return { folder: row.folder, jid: row.jid };
    } finally {
      db.close();
    }
  } catch {
    return undefined;
  }
}

// ── Failure notification ───────────────────────────────────────────────────

/**
 * Drop an IPC message file for the control group so the agent can surface
 * the failure. Falls back to a macOS desktop notification if no control
 * group is registered yet (fresh install).
 */
async function notifyFailure(reason: string): Promise<void> {
  const ts = Date.now();
  const controlGroup = await findControlGroup();
  if (controlGroup) {
    const messagesDir = path.join(
      DATA_DIR,
      'ipc',
      controlGroup.folder,
      'messages',
    );
    try {
      fs.mkdirSync(messagesDir, { recursive: true });
      // Source folder is the control group, so the IPC watcher's
      // isControlGroup check (src/ipc.ts:72-92) permits sending to any
      // chatJid. We target the control group's own jid so the failure
      // lands in the same chat the user uses to talk to Deus.
      const payload = {
        type: 'message',
        chatJid: controlGroup.jid,
        text: `Deus OAuth refresh failed: ${reason}. Run \`deus auth refresh\` manually, or \`/login\` from the CLI if the token is already invalid.`,
        source: 'auth-refresh',
        reason,
      };
      const fileName = `oauth-refresh-fail-${ts}.json`;
      fs.writeFileSync(
        path.join(messagesDir, fileName),
        JSON.stringify(payload, null, 2),
      );
      logLine({
        action: 'ipc-drop',
        folder: controlGroup.folder,
        file: fileName,
      });
      return;
    } catch (err) {
      logLine({ action: 'ipc-drop-failed', err: String(err) });
      // fall through to osascript
    }
  }

  if (IS_MACOS) {
    try {
      // Mirror scripts/log_review.py:409-414 notification style.
      execFileSync(
        'osascript',
        [
          '-e',
          `display notification "OAuth refresh failed: ${reason}" with title "Deus" subtitle "Run 'deus auth refresh' or '/login'"`,
        ],
        { timeout: 5000, stdio: 'ignore' },
      );
      logLine({ action: 'osascript-notified' });
    } catch (err) {
      logLine({ action: 'osascript-failed', err: String(err) });
    }
  }
}

// ── Lock handling ──────────────────────────────────────────────────────────

type LockHandle = { fd: number; path: string };

/**
 * Acquire the refresh lock. Returns a handle on success, or null if another
 * refresh is in flight and the lock is fresh (< STALE_LOCK_MS old).
 *
 * If the existing lock is older than STALE_LOCK_MS, we assume the previous
 * runner died and take over.
 */
function acquireLock(): LockHandle | null {
  const lockPath = getLockPath();
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });

  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const fd = fs.openSync(lockPath, 'wx');
      fs.writeSync(fd, String(process.pid));
      return { fd, path: lockPath };
    } catch (err: unknown) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== 'EEXIST') throw err;
    }

    let mtime: number;
    try {
      mtime = fs.statSync(lockPath).mtimeMs;
    } catch {
      // Lock vanished between failed open and stat — retry.
      continue;
    }
    const age = Date.now() - mtime;
    if (age < STALE_LOCK_MS) {
      return null; // fresh lock held by someone else
    }
    // Stale — delete and retry once.
    try {
      fs.unlinkSync(lockPath);
    } catch {
      // Another process may have cleaned up; loop and try openSync again.
    }
  }
  return null;
}

function releaseLock(lock: LockHandle): void {
  try {
    fs.closeSync(lock.fd);
  } catch {
    // already closed
  }
  try {
    fs.unlinkSync(lock.path);
  } catch {
    // already removed
  }
}

// ── Main ───────────────────────────────────────────────────────────────────

export interface RefreshResult {
  action: 'noop' | 'skip' | 'refreshed' | 'dry-run' | 'failed';
  reason?: string;
  expiresIn?: number;
}

/**
 * Run one refresh cycle. Exported for testing; the CLI wrapper below handles
 * process exit codes and top-level error reporting.
 */
export async function runRefresh(opts: {
  dryRun: boolean;
  now?: () => number;
}): Promise<RefreshResult> {
  const now = opts.now ?? Date.now;

  const creds = readCredentialsFile();
  if (!creds) {
    const result: RefreshResult = {
      action: 'noop',
      reason: 'no-credentials',
    };
    logLine({ action: result.action, reason: result.reason });
    return result;
  }

  const untilExpiry = creds.expiresAt - now();
  if (untilExpiry > REFRESH_GATE_MS) {
    const result: RefreshResult = {
      action: 'noop',
      reason: 'not-expiring',
      expiresIn: Math.floor(untilExpiry / 1000),
    };
    logLine({
      action: result.action,
      reason: result.reason,
      expiresIn: result.expiresIn,
    });
    return result;
  }

  if (!creds.refreshToken) {
    const result: RefreshResult = {
      action: 'failed',
      reason: 'no-refresh-token',
    };
    logLine({ action: result.action, reason: result.reason });
    await notifyFailure(result.reason!);
    return result;
  }

  if (opts.dryRun) {
    const result: RefreshResult = {
      action: 'dry-run',
      expiresIn: Math.floor(untilExpiry / 1000),
    };
    logLine({
      action: result.action,
      expiresIn: result.expiresIn,
      note: 'would-refresh',
    });
    return result;
  }

  const lock = acquireLock();
  if (!lock) {
    const result: RefreshResult = {
      action: 'skip',
      reason: 'another-refresh-in-flight',
    };
    logLine({ action: result.action, reason: result.reason });
    return result;
  }

  try {
    // Re-read credentials after acquiring the lock: the in-process proxy
    // or a prior CLI run may have refreshed the token between the initial
    // read and lock acquisition.
    const fresh = readCredentialsFile() ?? creds;
    if (fresh.expiresAt - now() > REFRESH_GATE_MS) {
      const result: RefreshResult = {
        action: 'noop',
        reason: 'refreshed-by-other',
        expiresIn: Math.floor((fresh.expiresAt - now()) / 1000),
      };
      logLine({
        action: result.action,
        reason: result.reason,
        expiresIn: result.expiresIn,
      });
      return result;
    }

    const refreshToken = fresh.refreshToken ?? creds.refreshToken;
    if (!refreshToken) {
      const result: RefreshResult = {
        action: 'failed',
        reason: 'no-refresh-token',
      };
      logLine({ action: result.action, reason: result.reason });
      await notifyFailure(result.reason!);
      return result;
    }

    const newCreds = await refreshOAuthToken(refreshToken);
    if (!newCreds) {
      const result: RefreshResult = {
        action: 'failed',
        reason: 'refresh-endpoint-rejected',
      };
      logLine({ action: result.action, reason: result.reason });
      await notifyFailure(result.reason!);
      return result;
    }

    writeCredentialsFile(newCreds satisfies OAuthCredentials);

    const expiresIn = Math.floor((newCreds.expiresAt - now()) / 1000);
    // Structured log line per requirement: { ts, action:"refresh", expiresIn }
    logLine({ action: 'refresh', expiresIn });
    return { action: 'refreshed', expiresIn };
  } finally {
    releaseLock(lock);
  }
}

// ── Entrypoint ─────────────────────────────────────────────────────────────

/**
 * True when this file is executed directly (vs imported by a test).
 * On Node ESM-over-tsc, import.meta.url is the compiled dist path; argv[1]
 * is the same path minus the file:// prefix.
 */
function isMain(): boolean {
  const url = import.meta.url;
  if (!url.startsWith('file:')) return false;
  const filePath = new URL(url).pathname;
  const arg = process.argv[1];
  if (!arg) return false;
  return filePath === arg || filePath === fs.realpathSync.native(arg);
}

async function main(): Promise<void> {
  const dryRun = process.argv.includes('--dry-run');
  try {
    const result = await runRefresh({ dryRun });
    process.exit(result.action === 'failed' ? 1 : 0);
  } catch (err) {
    logLine({ action: 'crashed', err: String(err) });
    try {
      await notifyFailure('crashed');
    } catch {
      // notification itself can't fail the process
    }
    process.exit(1);
  }
}

if (isMain()) {
  // Fire and forget — main() calls process.exit itself.
  void main();
}
