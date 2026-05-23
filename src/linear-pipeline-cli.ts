#!/usr/bin/env node
/**
 * CLI for pipeline event audit.
 *
 * Usage:
 *   deus pipeline LIA-123               Full timeline for an issue
 *   deus pipeline --failed --since 24h  All failures in last 24h
 *   deus pipeline --active              Currently in-flight issues
 */

import {
  getPipelineEvents,
  initDatabase,
  type PipelineEventFilter,
} from './db.js';
import { EVENT_LABELS } from './linear-notifications.js';

const GREEN = '\x1b[32m';
const RED = '\x1b[31m';
const YELLOW = '\x1b[33m';
const CYAN = '\x1b[36m';
const DIM = '\x1b[2m';
const RESET = '\x1b[0m';

const FAILED_TYPES = new Set([
  'gate_revise',
  'gate_error',
  'agent_failed',
  'automerge_failed',
]);

const SUCCESS_TYPES = new Set([
  'gate_ship',
  'automerge_done',
  'agent_completed',
  'moved_done',
]);

const PENDING_TYPES = new Set(['gate_cooldown', 'automerge_pending']);

const INFO_TYPES = new Set([
  'agent_dispatched',
  'agent_started',
  'pr_created',
  'state_changed',
]);

function colorFor(eventType: string): string {
  if (SUCCESS_TYPES.has(eventType)) return GREEN;
  if (FAILED_TYPES.has(eventType)) return RED;
  if (PENDING_TYPES.has(eventType)) return YELLOW;
  if (INFO_TYPES.has(eventType)) return CYAN;
  return DIM;
}

export function parseDuration(input: string): string | null {
  const match = input.match(/^(\d+)(m|h|d)$/);
  if (!match) return null;
  const value = parseInt(match[1], 10);
  const unit = match[2];
  const ms =
    unit === 'm'
      ? value * 60_000
      : unit === 'h'
        ? value * 3_600_000
        : value * 86_400_000;
  return new Date(Date.now() - ms).toISOString();
}

function printEvents(
  events: Array<{
    issue_id: string;
    identifier: string;
    event_type: string;
    detail: string | null;
    created_at: string;
  }>,
): void {
  if (events.length === 0) {
    console.log(`${DIM}No events found.${RESET}`);
    return;
  }

  for (const e of events) {
    const time = e.created_at.slice(0, 16).replace('T', ' ');
    const color = colorFor(e.event_type);
    const detail = e.detail ? ` ${DIM}— ${e.detail}${RESET}` : '';
    console.log(
      `${DIM}${time}${RESET}  ${CYAN}${e.identifier.padEnd(8)}${RESET}  ${color}${(EVENT_LABELS[e.event_type] || e.event_type).padEnd(22)}${RESET}${detail}`,
    );
  }
}

function main(): void {
  const args = process.argv.slice(2);

  if (args.length === 0 || args[0] === '--help') {
    console.log('Usage:');
    console.log('  deus pipeline <IDENTIFIER>          Timeline for an issue');
    console.log('  deus pipeline --failed [--since Xh]  Failed events');
    console.log('  deus pipeline --active               In-flight issues');
    console.log('  deus pipeline --all [--since Xh]     All events');
    process.exit(0);
  }

  initDatabase();

  const filter: PipelineEventFilter = {};
  let showFailed = false;
  let showActive = false;

  let i = 0;
  while (i < args.length) {
    const arg = args[i];
    if (arg === '--failed') {
      showFailed = true;
    } else if (arg === '--active') {
      showActive = true;
    } else if (arg === '--since' && args[i + 1]) {
      const since = parseDuration(args[i + 1]);
      if (!since) {
        console.error(
          `Invalid duration: ${args[i + 1]} (use e.g. 24h, 30m, 7d)`,
        );
        process.exit(1);
      }
      filter.since = since;
      i++;
    } else if (arg === '--type' && args[i + 1]) {
      filter.eventType = args[i + 1];
      i++;
    } else if (arg === '--all') {
      // no filter
    } else if (!arg.startsWith('--')) {
      filter.identifier = arg;
    }
    i++;
  }

  let events = getPipelineEvents(filter);

  if (showFailed) {
    events = events.filter((e) => FAILED_TYPES.has(e.event_type));
  }

  if (showActive) {
    const issueLatest = new Map<string, string>();
    for (const e of events) {
      issueLatest.set(e.issue_id, e.event_type);
    }
    const terminalTypes = new Set([
      'automerge_done',
      'moved_done',
      'automerge_failed',
      'agent_failed',
    ]);
    const activeIds = new Set<string>();
    for (const [id, lastType] of issueLatest) {
      if (!terminalTypes.has(lastType)) activeIds.add(id);
    }
    events = events.filter((e) => activeIds.has(e.issue_id));
  }

  printEvents(events);
}

const isDirectRun =
  process.argv[1]?.endsWith('linear-pipeline-cli.js') ||
  process.argv[1]?.endsWith('linear-pipeline-cli.ts');
if (isDirectRun) main();
