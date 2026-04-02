/**
 * Mutable router state — the four variables that persist across poll cycles.
 *
 * RouterState is the single source of truth for:
 *   - lastTimestamp        — "seen" cursor for all incoming messages
 *   - lastAgentTimestamp   — per-group cursor of last message sent to the agent
 *   - sessions             — per-group Claude Code session IDs
 *   - registeredGroups     — groups that Deus is configured to handle
 *
 * All persistence (DB reads/writes) is centralised here.
 */

import fs from 'fs';
import path from 'path';

import {
  getAllChats,
  getAllRegisteredGroups,
  getAllSessions,
  getRouterState,
  setRegisteredGroup,
  setRouterState,
} from './db.js';
import type { AvailableGroup } from './container-runner.js';
import { resolveGroupFolderPath } from './group-folder.js';
import { logger } from './logger.js';
import { RegisteredGroup } from './types.js';

export class RouterState {
  private _lastTimestamp = '';
  private _sessions: Record<string, string> = {};
  private _registeredGroups: Record<string, RegisteredGroup> = {};
  private _lastAgentTimestamp: Record<string, string> = {};

  load(): void {
    this._lastTimestamp = getRouterState('last_timestamp') || '';
    const agentTs = getRouterState('last_agent_timestamp');
    try {
      this._lastAgentTimestamp = agentTs ? JSON.parse(agentTs) : {};
    } catch {
      logger.warn('Corrupted last_agent_timestamp in DB, resetting');
      this._lastAgentTimestamp = {};
    }
    this._sessions = getAllSessions();
    this._registeredGroups = getAllRegisteredGroups();
    logger.info(
      { groupCount: Object.keys(this._registeredGroups).length },
      'State loaded',
    );
  }

  save(): void {
    setRouterState('last_timestamp', this._lastTimestamp);
    setRouterState(
      'last_agent_timestamp',
      JSON.stringify(this._lastAgentTimestamp),
    );
  }

  get lastTimestamp(): string {
    return this._lastTimestamp;
  }

  set lastTimestamp(ts: string) {
    this._lastTimestamp = ts;
  }

  getLastAgentTimestamp(jid: string): string {
    return this._lastAgentTimestamp[jid] || '';
  }

  setLastAgentTimestamp(jid: string, ts: string): void {
    this._lastAgentTimestamp[jid] = ts;
  }

  get sessions(): Record<string, string> {
    return this._sessions;
  }

  getSession(folder: string): string | undefined {
    return this._sessions[folder];
  }

  setSession(folder: string, sessionId: string): void {
    this._sessions[folder] = sessionId;
  }

  clearSession(folder: string): void {
    delete this._sessions[folder];
  }

  get registeredGroups(): Record<string, RegisteredGroup> {
    return this._registeredGroups;
  }

  registerGroup(jid: string, group: RegisteredGroup): void {
    let groupDir: string;
    try {
      groupDir = resolveGroupFolderPath(group.folder);
    } catch (err) {
      logger.warn(
        { jid, folder: group.folder, err },
        'Rejecting group registration with invalid folder',
      );
      return;
    }
    this._registeredGroups[jid] = group;
    setRegisteredGroup(jid, group);
    fs.mkdirSync(path.join(groupDir, 'logs'), { recursive: true });
    logger.info(
      { jid, name: group.name, folder: group.folder },
      'Group registered',
    );
  }

  /** @internal — for testing only */
  _setRegisteredGroups(groups: Record<string, RegisteredGroup>): void {
    this._registeredGroups = groups;
  }
}

/**
 * Returns all known groups available for registration, ordered by most recent
 * activity. Pure function — takes registered groups as a parameter so callers
 * control which state snapshot is used (easier to test).
 */
export function getAvailableGroups(
  registeredGroups: Record<string, RegisteredGroup>,
): AvailableGroup[] {
  const chats = getAllChats();
  const registeredJids = new Set(Object.keys(registeredGroups));
  return chats
    .filter((c) => c.jid !== '__group_sync__' && c.is_group)
    .map((c) => ({
      jid: c.jid,
      name: c.name,
      lastActivity: c.last_message_time,
      isRegistered: registeredJids.has(c.jid),
    }));
}
