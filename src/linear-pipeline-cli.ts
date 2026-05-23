#!/usr/bin/env node
/**
 * Pipeline monitor and event audit CLI.
 *
 * Default (no args): live dashboard backed by webhook-fed SQLite cache (2s refresh).
 * With args: one-shot queries against the local event log.
 */

import { LinearClient } from '@linear/sdk';
import {
  getPipelineEvents,
  getIssuePr,
  getLatestStatusSummary,
  getReviseCount,
  getIssuesFromCache,
  getIssueCacheCount,
  getMaxCachedAt,
  reconcileIssueCache,
  initDatabase,
  type PipelineEventFilter,
} from './db.js';
import { readEnvFile } from './env.js';
import { EVENT_LABELS } from './linear-notifications.js';

const GREEN = '\x1b[32m';
const RED = '\x1b[31m';
const YELLOW = '\x1b[33m';
const CYAN = '\x1b[36m';
const DIM = '\x1b[2m';
const BOLD = '\x1b[1m';
const RESET = '\x1b[0m';

const HIDE_CURSOR = '\x1b[?25l';
const SHOW_CURSOR = '\x1b[?25h';
const CURSOR_HOME = '\x1b[H';
const CLEAR_TO_END = '\x1b[J';
const ENTER_ALT_SCREEN = '\x1b[?1049h';
const EXIT_ALT_SCREEN = '\x1b[?1049l';

const REFRESH_MS = 10_000;
const DISPLAY_REFRESH_MS = 2_000;
const BACKOFF_MS = 60_000;
const CACHE_STALE_MS = 6 * 60_000;
const RESYNC_INTERVAL_MS = 5 * 60_000;
const RECENT_WINDOW_MS = 30 * 60_000;

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

const TERMINAL_TYPES = new Set([
  'automerge_done',
  'automerge_failed',
  'agent_failed',
  'moved_done',
]);

const ACTIVE_STATES = ['Ready for Agent', 'Agent Working', 'In Review'];
const QUEUED_STATES = ['Backlog', 'Todo'];
const ALL_VISIBLE_STATES = [...QUEUED_STATES, ...ACTIVE_STATES];

const STUCK_THRESHOLD_MS = 7_200_000; // 2h
const WARN_THRESHOLD_MS = 1_800_000; // 30m

function colorFor(eventType: string): string {
  if (SUCCESS_TYPES.has(eventType)) return GREEN;
  if (FAILED_TYPES.has(eventType)) return RED;
  if (PENDING_TYPES.has(eventType)) return YELLOW;
  if (INFO_TYPES.has(eventType)) return CYAN;
  return DIM;
}

function stateGlyph(stateName: string): { glyph: string; color: string } {
  switch (stateName) {
    case 'Backlog':
      return { glyph: '·', color: DIM };
    case 'Todo':
      return { glyph: '○', color: DIM };
    case 'Ready for Agent':
      return { glyph: '◇', color: YELLOW };
    case 'In Progress':
      return { glyph: '●', color: YELLOW };
    case 'Agent Working':
      return { glyph: '▶', color: CYAN };
    case 'In Review':
      return { glyph: '◆', color: CYAN };
    default:
      return { glyph: '?', color: DIM };
  }
}

export function elapsedMs(isoTimestamp: string): number {
  return Math.max(0, Date.now() - new Date(isoTimestamp).getTime());
}

export function computeColumnWidths(cols: number): {
  titleWidth: number;
  separatorWidth: number;
} {
  const clamped = Math.max(80, cols);
  // ID(8) + glyph(2) + PR(6) + status(24) + elapsed(6) + revise(4) + separators(6) = 56
  const titleWidth = Math.max(20, clamped - 56);
  return { titleWidth, separatorWidth: clamped - 2 };
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

export function formatElapsed(isoTimestamp: string): string {
  const ms = elapsedMs(isoTimestamp);
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

function truncate(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str;
  return str.slice(0, maxLen - 1) + '…';
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

// ── Watch mode ──────────────────────────────────────────────────────────────

interface ActiveIssue {
  identifier: string;
  title: string;
  stateName: string;
  lastEvent: string;
  lastEventTime: string;
  prNumber: string | null;
  statusSummary: string | null;
  reviseCount: number;
}

interface QueuedIssue {
  identifier: string;
  title: string;
  stateName: string;
  createdAt: string;
}

interface RecentIssue {
  identifier: string;
  eventType: string;
  lastEvent: string;
  lastEventTime: string;
  detail: string | null;
}

async function discoverTeamId(client: LinearClient): Promise<string> {
  const teams = await client.teams();
  if (teams.nodes.length === 0) {
    throw new Error('No Linear teams found');
  }
  const override = process.env.LINEAR_TEAM_ID;
  if (override) {
    const match = teams.nodes.find((t) => t.id === override);
    if (!match) {
      throw new Error(
        `LINEAR_TEAM_ID "${override}" not found among ${teams.nodes.length} teams`,
      );
    }
    return match.id;
  }
  return teams.nodes[0].id;
}

function extractPrNumber(prUrl: string): string | null {
  const match = prUrl.match(/\/pull\/(\d+)/);
  return match ? `#${match[1]}` : null;
}

function isCacheStale(): boolean {
  const count = getIssueCacheCount();
  if (count === 0) return true;
  const maxCachedAt = getMaxCachedAt();
  if (!maxCachedAt) return true;
  return elapsedMs(maxCachedAt) > CACHE_STALE_MS;
}

async function seedOrResyncCache(
  client: LinearClient,
  teamId: string,
): Promise<void> {
  const issues = await client.issues({
    filter: {
      state: { name: { in: ALL_VISIBLE_STATES } },
      team: { id: { eq: teamId } },
    },
  });
  const states = await Promise.all(issues.nodes.map((issue) => issue.state));

  if (issues.nodes.length === 0) return;

  const upserts = issues.nodes.map((issue, i) => ({
    issue_id: issue.id,
    identifier: issue.identifier,
    title: issue.title,
    state_name: states[i]?.name ?? 'Unknown',
    team_id: teamId,
    priority: issue.priority,
    created_at: issue.createdAt.toISOString(),
    updated_at: issue.updatedAt.toISOString(),
  }));

  const liveIds = new Set(issues.nodes.map((i) => i.id));
  reconcileIssueCache(liveIds, upserts);
}

function enrichFromDb(
  issueId: string,
  fallbackUpdatedAt: string,
): {
  lastEvent: string;
  lastEventTime: string;
  prNumber: string | null;
  statusSummary: string | null;
  reviseCount: number;
} {
  const events = getPipelineEvents({ issueId });
  const last = events.length > 0 ? events[events.length - 1] : null;
  const revCount = events.filter((e) => e.event_type === 'gate_revise').length;
  const pr = getIssuePr(issueId);
  const prNumber = pr ? extractPrNumber(pr.pr_url) : null;
  const statusSummary = getLatestStatusSummary(issueId);

  return {
    lastEvent: last ? EVENT_LABELS[last.event_type] || last.event_type : '—',
    lastEventTime: last?.created_at ?? fallbackUpdatedAt,
    prNumber,
    statusSummary,
    reviseCount: revCount,
  };
}

async function fetchActiveAndQueued(
  client: LinearClient,
  teamId: string,
): Promise<{
  active: ActiveIssue[];
  queued: QueuedIssue[];
  fromCache: boolean;
}> {
  let fromCache = !isCacheStale();
  if (!fromCache) {
    await seedOrResyncCache(client, teamId);
    fromCache = true;
  }

  const cached = getIssuesFromCache(ALL_VISIBLE_STATES);
  const active: ActiveIssue[] = [];
  const queued: QueuedIssue[] = [];

  for (const row of cached) {
    if (QUEUED_STATES.includes(row.state_name)) {
      queued.push({
        identifier: row.identifier,
        title: row.title,
        stateName: row.state_name,
        createdAt: row.created_at,
      });
      continue;
    }

    const enriched = enrichFromDb(row.issue_id, row.updated_at);
    active.push({
      identifier: row.identifier,
      title: row.title,
      stateName: row.state_name,
      ...enriched,
    });
  }

  active.sort((a, b) => {
    const stateOrder =
      ACTIVE_STATES.indexOf(a.stateName) - ACTIVE_STATES.indexOf(b.stateName);
    if (stateOrder !== 0) return stateOrder;
    return (
      new Date(b.lastEventTime).getTime() - new Date(a.lastEventTime).getTime()
    );
  });

  queued.sort(
    (a, b) => new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime(),
  );

  return { active, queued, fromCache };
}

function fetchRecentIssues(): RecentIssue[] {
  const since = new Date(Date.now() - RECENT_WINDOW_MS).toISOString();
  const events = getPipelineEvents({ since });

  const issueLatest = new Map<
    string,
    {
      identifier: string;
      event_type: string;
      created_at: string;
      detail: string | null;
    }
  >();
  for (const e of events) {
    issueLatest.set(e.issue_id, {
      identifier: e.identifier,
      event_type: e.event_type,
      created_at: e.created_at,
      detail: e.detail,
    });
  }

  const recent: RecentIssue[] = [];
  for (const [, last] of issueLatest) {
    if (TERMINAL_TYPES.has(last.event_type)) {
      recent.push({
        identifier: last.identifier,
        eventType: last.event_type,
        lastEvent: EVENT_LABELS[last.event_type] || last.event_type,
        lastEventTime: last.created_at,
        detail: last.detail,
      });
    }
  }

  return recent.sort(
    (a, b) =>
      new Date(b.lastEventTime).getTime() - new Date(a.lastEventTime).getTime(),
  );
}

function renderDashboardOutput(
  active: ActiveIssue[],
  queued: QueuedIssue[],
  recent: RecentIssue[],
  error?: string,
  currentRefreshMs?: number,
  warning?: string,
): string {
  const now = new Date().toLocaleTimeString('en-GB', { hour12: false });
  const cols = process.stdout.columns || 100;
  const { titleWidth, separatorWidth } = computeColumnWidths(cols);
  const lines: string[] = [];

  const stuckCount = active.filter(
    (i) => elapsedMs(i.lastEventTime) > STUCK_THRESHOLD_MS,
  ).length;
  const failCount = recent.filter((i) => FAILED_TYPES.has(i.eventType)).length;

  const stuckLabel =
    stuckCount > 0
      ? `${YELLOW}STUCK ${stuckCount}${RESET}`
      : `${DIM}STUCK 0${RESET}`;
  const failLabel =
    failCount > 0 ? `${RED}FAIL ${failCount}${RESET}` : `${DIM}FAIL 0${RESET}`;
  const statsBar = `ACTIVE ${active.length} ${DIM}·${RESET} ${stuckLabel} ${DIM}·${RESET} ${failLabel}`;

  const effectiveRefreshMs = currentRefreshMs ?? REFRESH_MS;
  const refreshSec = effectiveRefreshMs / 1000;
  const refreshLabel =
    effectiveRefreshMs > REFRESH_MS
      ? `${YELLOW}[every ${refreshSec}s ↓]${RESET}`
      : `${DIM}[every ${refreshSec}s]${RESET}`;
  lines.push(
    `${BOLD} Deus Pipeline${RESET}    ${statsBar}${DIM}${' '.repeat(Math.max(1, cols - 70))}${now}  ${RESET}${refreshLabel}`,
  );
  lines.push(`${DIM} ${'─'.repeat(separatorWidth)}${RESET}`);

  if (error) {
    lines.push(`${RED} ⚠ ${error}${RESET}`);
    lines.push('');
  }

  if (warning) {
    lines.push(`${YELLOW} ⚠ ${warning}${RESET}`);
  }

  lines.push('');

  if (active.length === 0) {
    lines.push(`${DIM}  No issues in pipeline.${RESET}`);
  } else {
    for (const issue of active) {
      const id = issue.identifier.padEnd(8);
      const sg = stateGlyph(issue.stateName);
      const glyph = `${sg.color}${sg.glyph}${RESET}`;
      const title = truncate(issue.title, titleWidth).padEnd(titleWidth);
      const pr = issue.prNumber
        ? `${DIM}${issue.prNumber.padStart(5)}${RESET}`
        : `${DIM}${'—'.padStart(5)}${RESET}`;
      const statusText = issue.statusSummary ?? issue.lastEvent;
      const status = truncate(statusText, 22).padEnd(22);

      const ms = elapsedMs(issue.lastEventTime);
      const elapsedStr = formatElapsed(issue.lastEventTime).padStart(4);
      let elapsedColored: string;
      if (ms > STUCK_THRESHOLD_MS) {
        elapsedColored = `${RED}${elapsedStr} !!${RESET}`;
      } else if (ms > WARN_THRESHOLD_MS) {
        elapsedColored = `${YELLOW}${elapsedStr}${RESET}`;
      } else {
        elapsedColored = `${DIM}${elapsedStr}${RESET}`;
      }

      const revise =
        issue.reviseCount > 0 ? ` ${RED}×${issue.reviseCount}${RESET}` : '';

      lines.push(
        `  ${CYAN}${id}${RESET}${glyph} ${title} ${pr} ${DIM}${status}${RESET} ${elapsedColored}${revise}`,
      );
    }
  }

  if (queued.length > 0) {
    lines.push('');
    lines.push(`${DIM} QUEUED (${queued.length})${RESET}`);
    for (const issue of queued) {
      const id = issue.identifier.padEnd(8);
      const sg = stateGlyph(issue.stateName);
      const glyph = `${sg.color}${sg.glyph}${RESET}`;
      const title = truncate(issue.title, titleWidth).padEnd(titleWidth);
      const elapsed = formatElapsed(issue.createdAt).padStart(4);
      lines.push(
        `${DIM}  ${id}${RESET}${glyph} ${DIM}${title} ${' '.repeat(28)} ${elapsed}${RESET}`,
      );
    }
  }

  if (recent.length > 0) {
    lines.push('');
    lines.push(`${DIM} RECENT (${recent.length})${RESET}`);
    for (const issue of recent) {
      const id = issue.identifier.padEnd(8);
      const evtColor = FAILED_TYPES.has(issue.eventType) ? RED : GREEN;
      const evt = truncate(issue.lastEvent, 22).padEnd(22);
      const detail = issue.detail ? truncate(issue.detail, titleWidth) : '';
      const elapsed = formatElapsed(issue.lastEventTime).padStart(4);
      lines.push(
        `${DIM}  ${id} ${' '.repeat(2)}${evtColor}${evt}${DIM} ${detail.padEnd(titleWidth)} ${elapsed}${RESET}`,
      );
    }
  }

  lines.push('');
  lines.push(`${DIM} Ctrl+C to exit${RESET}`);

  return lines.join('\n');
}

async function startWatchMode(): Promise<void> {
  const env = readEnvFile([
    'LINEAR_API_KEY',
    'LINEAR_API_TOKEN',
    'LINEAR_TEAM_ID',
  ]);
  for (const [k, v] of Object.entries(env)) {
    if (v && !process.env[k]) process.env[k] = v;
  }

  const apiKey = process.env.LINEAR_API_KEY || process.env.LINEAR_API_TOKEN;
  if (!apiKey) {
    console.error(
      `${RED}LINEAR_API_TOKEN not set${RESET} — add it to .env for live monitoring.`,
    );
    console.error(
      `${DIM}Falling back to DB-only view. Use 'deus pipeline --active' for one-shot.${RESET}`,
    );
    initDatabase();
    const events = getPipelineEvents();
    const issueLatest = new Map<string, string>();
    for (const e of events) issueLatest.set(e.issue_id, e.event_type);
    const activeIds = new Set<string>();
    for (const [id, lastType] of issueLatest) {
      if (!TERMINAL_TYPES.has(lastType)) activeIds.add(id);
    }
    printEvents(events.filter((e) => activeIds.has(e.issue_id)));
    return;
  }

  initDatabase();

  const client = new LinearClient({ apiKey });
  let teamId: string;
  try {
    teamId = await discoverTeamId(client);
  } catch (err) {
    console.error(
      `${RED}Failed to connect to Linear:${RESET} ${err instanceof Error ? err.message : err}`,
    );
    process.exit(1);
  }

  process.stdout.write(ENTER_ALT_SCREEN + HIDE_CURSOR);

  let rendering = false;
  let lastError: string | undefined;
  let nextRefreshMs = DISPLAY_REFRESH_MS;
  let timer: ReturnType<typeof setTimeout> | undefined;

  function isRateLimitError(msg: string): boolean {
    return /rate.?limit/i.test(msg) || /429/.test(msg) || /too many/i.test(msg);
  }

  async function tick(): Promise<void> {
    if (rendering) return;
    rendering = true;
    try {
      const { active, queued, fromCache } = await fetchActiveAndQueued(
        client,
        teamId,
      );
      const recent = fetchRecentIssues();
      lastError = undefined;
      nextRefreshMs = fromCache ? DISPLAY_REFRESH_MS : REFRESH_MS;
      const warning = fromCache ? undefined : 'cache stale — polling API';
      const output = renderDashboardOutput(
        active,
        queued,
        recent,
        undefined,
        nextRefreshMs,
        warning,
      );
      process.stdout.write(CURSOR_HOME + output + '\n' + CLEAR_TO_END);
    } catch (err) {
      lastError = err instanceof Error ? err.message : 'Unknown error';
      if (isRateLimitError(lastError)) {
        nextRefreshMs = BACKOFF_MS;
      } else {
        nextRefreshMs = REFRESH_MS;
      }
      const recent = fetchRecentIssues();
      const output = renderDashboardOutput(
        [],
        [],
        recent,
        lastError,
        nextRefreshMs,
      );
      process.stdout.write(CURSOR_HOME + output + '\n' + CLEAR_TO_END);
    } finally {
      rendering = false;
      timer = setTimeout(tick, nextRefreshMs);
    }
  }

  await tick();

  const resyncTimer = setInterval(() => {
    seedOrResyncCache(client, teamId).catch((err) => {
      console.error('issue-cache: background resync failed', err);
    });
  }, RESYNC_INTERVAL_MS);

  const cleanup = () => {
    if (timer) clearTimeout(timer);
    clearInterval(resyncTimer);
    process.stdout.write(EXIT_ALT_SCREEN + SHOW_CURSOR);
    process.exit(0);
  };

  process.on('SIGINT', cleanup);
  process.on('SIGTERM', cleanup);
  process.on('SIGWINCH', () => {
    if (!rendering) tick().catch(() => {});
  });
}

// ── One-shot mode (existing behavior) ───────────────────────────────────────

function main(): void {
  const args = process.argv.slice(2);

  if (args.length === 0) {
    startWatchMode().catch((err) => {
      process.stdout.write(EXIT_ALT_SCREEN + SHOW_CURSOR);
      console.error(err);
      process.exit(1);
    });
    return;
  }

  if (args[0] === '--help') {
    console.log('Usage:');
    console.log(
      '  deus pipeline                        Live monitor (default)',
    );
    console.log('  deus pipeline <IDENTIFIER>           Timeline for an issue');
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
