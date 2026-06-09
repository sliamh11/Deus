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
  getIssuesFromCache,
  getIssueCacheCount,
  getMaxCachedAt,
  reconcileIssueCache,
  getStageEntryTime,
  computeStageMedians,
  initDatabase,
  type PipelineEventFilter,
} from './db.js';
import { readEnvFile } from './env.js';
import { EVENT_LABELS } from './linear-notifications.js';
import { formatLocalDateTime } from './timezone.js';
import {
  initActionContext,
  handleOpenInBrowser,
  toggleWardenSkip,
  triggerGateRerun,
  moveIssueState,
  startIssueOrchestration,
  STARTABLE_STATES,
  type ActionContext,
} from './linear-actions.js';
import { setPollInterval } from './linear-dispatcher.js';

// ── Footer throughput types ──────────────────────────────────────────────────

export interface TodayStats {
  shipped: number;
  failed: number;
  medianAgentMs: number | null;
  automergeFailRate: number | null; // 0-100 percent, null if no automerge events
}

export type GateRevisionCounts = Record<string, number>;

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

export const TERMINAL_TYPES = new Set([
  'automerge_done',
  'automerge_failed',
  'agent_failed',
  'moved_done',
  'gate_error',
  'gate_blocked',
]);

const ACTIVE_STATES = ['Ready for Agent', 'Agent Working', 'In Review'];
const QUEUED_STATES = ['Backlog', 'Todo'];
const ALL_VISIBLE_STATES = [...QUEUED_STATES, ...ACTIVE_STATES];

const STUCK_THRESHOLD_MS = 7_200_000; // 2h
const WARN_THRESHOLD_MS = 1_800_000; // 30m

const GATE_PREFIX_RE = /^Gate review required[;:]\s*/i;
const AGENT_PREFIX_RE = /^Agent started working[;:]\s*/i;

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
  showWhy: boolean;
  showStageBar: boolean;
} {
  const showWhy = cols >= 130;
  const showStageBar = cols >= 110;
  let overhead = 64; // +6 for ETA column (1 space + 5 chars)
  if (showWhy) overhead += 18;
  if (showStageBar) overhead += 12;
  const clamped = Math.max(80, cols);
  const titleWidth = Math.max(20, clamped - overhead);
  return { titleWidth, separatorWidth: clamped - 2, showWhy, showStageBar };
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

/** Strip ANSI escape sequences for display-width measurement. */
function stripAnsi(str: string): string {
  // eslint-disable-next-line no-control-regex
  return str.replace(/\x1b\[[0-9;]*m/g, '');
}

/**
 * Compute the ETA display string for the 5-char ETA column.
 *
 * Returns:
 *   `'?'`      when sampleSize < 5, stageEntryTime is null, or medianMs is undefined
 *   `'~Xm'`    when remainingMs > 0 (X = ceiling of remaining minutes)
 *   yellow `'past'` when spent >= median and spent <= 1.5× median
 *   red    `'past'` when spent > 1.5× median
 */
export function computeETADisplay(
  stageEntryTime: string | null,
  medianMs: number | undefined,
  sampleSize: number,
): string {
  if (sampleSize < 5 || stageEntryTime === null || medianMs === undefined) {
    return '?';
  }
  const spentMs = Date.now() - new Date(stageEntryTime).getTime();
  const remainingMs = medianMs - spentMs;
  if (remainingMs > 0) {
    return '~' + Math.ceil(remainingMs / 60_000) + 'm';
  }
  if (spentMs > 1.5 * medianMs) {
    return RED + 'past' + RESET;
  }
  return YELLOW + 'past' + RESET;
}

function truncate(str: string, maxLen: number): string {
  // DB stores raw gh stderr which embeds newlines/tabs that break TUI rows
  const clean = str.replace(/[\r\n\t]+/g, ' ');
  if (clean.length <= maxLen) return clean;
  return clean.slice(0, maxLen - 1) + '…';
}

function formatMedianMs(ms: number): string {
  const minutes = Math.round(ms / 60_000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remaining = minutes % 60;
  return remaining > 0 ? `${hours}h${remaining}m` : `${hours}h`;
}

// ── Footer throughput computations ───────────────────────────────────────────

export function computeTodayStats(): TodayStats {
  const todayStart = new Date();
  todayStart.setHours(0, 0, 0, 0); // local midnight
  const since = todayStart.toISOString();

  const todayEvents = getPipelineEvents({ since });

  const shipped = todayEvents.filter(
    (e) => e.event_type === 'moved_done' || e.event_type === 'automerge_done',
  ).length;

  const failed = todayEvents.filter(
    (e) =>
      e.event_type === 'automerge_failed' || e.event_type === 'agent_failed',
  ).length;

  const automergeTerminal = todayEvents.filter(
    (e) =>
      e.event_type === 'automerge_done' || e.event_type === 'automerge_failed',
  ).length;
  const automergeFailed = todayEvents.filter(
    (e) => e.event_type === 'automerge_failed',
  ).length;
  const automergeFailRate =
    automergeTerminal > 0
      ? Math.round((automergeFailed / automergeTerminal) * 100)
      : null;

  // Median agent duration: time from agent_started → agent_completed / pr_created.
  // Look for starts in all history so we capture cycles that began before today.
  const allEvents = getPipelineEvents();
  const issueAgentStart = new Map<string, number>(); // issue_id → latest start ms
  for (const e of allEvents) {
    if (e.event_type === 'agent_started') {
      issueAgentStart.set(e.issue_id, new Date(e.created_at).getTime());
    }
  }

  const durations: number[] = [];
  for (const e of todayEvents) {
    if (e.event_type === 'agent_completed' || e.event_type === 'pr_created') {
      const startMs = issueAgentStart.get(e.issue_id);
      if (startMs !== undefined) {
        const dur = new Date(e.created_at).getTime() - startMs;
        if (dur > 0) durations.push(dur);
      }
    }
  }

  let medianAgentMs: number | null = null;
  if (durations.length > 0) {
    durations.sort((a, b) => a - b);
    const mid = Math.floor(durations.length / 2);
    medianAgentMs =
      durations.length % 2 === 0
        ? (durations[mid - 1] + durations[mid]) / 2
        : durations[mid];
  }

  return { shipped, failed, medianAgentMs, automergeFailRate };
}

export function computeGateRevisions(): GateRevisionCounts {
  const events = getPipelineEvents({ eventType: 'gate_revise' });
  const counts: GateRevisionCounts = {};

  for (const e of events) {
    if (e.detail) {
      // detail format: "{gate-name}: REVISE"  e.g. "completion-gate: REVISE"
      const match = e.detail.match(/^(.+?):\s*REVISE\b/i);
      if (match) {
        const name = match[1].trim();
        counts[name] = (counts[name] ?? 0) + 1;
      }
    }
  }

  return counts;
}

// ── Footer throughput renderer ───────────────────────────────────────────────

export function renderThroughputFooter(
  stats: TodayStats,
  gateRevisions: GateRevisionCounts,
  cols: number,
): string[] {
  const lines: string[] = [];

  // Line 1 — daily throughput stats
  const dot = `${DIM}·${RESET}`;
  const shippedStr = `${GREEN}${stats.shipped} shipped${RESET}`;
  const failedStr =
    stats.failed > 0
      ? `${RED}${stats.failed} failed${RESET}`
      : `${DIM}${stats.failed} failed${RESET}`;
  const medianStr =
    stats.medianAgentMs !== null
      ? `Median agent ${CYAN}${formatMedianMs(stats.medianAgentMs)}${RESET}`
      : `${DIM}Median agent —${RESET}`;
  const failRateStr =
    stats.automergeFailRate !== null
      ? `Automerge fail ${stats.automergeFailRate > 50 ? RED : DIM}${stats.automergeFailRate}%${RESET}`
      : `${DIM}Automerge fail —${RESET}`;

  lines.push(
    ` ${DIM}Today${RESET} ${shippedStr} ${dot} ${failedStr} ${dot} ${medianStr} ${dot} ${failRateStr}`,
  );

  // Line 2 — per-gate revision bars, sorted descending by count (worst first)
  const gates = Object.entries(gateRevisions).sort((a, b) => b[1] - a[1]);

  if (gates.length > 0) {
    const maxCount = gates[0][1];
    const MAX_BAR = 5;

    const prefix = ` ${DIM}Gate revisions${RESET}  `;
    let line2 = prefix;
    let visibleLen = stripAnsi(prefix).length;

    for (let i = 0; i < gates.length; i++) {
      const [name, count] = gates[i];
      const barLen = Math.max(1, Math.round((count / maxCount) * MAX_BAR));
      // Highest-count gate gets solid block; others get lighter shade
      const barChar = count === maxCount ? '▓' : '░';
      const bar = barChar.repeat(barLen);
      const sep = i > 0 ? '  ' : '';
      const segment = `${DIM}${name}${RESET} ${CYAN}${bar}${RESET}`;
      const segVisible = sep + name + ' ' + bar;

      if (visibleLen + segVisible.length > cols - 1) break;

      line2 += sep + segment;
      visibleLen += segVisible.length;
    }

    lines.push(line2);
  }

  return lines;
}

// ── Stage bar ──────────────────────────────────────────────────────────────

/**
 * A pipeline event row as returned by getPipelineEvents.
 */
export interface PipelineEvent {
  event_type: string;
  created_at: string;
}

/**
 * Renders a fixed-width 10-character stage progress bar for a single issue.
 *
 * Bar format: `[ ▓▓▓░·· ]`  (bracket + space + 6 stage chars + space + bracket = 10 chars)
 *
 * Stage mapping (in order):
 *  1. Scope            — first `gate_ship` event before any `agent_started`
 *  2. Dispatch/working — `agent_started`
 *  3. Agent done / PR  — `agent_completed` OR `pr_created` (whichever first)
 *  4. Quality gate     — first `gate_ship` after stage 3
 *  5. Completion gate  — second `gate_ship` after stage 3
 *  6. Auto-merge       — `automerge_done`
 *
 * Character rules:
 *  · = not started
 *  ░ = in progress (current active frontier)
 *  ▓ = completed
 *
 * ANSI color rules:
 *  - completed ▓ → DIM
 *  - active ░    → bright CYAN
 *  - completed ▓ where a gate_revise precedes the corresponding gate_ship → RED
 */
export function buildStageBar(events: PipelineEvent[]): string {
  // Stage completion flags
  let stage1Done = false; // Scope gate_ship (before agent_started)
  let stage2Done = false; // agent_started
  let stage3Done = false; // agent_completed or pr_created
  let stage4Done = false; // first gate_ship after stage 3
  let stage5Done = false; // second gate_ship after stage 3

  // Whether a gate_revise preceded the gate_ship for stage 4 or 5
  let stage4Revised = false;
  let stage5Revised = false;
  let stage6Done = false; // automerge_done

  // We also need to track whether there was a gate_revise before the scope gate_ship (stage 1)
  let stage1Revised = false;

  // Process events in order to determine stage completion
  let agentStartedSeen = false;
  let stage3EventSeen = false;
  let postStage3GateShipCount = 0;
  let postStage3ReviseBeforeNextShip = false;

  for (const ev of events) {
    const t = ev.event_type;

    if (!agentStartedSeen) {
      // We are in the pre-agent phase (stage 1 territory)
      if (t === 'gate_revise') {
        // revise before any agent_started → might affect stage 1 red coloring
        stage1Revised = true;
      } else if (t === 'gate_ship') {
        if (!stage1Done) {
          stage1Done = true;
          // stage1Revised already set above if applicable
        }
      } else if (t === 'agent_started') {
        agentStartedSeen = true;
        stage2Done = true;
      }
    } else {
      // agent_started has been seen
      if (!stage3EventSeen) {
        if (t === 'agent_completed' || t === 'pr_created') {
          stage3Done = true;
          stage3EventSeen = true;
        }
      } else {
        // stage 3 done, now counting gate_ships for stage 4 & 5
        if (t === 'gate_revise') {
          postStage3ReviseBeforeNextShip = true;
        } else if (t === 'gate_ship') {
          postStage3GateShipCount++;
          if (postStage3GateShipCount === 1) {
            stage4Done = true;
            stage4Revised = postStage3ReviseBeforeNextShip;
            postStage3ReviseBeforeNextShip = false;
          } else if (postStage3GateShipCount === 2) {
            stage5Done = true;
            stage5Revised = postStage3ReviseBeforeNextShip;
            postStage3ReviseBeforeNextShip = false;
          }
        } else if (t === 'automerge_done') {
          stage6Done = true;
        }
      }
    }
  }

  // Determine the active frontier: the first stage that is NOT done
  // Stages: 1,2,3,4,5,6
  const stages = [
    stage1Done,
    stage2Done,
    stage3Done,
    stage4Done,
    stage5Done,
    stage6Done,
  ];
  const revised = [
    stage1Revised,
    false, // stage 2 (agent_started) can't have a revise
    false, // stage 3
    stage4Revised,
    stage5Revised,
    false, // stage 6
  ];

  // Find first incomplete stage index (0-based)
  const frontierIdx = stages.findIndex((done) => !done);

  const chars: string[] = [];
  for (let i = 0; i < 6; i++) {
    if (stages[i]) {
      // Completed
      const glyph = '▓';
      const color = revised[i] ? RED : DIM;
      chars.push(`${color}${glyph}${RESET}`);
    } else if (i === frontierIdx) {
      // Active frontier
      chars.push(`\x1b[96m░${RESET}`); // bright cyan
    } else {
      // Not started
      chars.push(`${DIM}·${RESET}`);
    }
  }

  return `[ ${chars.join('')} ]`;
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

  const cols = process.stdout.columns || 80;
  const detailWidth = Math.max(10, cols - 46);

  for (const e of events) {
    const time = formatLocalDateTime(e.created_at);
    const color = colorFor(e.event_type);
    const detail = e.detail
      ? ` ${DIM}— ${truncate(e.detail, detailWidth)}${RESET}`
      : '';
    console.log(
      `${DIM}${time}${RESET}  ${CYAN}${e.identifier.padEnd(8)}${RESET}  ${color}${(EVENT_LABELS[e.event_type] || e.event_type).padEnd(22)}${RESET}${detail}`,
    );
  }
}

// ── Watch mode ──────────────────────────────────────────────────────────────

interface ActiveIssue {
  id: string;
  identifier: string;
  title: string;
  stateName: string;
  lastEvent: string;
  lastEventTime: string;
  prNumber: string | null;
  statusSummary: string | null;
  reviseCount: number;
  events: PipelineEvent[];
  /** ISO timestamp at which this issue entered its current stage; null if unknown. */
  stageEntryTime: string | null;
}

interface QueuedIssue {
  id: string;
  identifier: string;
  title: string;
  stateName: string;
  createdAt: string;
}

type SelectableIssue = ActiveIssue | QueuedIssue;

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
  events: PipelineEvent[];
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
    events,
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
        id: row.issue_id,
        identifier: row.identifier,
        title: row.title,
        stateName: row.state_name,
        createdAt: row.created_at,
      });
      continue;
    }

    const enriched = enrichFromDb(row.issue_id, row.updated_at);
    let stageEntryTime = getStageEntryTime(row.issue_id, row.state_name);
    if (stageEntryTime === null && row.state_name === 'Ready for Agent') {
      // Fall back to cache updated_at timestamp
      stageEntryTime = row.updated_at;
    }
    active.push({
      id: row.issue_id,
      identifier: row.identifier,
      title: row.title,
      stateName: row.state_name,
      ...enriched,
      stageEntryTime,
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

interface RenderOptions {
  error?: string;
  currentRefreshMs?: number;
  warning?: string;
  selectedIndex?: number;
  isPaused?: boolean;
  confirmPrompt?: string;
  cmdLine?: string;
  lastResult?: { message: string; ok: boolean };
  todayStats?: TodayStats;
  gateRevisions?: GateRevisionCounts;
}

export function formatWhyReason(issue: {
  stateName: string;
  events: PipelineEvent[];
}): string {
  const last =
    issue.events.length > 0 ? issue.events[issue.events.length - 1] : null;
  if (!last) {
    if (issue.stateName === 'Ready for Agent') return 'Waiting';
    if (issue.stateName === 'Agent Working') return 'Agent working';
    if (issue.stateName === 'In Review') return 'In review';
    return 'In progress';
  }
  switch (last.event_type) {
    case 'gate_error':
      return 'Gate error';
    case 'gate_revise':
      return 'Review needed';
    case 'gate_cooldown':
      return 'Cooldown';
    case 'automerge_pending':
      return 'CI pending';
    case 'agent_started':
    case 'agent_dispatched':
      return 'Agent working';
    case 'pr_created':
    case 'agent_completed':
      return 'Gate pending';
    case 'state_changed':
      return 'State changed';
    case 'gate_blocked':
      return 'Blocked';
    default:
      break;
  }
  if (issue.stateName === 'Ready for Agent') return 'Waiting';
  if (issue.stateName === 'Agent Working') return 'Agent working';
  if (issue.stateName === 'In Review') return 'In review';
  return 'In progress';
}

/**
 * Truncates the lines array to fit within `viewportHeight` rows.
 * Preserves the first `headerCount` lines and the last `footerCount` lines.
 * Inserts a single truncation indicator line when content is clipped.
 */
export function capViewport(
  lines: string[],
  viewportHeight: number,
  headerCount: number,
  footerCount: number,
): string[] {
  const total = lines.length;
  // +1 accounts for the truncation indicator line we'll insert
  if (total <= viewportHeight) return lines;

  const available = viewportHeight - headerCount - footerCount - 1; // -1 for indicator
  if (available <= 0) {
    // Viewport is so tiny we can't show any content rows — just header + indicator + footer
    const indicator = `  ${DIM}... (resize terminal to see issues)${RESET}`;
    return [
      ...lines.slice(0, headerCount),
      indicator,
      ...lines.slice(total - footerCount),
    ];
  }

  const hidden = total - headerCount - footerCount - available;
  const indicator = `  ${DIM}... +${hidden} more issues (resize terminal to see all)${RESET}`;
  return [
    ...lines.slice(0, headerCount + available),
    indicator,
    ...lines.slice(total - footerCount),
  ];
}

export function renderDashboardOutput(
  active: ActiveIssue[],
  queued: QueuedIssue[],
  recent: RecentIssue[],
  stageMedians: Map<string, { medianMs: number; sampleSize: number }>,
  opts: RenderOptions = {},
): string {
  const now = new Date().toLocaleTimeString('en-GB', { hour12: false });
  const cols = process.stdout.columns || 100;
  const { titleWidth, separatorWidth, showWhy, showStageBar } =
    computeColumnWidths(cols);
  const lines: string[] = [];

  const stuckCount = active.filter(
    (i) => elapsedMs(i.lastEventTime) > STUCK_THRESHOLD_MS,
  ).length;
  const failCount = recent.filter((i) => FAILED_TYPES.has(i.eventType)).length;

  const statsParts: string[] = [`ACTIVE ${active.length}`];
  if (stuckCount > 0) statsParts.push(`${YELLOW}STUCK ${stuckCount}${RESET}`);
  if (failCount > 0) statsParts.push(`${RED}FAIL ${failCount}${RESET}`);
  const statsBar = statsParts.join(` ${DIM}·${RESET} `);

  const effectiveRefreshMs = opts.currentRefreshMs ?? REFRESH_MS;
  const refreshSec = effectiveRefreshMs / 1000;
  const pauseLabel = opts.isPaused ? `${YELLOW}[paused]${RESET}` : '';
  const refreshLabel =
    effectiveRefreshMs > REFRESH_MS
      ? `${YELLOW}[${refreshSec}s rate-limited]${RESET}`
      : `${DIM}[every ${refreshSec}s]${RESET}`;
  lines.push(
    `${BOLD} Deus Pipeline${RESET}    ${statsBar}${DIM}${' '.repeat(Math.max(1, cols - 70))}${now}  ${RESET}${pauseLabel || refreshLabel}`,
  );
  lines.push(`${DIM} ${'─'.repeat(separatorWidth)}${RESET}`);

  if (opts.error) {
    lines.push(`${RED} ⚠ ${opts.error}${RESET}`);
    lines.push('');
  }

  if (opts.warning) {
    lines.push(`${YELLOW} ⚠ ${opts.warning}${RESET}`);
  }

  lines.push('');
  const whyHeader = showWhy ? ` ${'WHY'.padEnd(16)}` : '';
  const stageHeader = showStageBar ? '     STAGE' : '';
  lines.push(
    `${DIM}  ${'ID'.padEnd(8)}  ${'TITLE'.padEnd(titleWidth)} ${'PR'.padStart(6)} ${'STATUS'.padEnd(22)}${whyHeader} ${'AGE'.padStart(4)} ${'ETA'.padEnd(5)}${stageHeader}${RESET}`,
  );
  // Snapshot how many header lines we've built so far (everything above the data rows)
  const headerLineCount = lines.length;

  let rowIndex = 0;
  const sel = opts.selectedIndex ?? -1;

  if (active.length === 0) {
    lines.push(`${DIM}  No issues in pipeline.${RESET}`);
  } else {
    for (const issue of active) {
      const ms = elapsedMs(issue.lastEventTime);
      const isStuck = ms > STUCK_THRESHOLD_MS;
      const cursor =
        rowIndex === sel
          ? `${CYAN}▸${RESET}`
          : isStuck
            ? `${RED}!${RESET}`
            : ' ';
      const id = issue.identifier.padEnd(8);
      const sg = stateGlyph(issue.stateName);
      const glyph = `${sg.color}${sg.glyph}${RESET}`;
      const title = truncate(issue.title, titleWidth).padEnd(titleWidth);
      const pr = issue.prNumber
        ? `${DIM}${issue.prNumber.padStart(6)}${RESET}`
        : `${DIM}${'—'.padStart(6)}${RESET}`;
      const lastRawEventType =
        issue.events.length > 0
          ? issue.events[issue.events.length - 1].event_type
          : null;
      const isStale =
        lastRawEventType !== null &&
        !TERMINAL_TYPES.has(lastRawEventType) &&
        elapsedMs(issue.lastEventTime) > 3 * 60_000;
      let statusText = issue.statusSummary ?? issue.lastEvent;
      statusText = statusText
        .replace(GATE_PREFIX_RE, 'Gate: ')
        .replace(AGENT_PREFIX_RE, 'Agent: ');
      if (isStale) statusText = `[stale] ${statusText}`;
      const status = truncate(statusText, 22).padEnd(22);
      const why = truncate(formatWhyReason(issue), 16).padEnd(16);

      const elapsedStr = formatElapsed(issue.lastEventTime).padStart(4);
      let elapsedColored: string;
      if (ms > STUCK_THRESHOLD_MS) {
        elapsedColored = `${RED}${elapsedStr} ⚠${RESET}`;
      } else if (ms > WARN_THRESHOLD_MS) {
        elapsedColored = `${YELLOW}${elapsedStr}${RESET}`;
      } else {
        elapsedColored = `${DIM}${elapsedStr}${RESET}`;
      }

      const revise =
        issue.reviseCount > 0 ? ` ${RED}×${issue.reviseCount}${RESET}` : '';

      const whyCol = showWhy ? ` ${DIM}${why}${RESET}` : '';
      const stageCol = showStageBar ? ` ${buildStageBar(issue.events)}` : '';

      const stageInfo = stageMedians.get(issue.stateName);
      const etaRaw = computeETADisplay(
        issue.stageEntryTime,
        stageInfo?.medianMs,
        stageInfo?.sampleSize ?? 0,
      );
      // Pad the ETA field to exactly 5 visible characters
      const etaPadded =
        etaRaw + ' '.repeat(Math.max(0, 5 - stripAnsi(etaRaw).length));

      lines.push(
        `${cursor} ${CYAN}${id}${RESET}${glyph} ${title} ${pr} ${DIM}${status}${RESET}${whyCol} ${elapsedColored} ${etaPadded}${revise}${stageCol}`,
      );
      rowIndex++;
    }
  }

  if (queued.length > 0) {
    lines.push('');
    lines.push(`${DIM} QUEUED (${queued.length})${RESET}`);
    for (const issue of queued) {
      const cursor = rowIndex === sel ? `${CYAN}▸${RESET}` : ' ';
      const id = issue.identifier.padEnd(8);
      const sg = stateGlyph(issue.stateName);
      const glyph = `${sg.color}${sg.glyph}${RESET}`;
      const title = truncate(issue.title, titleWidth).padEnd(titleWidth);
      const elapsed = formatElapsed(issue.createdAt).padStart(4);
      const qPr = `${DIM}${'—'.padStart(6)}${RESET}`;
      const qStatus = `${DIM}${'—'.padEnd(22)}${RESET}`;
      lines.push(
        `${cursor} ${DIM}${id}${RESET}${glyph} ${DIM}${title}${RESET} ${qPr} ${qStatus} ${DIM}${elapsed}${RESET}`,
      );
      rowIndex++;
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

  if (opts.lastResult) {
    const rc = opts.lastResult.ok ? GREEN : RED;
    const icon = opts.lastResult.ok ? '✓' : '✗';
    lines.push(`${rc} ${icon} ${opts.lastResult.message}${RESET}`);
  }

  if (opts.todayStats) {
    lines.push('');
    const footerLines = renderThroughputFooter(
      opts.todayStats,
      opts.gateRevisions ?? {},
      cols,
    );
    for (const l of footerLines) lines.push(l);
  }

  lines.push('');
  if (opts.confirmPrompt) {
    lines.push(`${YELLOW} ${opts.confirmPrompt} [y/N]${RESET}`);
  } else if (opts.cmdLine !== undefined) {
    lines.push(`${CYAN} :${opts.cmdLine}█${RESET}`);
  } else {
    lines.push(
      `${DIM} ↑↓ select · → detail · o open · r re-eval · l skip · s start · : cmd (:move/:poll/:start) · Ctrl+R refresh · Ctrl+C exit${RESET}`,
    );
  }

  // Footer is always the last 2 lines: empty separator + hint/status
  const footerLineCount = 2;
  const viewportHeight = process.stdout.rows ?? 40;
  const capped = capViewport(
    lines,
    viewportHeight,
    headerLineCount,
    footerLineCount,
  );
  return capped.join('\n');
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

  let selectedIndex = 0;
  let allIssues: SelectableIssue[] = [];
  let paused = false;
  let lastActionResult: string | undefined;
  let lastActionOk = true;
  let confirmPending: (() => Promise<void>) | null = null;
  let confirmLabel = '';
  let cmdMode = false;
  let cmdBuffer = '';
  let cmdCompletions: string[] = [];
  let cmdCompletionIdx = -1;
  let actionCtx: ActionContext | null = null;
  let viewMode: 'list' | 'detail' = 'list';
  interface DetailData {
    issue: SelectableIssue;
    events: Array<{
      event_type: string;
      detail: string | null;
      created_at: string;
    }>;
    prUrl: string | null;
    labelNames: string[];
    url: string | null;
  }
  let detailData: DetailData | null = null;

  let cachedActive: ActiveIssue[] = [];
  let cachedQueued: QueuedIssue[] = [];
  let cachedRecent: RecentIssue[] = [];
  let cachedStageMedians: Map<
    string,
    { medianMs: number; sampleSize: number }
  > = new Map();
  let lastWarning: string | undefined;
  let cachedTodayStats: TodayStats | undefined;
  let cachedGateRevisions: GateRevisionCounts | undefined;

  try {
    actionCtx = await initActionContext(client, teamId);
  } catch {
    lastActionResult = 'Actions unavailable';
    lastActionOk = false;
  }

  function isRateLimitError(msg: string): boolean {
    return /rate.?limit/i.test(msg) || /429/.test(msg) || /too many/i.test(msg);
  }

  function renderDetailView(data: DetailData): string {
    const cols = process.stdout.columns || 100;
    const { separatorWidth } = computeColumnWidths(cols);
    const lines: string[] = [];
    const sg = stateGlyph(data.issue.stateName);

    lines.push(
      `${BOLD} ${data.issue.identifier}${RESET}  ${sg.color}${sg.glyph} ${data.issue.stateName}${RESET}`,
    );
    lines.push(`${DIM} ${'─'.repeat(separatorWidth)}${RESET}`);
    lines.push(`  ${data.issue.title}`);
    lines.push('');

    if (data.prUrl) {
      lines.push(`  ${DIM}PR:${RESET} ${CYAN}${data.prUrl}${RESET}`);
    }
    if (data.url) {
      lines.push(`  ${DIM}URL:${RESET} ${CYAN}${data.url}${RESET}`);
    }
    if (data.labelNames.length > 0) {
      lines.push(
        `  ${DIM}Labels:${RESET} ${data.labelNames.map((l) => `${YELLOW}${l}${RESET}`).join(' ')}`,
      );
    }

    lines.push('');
    lines.push(`${BOLD} Timeline${RESET}`);
    lines.push(`${DIM} ${'─'.repeat(separatorWidth)}${RESET}`);

    if (data.events.length === 0) {
      lines.push(`${DIM}  No pipeline events.${RESET}`);
    } else {
      const visible = data.events.slice(-20);
      if (data.events.length > 20) {
        lines.push(
          `${DIM}  ...${data.events.length - 20} earlier events omitted${RESET}`,
        );
      }
      for (const e of visible) {
        const time = formatLocalDateTime(e.created_at);
        const color = colorFor(e.event_type);
        const label = EVENT_LABELS[e.event_type] || e.event_type;
        const detailWidth = Math.max(10, cols - 46);
        const detail = e.detail
          ? ` ${DIM}— ${truncate(e.detail, detailWidth)}${RESET}`
          : '';
        lines.push(
          `  ${DIM}${time}${RESET}  ${color}${label.padEnd(22)}${RESET}${detail}`,
        );
      }
    }

    if (data.events.length > 0) {
      const bar = buildStageBar(data.events);
      lines.push('');
      lines.push(`${BOLD} Stage${RESET}   ${bar}`);
      lines.push(
        `${DIM}         Scope Dispatch PR    QGate CmpGate Merge${RESET}`,
      );
    }

    lines.push('');
    lines.push(`${BOLD} Actions${RESET}`);
    lines.push(`${DIM} ${'─'.repeat(separatorWidth)}${RESET}`);
    lines.push(`  ${CYAN}o${RESET}  Open in browser`);
    lines.push(`  ${CYAN}r${RESET}  Re-run gate`);
    lines.push(`  ${CYAN}l${RESET}  Toggle warden:skip`);
    lines.push(`  ${CYAN}s${RESET}  Start orchestration (Backlog/Todo only)`);
    lines.push(
      `  ${CYAN}:${RESET}  Command mode (:move <state> · :poll <s> · :start)`,
    );

    if (lastActionResult) {
      lines.push('');
      const rc = lastActionOk ? GREEN : RED;
      const icon = lastActionOk ? '✓' : '✗';
      lines.push(`${rc} ${icon} ${lastActionResult}${RESET}`);
    }

    lines.push('');
    if (confirmPending) {
      lines.push(`${YELLOW} ${confirmLabel} [y/N]${RESET}`);
    } else if (cmdMode) {
      lines.push(`${CYAN} :${cmdBuffer}█${RESET}`);
    } else {
      lines.push(
        `${DIM} ← back · o open · r re-eval · l skip · s start · : cmd · Ctrl+C exit${RESET}`,
      );
    }

    return lines.join('\n');
  }

  function rerender(): void {
    let output: string;
    if (viewMode === 'detail' && detailData) {
      output = renderDetailView(detailData);
    } else {
      output = renderDashboardOutput(
        cachedActive,
        cachedQueued,
        cachedRecent,
        cachedStageMedians,
        {
          error: lastError,
          currentRefreshMs: nextRefreshMs,
          warning: lastWarning,
          selectedIndex,
          isPaused: paused,
          confirmPrompt: confirmPending ? confirmLabel : undefined,
          cmdLine: cmdMode ? cmdBuffer : undefined,
          lastResult: lastActionResult
            ? { message: lastActionResult, ok: lastActionOk }
            : undefined,
          todayStats: cachedTodayStats,
          gateRevisions: cachedGateRevisions,
        },
      );
    }
    process.stdout.write(CURSOR_HOME + CLEAR_TO_END + output + '\n');
  }

  async function tick(): Promise<void> {
    if (rendering) return;
    rendering = true;

    // Footer stats are DB-only — compute before the API call so they're
    // available in both the success and error render paths.
    cachedTodayStats = computeTodayStats();
    cachedGateRevisions = computeGateRevisions();

    try {
      const { active, queued, fromCache } = await fetchActiveAndQueued(
        client,
        teamId,
      );
      const recent = fetchRecentIssues();
      cachedActive = active;
      cachedQueued = queued;
      cachedRecent = recent;
      cachedStageMedians = computeStageMedians();
      allIssues = [...active, ...queued];
      selectedIndex = Math.min(
        selectedIndex,
        Math.max(0, allIssues.length - 1),
      );
      lastError = undefined;
      lastWarning = fromCache ? undefined : 'cache stale — polling API';
      nextRefreshMs = fromCache ? DISPLAY_REFRESH_MS : REFRESH_MS;
      rerender();
    } catch (err) {
      lastError = err instanceof Error ? err.message : 'Unknown error';
      if (isRateLimitError(lastError)) {
        nextRefreshMs = BACKOFF_MS;
      } else {
        nextRefreshMs = REFRESH_MS;
      }
      cachedRecent = fetchRecentIssues();
      rerender();
    } finally {
      rendering = false;
      if (!paused) {
        timer = setTimeout(tick, nextRefreshMs);
      }
    }
  }

  function resumeRefresh(): void {
    paused = false;
    confirmPending = null;
    confirmLabel = '';
    cmdMode = false;
    cmdBuffer = '';
    cmdCompletions = [];
    cmdCompletionIdx = -1;
    timer = setTimeout(tick, 0);
  }

  async function fetchLabelsForIssue(issueId: string): Promise<string[]> {
    try {
      const issue = await client.issue(issueId);
      const labels = await issue.labels();
      return labels.nodes.map((l) => l.id);
    } catch {
      return [];
    }
  }

  async function enterDetailView(): Promise<void> {
    const issue = allIssues[selectedIndex];
    if (!issue) return;

    paused = true;
    if (timer) {
      clearTimeout(timer);
      timer = undefined;
    }

    const events = getPipelineEvents({ issueId: issue.id });
    const pr = getIssuePr(issue.id);

    let labelNames: string[] = [];
    let url: string | null = null;
    try {
      const fetched = await client.issue(issue.id);
      url = fetched.url;
      const labels = await fetched.labels();
      labelNames = labels.nodes.map((l) => l.name);
    } catch {
      /* best-effort */
    }

    detailData = {
      issue,
      events,
      prUrl: pr?.pr_url ?? null,
      labelNames,
      url,
    };
    viewMode = 'detail';
    rerender();
  }

  function exitDetailView(): void {
    viewMode = 'list';
    detailData = null;
    resumeRefresh();
    rerender();
  }

  async function handleOpen(): Promise<void> {
    const issue = allIssues[selectedIndex];
    if (!issue) return;

    const cachedUrl = viewMode === 'detail' && detailData?.url;
    if (cachedUrl) {
      const result = handleOpenInBrowser(cachedUrl, issue.identifier);
      lastActionResult = result.message;
      lastActionOk = result.ok;
      rerender();
      return;
    }

    try {
      const fetched = await client.issue(issue.id);
      const result = handleOpenInBrowser(fetched.url, issue.identifier);
      lastActionResult = result.message;
      lastActionOk = result.ok;
    } catch {
      lastActionResult = `Failed to fetch URL for ${issue.identifier}`;
      lastActionOk = false;
    }
    rerender();
  }

  async function handleRerun(): Promise<void> {
    const issue = allIssues[selectedIndex];
    if (!issue || !actionCtx) return;
    const stateName = 'stateName' in issue ? issue.stateName : '';
    confirmLabel = `Re-run gate on ${issue.identifier}?`;
    confirmPending = async () => {
      const result = await triggerGateRerun(
        actionCtx!,
        issue.id,
        issue.identifier,
        stateName,
      );
      lastActionResult = result.message;
      lastActionOk = result.ok;
    };
    paused = true;
    if (timer) {
      clearTimeout(timer);
      timer = undefined;
    }
    rerender();
  }

  async function handleToggleSkip(): Promise<void> {
    const issue = allIssues[selectedIndex];
    if (!issue || !actionCtx) return;
    confirmLabel = `Toggle warden:skip on ${issue.identifier}?`;
    confirmPending = async () => {
      const labelIds = await fetchLabelsForIssue(issue.id);
      const result = await toggleWardenSkip(
        actionCtx!,
        issue.id,
        issue.identifier,
        labelIds,
      );
      lastActionResult = result.message;
      lastActionOk = result.ok;
    };
    paused = true;
    if (timer) {
      clearTimeout(timer);
      timer = undefined;
    }
    rerender();
  }

  async function handleStart(): Promise<void> {
    const issue = allIssues[selectedIndex];
    if (!issue || !actionCtx) return;
    if (!STARTABLE_STATES.has(issue.stateName)) {
      lastActionResult = `Can only start Backlog/Todo issues (current: ${issue.stateName})`;
      lastActionOk = false;
      rerender();
      return;
    }
    confirmLabel = `Start orchestration for ${issue.identifier}?`;
    confirmPending = async () => {
      const result = await startIssueOrchestration(
        actionCtx!,
        issue.id,
        issue.identifier,
        issue.stateName,
      );
      lastActionResult = result.message;
      lastActionOk = result.ok;
    };
    paused = true;
    if (timer) {
      clearTimeout(timer);
      timer = undefined;
    }
    rerender();
  }

  async function dispatchCommand(input: string): Promise<void> {
    const trimmed = input.trim();

    const pollMatch = trimmed.match(/^poll\s+(\d+)$/i);
    if (pollMatch) {
      const seconds = parseInt(pollMatch[1], 10);
      if (isNaN(seconds) || seconds < 5 || seconds > 300) {
        lastActionResult = 'Poll interval must be 5-300 seconds';
        lastActionOk = false;
        return;
      }
      const ok = setPollInterval(seconds * 1000);
      lastActionResult = ok
        ? `Poll interval → ${seconds}s`
        : 'Dispatcher not running';
      lastActionOk = ok;
      return;
    }

    if (trimmed.toLowerCase() === 'start') {
      await handleStart();
      return;
    }

    const moveMatch = trimmed.match(/^move\s+(.+)$/i);
    if (moveMatch) {
      const targetState = moveMatch[1].replace(/^["']|["']$/g, '');
      const issue = allIssues[selectedIndex];
      if (!issue || !actionCtx) return;
      const result = await moveIssueState(
        actionCtx,
        issue.id,
        issue.identifier,
        targetState,
      );
      lastActionResult = result.message;
      lastActionOk = result.ok;
      return;
    }

    lastActionResult = `Unknown command: ${trimmed}`;
    lastActionOk = false;
  }

  const resyncTimerRef = setInterval(() => {
    seedOrResyncCache(client, teamId).catch((err) => {
      console.error('issue-cache: background resync failed', err);
    });
  }, RESYNC_INTERVAL_MS);

  const cleanup = () => {
    if (timer) clearTimeout(timer);
    clearInterval(resyncTimerRef);
    if (process.stdin.isTTY) {
      try {
        process.stdin.setRawMode(false);
        process.stdin.pause();
      } catch {
        /* ignore */
      }
    }
    process.stdout.write(EXIT_ALT_SCREEN + SHOW_CURSOR);
    process.exit(0);
  };

  process.on('SIGINT', cleanup);
  process.on('SIGTERM', cleanup);
  process.on('uncaughtException', (err) => {
    if (process.stdin.isTTY) {
      try {
        process.stdin.setRawMode(false);
      } catch {
        /* ignore */
      }
    }
    process.stdout.write(EXIT_ALT_SCREEN + SHOW_CURSOR);
    console.error('Uncaught exception:', err);
    process.exit(1);
  });
  process.on('SIGWINCH', () => {
    if (!rendering) {
      if (paused) {
        rerender();
      } else {
        tick().catch(() => {});
      }
    }
  });

  if (process.stdin.isTTY) {
    process.stdin.setRawMode(true);
    process.stdin.resume();
    process.stdin.setEncoding('utf8');

    process.stdin.on('data', async (key: string) => {
      if (key === '') {
        cleanup();
        return;
      }

      if (confirmPending) {
        if (key === 'y' || key === 'Y') {
          const fn = confirmPending;
          confirmPending = null;
          confirmLabel = '';
          await fn();
          resumeRefresh();
        } else {
          resumeRefresh();
        }
        rerender();
        return;
      }

      if (cmdMode) {
        if (key === '\r' || key === '\n') {
          await dispatchCommand(cmdBuffer);
          cmdMode = false;
          cmdBuffer = '';
          cmdCompletions = [];
          cmdCompletionIdx = -1;
          resumeRefresh();
          rerender();
        } else if (key === '\t') {
          const movePrefix = cmdBuffer.match(/^move\s+(.*)/i);
          if (movePrefix && actionCtx) {
            const partial = movePrefix[1].toLowerCase();
            if (cmdCompletionIdx < 0 || cmdCompletions.length === 0) {
              cmdCompletions = [...actionCtx.stateByName.keys()].filter((n) =>
                n.toLowerCase().startsWith(partial),
              );
              cmdCompletionIdx = 0;
            } else {
              cmdCompletionIdx = (cmdCompletionIdx + 1) % cmdCompletions.length;
            }
            if (cmdCompletions.length > 0) {
              cmdBuffer = `move ${cmdCompletions[cmdCompletionIdx]}`;
            }
          }
          rerender();
        } else if (key === '') {
          cmdBuffer = cmdBuffer.slice(0, -1);
          cmdCompletions = [];
          cmdCompletionIdx = -1;
          rerender();
        } else if (key === '' && key.length === 1) {
          cmdCompletions = [];
          cmdCompletionIdx = -1;
          resumeRefresh();
          rerender();
        } else if (key.length === 1 && key >= ' ') {
          cmdBuffer += key;
          cmdCompletions = [];
          cmdCompletionIdx = -1;
          rerender();
        }
        return;
      }

      if (viewMode === 'detail') {
        if (key === '[D' || (key === '' && key.length === 1)) {
          exitDetailView();
          return;
        }
        if (key === 'o') {
          await handleOpen();
          return;
        }
        if (key === 'r') {
          await handleRerun();
          return;
        }
        if (key === 'l') {
          await handleToggleSkip();
          return;
        }
        if (key === 's') {
          await handleStart();
          return;
        }
        if (key === ':') {
          cmdMode = true;
          cmdBuffer = '';
          rerender();
          return;
        }
        return;
      }

      switch (key) {
        case '[A':
        case 'k':
          selectedIndex = Math.max(0, selectedIndex - 1);
          rerender();
          break;
        case '[B':
        case 'j':
          selectedIndex = Math.min(allIssues.length - 1, selectedIndex + 1);
          rerender();
          break;
        case '[C':
        case '\r':
          await enterDetailView();
          break;
        case 'o':
          await handleOpen();
          break;
        case 'r':
          await handleRerun();
          break;
        case 'l':
          await handleToggleSkip();
          break;
        case 's':
          await handleStart();
          break;
        case ':':
          cmdMode = true;
          cmdBuffer = '';
          paused = true;
          if (timer) {
            clearTimeout(timer);
            timer = undefined;
          }
          rerender();
          break;
        case '\x12': // Ctrl+R — immediate refresh
          tick().catch(() => {});
          break;
        case '':
          if (key.length === 1) {
            rerender();
          }
          break;
      }
    });
  }

  await tick();
}

// ── Health check ─────────────────────────────────────────────────────────────

interface HealthReadyResponse {
  status: 'ok' | 'degraded' | 'stalled';
  dispatcher: {
    lastTickAt: number | null;
    lagMs: number | null;
    pollMs: number;
    inFlightCount: number;
  };
  webhook: { lastIngestAt: string | null; recentLagMs: number | null };
  inFlight: Array<{
    subsystem: string;
    startedAt: string;
    elapsedMs: number;
  }>;
  gates: string[];
  reasons: string[];
}

function prettyPrintHealth(data: HealthReadyResponse): void {
  const statusColor =
    data.status === 'ok'
      ? '\x1b[32m'
      : data.status === 'degraded'
        ? '\x1b[33m'
        : '\x1b[31m';
  const reset = '\x1b[0m';

  console.log(`${statusColor}Pipeline: ${data.status.toUpperCase()}${reset}`);
  console.log();

  const lagStr =
    data.dispatcher.lagMs != null
      ? `${Math.round(data.dispatcher.lagMs / 1000)}s`
      : 'n/a';
  console.log(
    `  Dispatcher   lag=${lagStr}  poll=${data.dispatcher.pollMs / 1000}s  in-flight=${data.dispatcher.inFlightCount}`,
  );

  const webhookLag =
    data.webhook.recentLagMs != null
      ? `${Math.round(data.webhook.recentLagMs)}ms`
      : 'n/a';
  console.log(`  Webhook      lag=${webhookLag}`);

  if (data.inFlight.length > 0) {
    console.log(`  In-flight:`);
    for (const f of data.inFlight) {
      const mins = Math.round(f.elapsedMs / 60_000);
      console.log(`    ${f.subsystem}  ${mins}min`);
    }
  }

  console.log(`  Gates:       ${data.gates.join(', ')}`);

  if (data.reasons.length > 0) {
    console.log();
    console.log(`  ${statusColor}Reasons:${reset}`);
    for (const r of data.reasons) {
      console.log(`    - ${r}`);
    }
  }
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
    console.log(
      '  deus pipeline --stuck                Stuck issues (elapsed > 2h)',
    );
    console.log('  deus pipeline --active               In-flight issues');
    console.log('  deus pipeline --all [--since Xh]     All events');
    console.log('  deus pipeline --health [--json]      Pipeline health check');
    process.exit(0);
  }

  if (args[0] === '--health') {
    const port = process.env.LINEAR_WEBHOOK_PORT || '3005';
    fetch(`http://localhost:${port}/health/ready`)
      .then(async (res) => {
        const data = await res.json();
        if (args.includes('--json')) {
          console.log(JSON.stringify(data, null, 2));
        } else {
          prettyPrintHealth(data as HealthReadyResponse);
        }
        process.exit(res.status === 200 ? 0 : 1);
      })
      .catch(() => {
        console.error(`Could not reach webhook server at localhost:${port}`);
        process.exit(1);
      });
    return;
  }

  initDatabase();

  const filter: PipelineEventFilter = {};
  let showFailed = false;
  let showStuck = false;
  let showActive = false;

  let i = 0;
  while (i < args.length) {
    const arg = args[i];
    if (arg === '--failed') {
      showFailed = true;
    } else if (arg === '--stuck') {
      showStuck = true;
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

  if (showStuck) {
    const issueLatestTime = new Map<string, string>();
    for (const e of events) {
      issueLatestTime.set(e.issue_id, e.created_at);
    }
    const stuckIds = new Set<string>();
    for (const [id, createdAt] of issueLatestTime) {
      if (elapsedMs(createdAt) > STUCK_THRESHOLD_MS) stuckIds.add(id);
    }
    events = events.filter((e) => stuckIds.has(e.issue_id));
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
