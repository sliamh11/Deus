/**
 * Mutable router state — the four variables that persist across poll cycles.
 *
 * RouterState is the single source of truth for:
 *   - lastTimestamp        — "seen" cursor for all incoming messages
 *   - lastAgentTimestamp   — per-group cursor of last message sent to the agent
 *   - sessions             — per-group backend session references
 *   - registeredGroups     — groups that Deus is configured to handle
 *
 * All persistence (DB reads/writes) is centralised here.
 */

import fs from 'fs';
import path from 'path';

import {
  getAllChats,
  getAllRegisteredGroups,
  getAllBackendSessions,
  getRouterState,
  setRegisteredGroup,
  setRouterState,
} from './db.js';
import type { AvailableGroup } from './container-runner.js';
import type { AgentRuntimeId, RuntimeSession } from './agent-runtimes/types.js';
import { resolveGroupFolderPath } from './group-folder.js';
import { logger } from './logger.js';
import { RegisteredGroup } from './types.js';

export class RouterState {
  private _lastTimestamp = '';
  private _sessions: Record<
    string,
    Partial<Record<AgentRuntimeId, RuntimeSession>>
  > = {};
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
    this._sessions = getAllBackendSessions();
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

  get sessions(): Record<string, RuntimeSession> {
    const flat: Record<string, RuntimeSession> = {};
    for (const [folder, refs] of Object.entries(this._sessions)) {
      const ref = refs.claude ?? refs.openai;
      if (ref) flat[folder] = ref;
    }
    return flat;
  }

  getSession(
    folder: string,
    backend?: AgentRuntimeId,
  ): RuntimeSession | undefined {
    const refs = this._sessions[folder];
    if (!refs) return undefined;
    return backend ? refs[backend] : (refs.claude ?? refs.openai);
  }

  setSession(folder: string, session: RuntimeSession): void {
    this._sessions[folder] ??= {};
    this._sessions[folder][session.backend] = session;
  }

  clearSession(folder: string, backend?: AgentRuntimeId): void {
    if (!backend) {
      delete this._sessions[folder];
      return;
    }
    delete this._sessions[folder]?.[backend];
    if (Object.keys(this._sessions[folder] ?? {}).length === 0) {
      delete this._sessions[folder];
    }
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
