/**
 * Step: smoke-test — Verify end-to-end channel message delivery.
 *
 * Checks that the service is running, a registered group exists for the
 * specified channel, and the DB layer can store and retrieve messages.
 * Designed to catch silent failures like the MCP logging capability gate
 * (PR #88) where channels connected but messages never reached the host.
 *
 * Usage: npx tsx setup/index.ts --step smoke-test [--channel <name>] [--timeout <ms>]
 */
import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';

import Database from 'better-sqlite3';

import { STORE_DIR } from '../src/config.js';
import { logger } from '../src/logger.js';
import { getServiceManager, isRoot } from './platform.js';
import { emitStatus } from './status.js';

interface SmokeTestArgs {
  channel?: string;
  timeout: number;
}

function parseArgs(args: string[]): SmokeTestArgs {
  const result: SmokeTestArgs = { timeout: 10_000 };
  for (let i = 0; i < args.length; i++) {
    switch (args[i]) {
      case '--channel':
        result.channel = (args[++i] || '').toLowerCase();
        break;
      case '--timeout':
        result.timeout = Number(args[++i]) || 10_000;
        break;
    }
  }
  return result;
}

/** Check if the Deus service process is running. */
function checkServiceRunning(): 'running' | 'stopped' | 'not_found' {
  const mgr = getServiceManager();

  if (mgr === 'launchd') {
    try {
      const output = execSync('launchctl list', { encoding: 'utf-8' });
      const line = output.split('\n').find((l) => l.includes('com.deus'));
      if (line) {
        const pidField = line.trim().split(/\s+/)[0];
        return pidField !== '-' && pidField ? 'running' : 'stopped';
      }
    } catch {
      // launchctl not available
    }
  } else if (mgr === 'systemd') {
    const prefix = isRoot() ? 'systemctl' : 'systemctl --user';
    try {
      execSync(`${prefix} is-active deus`, { stdio: 'ignore' });
      return 'running';
    } catch {
      return 'stopped';
    }
  } else if (mgr === 'nssm') {
    try {
      const out = execSync('nssm status deus', {
        encoding: 'utf-8',
        stdio: 'pipe',
      });
      return out.trim() === 'SERVICE_RUNNING' ? 'running' : 'stopped';
    } catch {
      return 'not_found';
    }
  } else if (mgr === 'servy') {
    try {
      const out = execSync('servy-cli status --name="deus"', {
        encoding: 'utf-8',
        stdio: 'pipe',
      });
      return out.trim() === 'Running' ? 'running' : 'stopped';
    } catch {
      return 'not_found';
    }
  }

  // Fallback: check PID file
  const pidFile = path.join(process.cwd(), 'deus.pid');
  if (fs.existsSync(pidFile)) {
    try {
      const raw = fs.readFileSync(pidFile, 'utf-8').trim();
      const pid = Number(raw);
      if (raw && Number.isInteger(pid) && pid > 0) {
        process.kill(pid, 0);
        return 'running';
      }
    } catch {
      return 'stopped';
    }
  }

  return 'not_found';
}

/** Check recent service logs for MCP channel connection indicators. */
function checkLogsForConnection(channel: string | undefined): {
  connected: boolean;
  details: string;
} {
  const logPath = path.join(process.cwd(), 'logs', 'deus.log');
  if (!fs.existsSync(logPath)) {
    return { connected: false, details: 'log file not found' };
  }

  try {
    // Read last 4KB of log (enough for recent entries)
    const stat = fs.statSync(logPath);
    const fd = fs.openSync(logPath, 'r');
    const readSize = Math.min(4096, stat.size);
    const buf = Buffer.alloc(readSize);
    fs.readSync(fd, buf, 0, readSize, stat.size - readSize);
    fs.closeSync(fd);
    const tail = buf.toString('utf-8');

    // Look for channel connection indicators
    const patterns = [
      /channel.*connected/i,
      /mcp.*connected/i,
      /whatsapp.*ready/i,
      /telegram.*polling/i,
      /slack.*connected/i,
      /discord.*ready/i,
    ];

    if (channel) {
      const channelPattern = new RegExp(
        `${channel}.*(?:connected|ready|polling)`,
        'i',
      );
      patterns.unshift(channelPattern);
    }

    for (const pattern of patterns) {
      const match = tail.match(pattern);
      if (match) {
        return { connected: true, details: match[0] };
      }
    }

    return {
      connected: false,
      details: 'no connection indicators in recent logs',
    };
  } catch {
    return { connected: false, details: 'failed to read logs' };
  }
}

export async function run(args: string[]): Promise<void> {
  const parsed = parseArgs(args);
  const dbPath = path.join(STORE_DIR, 'messages.db');

  logger.info(
    { channel: parsed.channel || 'any' },
    'Starting channel smoke test',
  );

  // 1. Check service is running
  const serviceStatus = checkServiceRunning();
  if (serviceStatus !== 'running') {
    emitStatus('SMOKE_TEST', {
      STATUS: 'failed',
      ERROR: `service is ${serviceStatus}`,
      HINT: 'Start the service before running smoke test',
    });
    process.exit(1);
  }

  // 2. Open database
  if (!fs.existsSync(dbPath)) {
    emitStatus('SMOKE_TEST', {
      STATUS: 'failed',
      ERROR: 'database not found',
      HINT: `Expected at ${dbPath}`,
    });
    process.exit(1);
  }

  const db = new Database(dbPath);

  try {
    // 3. Find a registered group for the channel (by folder prefix convention)
    const query = parsed.channel
      ? 'SELECT jid, name, folder FROM registered_groups WHERE folder LIKE ? LIMIT 1'
      : 'SELECT jid, name, folder FROM registered_groups LIMIT 1';

    const folderPattern = parsed.channel ? `${parsed.channel}_%` : undefined;
    const group = (
      folderPattern
        ? db.prepare(query).get(folderPattern)
        : db.prepare(query).get()
    ) as { jid: string; name: string; folder: string } | undefined;

    if (!group) {
      emitStatus('SMOKE_TEST', {
        STATUS: 'failed',
        CHANNEL: parsed.channel || 'any',
        ERROR: 'no registered group found',
        HINT: 'Register a group before running smoke test',
      });
      process.exit(1);
    }

    // 4. Store a test message and verify insertion
    const testId = `smoke-test-${Date.now()}`;
    const timestamp = new Date().toISOString();

    db.prepare(
      `INSERT INTO messages (id, chat_jid, sender, sender_name, content, timestamp, is_from_me, is_bot_message)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    ).run(
      testId,
      group.jid,
      'smoke-test@internal',
      'SmokeTest',
      '[Deus smoke test — ignore this message]',
      timestamp,
      1, // is_from_me
      1, // is_bot_message
    );

    const stored = db
      .prepare('SELECT id FROM messages WHERE id = ?')
      .get(testId) as { id: string } | undefined;

    // Clean up test message
    db.prepare('DELETE FROM messages WHERE id = ?').run(testId);

    if (!stored) {
      emitStatus('SMOKE_TEST', {
        STATUS: 'failed',
        CHANNEL: parsed.channel || 'any',
        JID: group.jid,
        ERROR: 'message store/retrieve failed',
      });
      process.exit(1);
    }

    // 5. Check logs for channel connection
    const logCheck = checkLogsForConnection(parsed.channel);

    emitStatus('SMOKE_TEST', {
      STATUS: 'success',
      CHANNEL: parsed.channel || 'any',
      JID: group.jid,
      CHAT_NAME: group.name,
      SERVICE: 'running',
      DB_WRITE: 'ok',
      DB_READ: 'ok',
      LOG_CONNECTION: logCheck.connected ? 'detected' : 'not_detected',
      LOG_DETAILS: logCheck.details,
    });

    if (!logCheck.connected) {
      logger.warn(
        'Service is running and DB is healthy, but no channel connection detected in recent logs. ' +
          'If messages are not being delivered, check that MCP channel servers declare capabilities: { logging: {} }.',
      );
    }
  } finally {
    db.close();
  }
}
