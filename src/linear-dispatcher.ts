import { execFile, spawn } from 'child_process';
import { minimatch } from 'minimatch';
import { createHash } from 'node:crypto';
import fs from 'fs';
import path from 'path';
import { promisify } from 'util';
import { LinearClient } from '@linear/sdk';
import { DATA_DIR, HOME_DIR, GROUPS_DIR } from './config.js';
import { parse as parseYaml } from 'yaml';
import { logger } from './logger.js';
import { PROJECT_ROOT } from './config.js';
import { getProjectByPath, registerProject } from './project-registry.js';
import { FatalError, RetryableError } from './errors/index.js';
import { fireAndForget, withTimeout } from './async/index.js';
import {
  IS_MACOS,
  IS_LINUX,
  forceKillProcessGroup,
  PYTHON_BIN,
} from './platform.js';
import { extractPrUrl } from './pr-url-extractor.js';
import { queryPrState, checkConflictingPrs } from './linear-auto-merge.js';
import {
  CIRCUIT_BREAKER_THRESHOLD,
  getConsecutiveFailCount,
  getIssuePr,
  getLastFailTime,
  getPipelineEvents,
  logPipelineEvent,
  updatePrAutoMergeState,
  upsertIssuePr,
} from './db.js';
import { notifyPipelineStep } from './linear-notifications.js';
import { resolveVaultPath } from './solutions/store.js';
import { defaultSession } from './agent-runtimes/types.js';
import { getBus } from './events/bus.js';
import type {
  RunContext,
  RuntimeEventSink,
  RuntimeSession,
} from './agent-runtimes/types.js';
import type { RuntimeRegistry } from './agent-runtimes/registry.js';
import type { GroupQueue } from './group-queue.js';
import type { RegisteredGroup } from './types.js';
import type { EventBus } from './events/bus.js';

const execFileAsync = promisify(execFile);

const DEFAULT_POLL_MS = 30_000;
// Deadline ceiling for a single Linear poll fetch (env-overridable). The effective
// per-poll timeout (_fetchTimeoutMs) is derived from the active poll interval so it
// is always < the interval — a hung fetch can never overlap the next tick.
const LINEAR_FETCH_TIMEOUT_MS =
  Number(process.env.LINEAR_FETCH_TIMEOUT_MS) || 15_000;
// Effective per-poll fetch deadline. Recomputed by startLinearDispatcher and
// setPollInterval (which co-manage _timer) so it tracks the live poll interval.
let _fetchTimeoutMs = LINEAR_FETCH_TIMEOUT_MS;
// Effective per-poll deadline. Callers (startLinearDispatcher, setPollInterval) only
// pass pollMs >= 1000ms, so the 500ms floor is never reached and the result is always
// strictly < pollMs — a hung fetch settles before the next tick fires.
export const deriveFetchTimeout = (pollMs: number): number =>
  Math.max(500, Math.min(LINEAR_FETCH_TIMEOUT_MS, Math.floor(pollMs * 0.9)));
const DISPATCH_GROUP_JID = 'linear-dispatch';
const BACKOFF_FIRST_MS = 5 * 60_000;
const BACKOFF_REPEAT_MS = 10 * 60_000;
const AGENTS_DIR = path.join(PROJECT_ROOT, '.claude', 'agents');
const GIT_TIMEOUT_MS = 30_000;
const BUILD_TIMEOUT_MS = 120_000;
const PUSH_TIMEOUT_MS = 120_000;

// Linear identifier format: alpha-led alphanumeric team prefix + numeric suffix (e.g. LIA-115).
const LINEAR_ID_RE = /^[A-Z][A-Z0-9]+-[0-9]+$/;
export function validateLinearIdentifier(id: string): void {
  if (!LINEAR_ID_RE.test(id)) {
    throw new Error(`Invalid Linear identifier: "${id}"`);
  }
}

// Trailing '/' = prefix match; exact names match literally (prevents .env matching .envrc)
function matchesPath(file: string, entry: string): boolean {
  if (entry.endsWith('/')) {
    return file.startsWith(entry);
  }
  return file === entry;
}

const HARD_BLOCKED_PATHS_DEFAULT = [
  '.claude/',
  'CLAUDE.md',
  'AGENTS.md',
  '.env',
  '.mex/',
  '.github/workflows/',
];
// Intentionally not env-configurable — warn-only paths rarely change and misconfiguration risks silent bypass
const WARN_ONLY_PATHS = ['package.json', 'tsconfig.json'];

export interface LinearDispatcherDependencies {
  registeredGroups: () => Record<string, RegisteredGroup>;
  registerGroup: (jid: string, group: RegisteredGroup) => void;
  registry: RuntimeRegistry;
  queue: GroupQueue;
}

interface RoleSpec {
  label: string;
  name: string;
  model?: string;
  content: string;
  writeAllowlist?: string[];
}

export interface WorkflowState {
  id: string;
  name: string;
}

export interface GateLabels {
  evaluating?: string;
  scoped?: string;
  revise?: string;
  error?: string;
  wardenSkip?: string;
  blocked?: string;
  bouncedUnscoped?: string;
  bouncedStale?: string;
  bouncedNoContext?: string;
  conflict?: string;
  effort: Record<number, string>;
  complexity: Record<number, string>;
}

export interface LinearContext {
  client: LinearClient;
  bus: EventBus; // app-wide event hub; same singleton at emit- and register-side
  stateByName: Map<string, WorkflowState>;
  stateById: Map<string, WorkflowState>;
  botUserId: string;
  viewerId: string; // human operator ID, used to assign issues on In Review
  deps: LinearDispatcherDependencies;
  dispatchGroup: RegisteredGroup;
  inFlightDispatch: Set<string>;
  inFlightGate: Set<string>;
  gateLabels: GateLabels;
  teamId: string;
  repoSlug?: string;
  vaultPath: string | null;
}

let _timer: ReturnType<typeof setInterval> | null = null;
let _tick: (() => void) | null = null;
let _roleSpecs: Map<string, RoleSpec> = new Map();
let _ctx: LinearContext | null = null;
// Promise-chain serializer — same pattern as commentLocks in linear-notifications.ts
let _patchMutex: Promise<void> = Promise.resolve();

export function extractFrontmatter(content: string): {
  data: Record<string, unknown>;
  body: string;
} {
  const match = content.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?([\s\S]*)$/);
  if (!match) return { data: {}, body: content };
  try {
    return {
      data: parseYaml(match[1]) as Record<string, unknown>,
      body: match[2],
    };
  } catch {
    return { data: {}, body: content };
  }
}

export function loadRoleSpecs(agentsDir: string): Map<string, RoleSpec> {
  const specs = new Map<string, RoleSpec>();
  if (!fs.existsSync(agentsDir)) return specs;

  const files = fs.readdirSync(agentsDir).filter((f) => f.endsWith('.md'));
  for (const file of files) {
    const raw = fs.readFileSync(path.join(agentsDir, file), 'utf-8');
    const { data, body } = extractFrontmatter(raw);
    const label =
      typeof data.linear_label === 'string' ? data.linear_label : undefined;
    if (!label) continue;
    const rawAllowlist = data.write_allowlist;
    let writeAllowlist: string[] | undefined;
    if (rawAllowlist !== undefined) {
      if (Array.isArray(rawAllowlist)) {
        writeAllowlist = (rawAllowlist as unknown[]).filter(
          (v): v is string => {
            if (typeof v === 'string') return true;
            logger.warn(
              { file, value: v },
              'linear-dispatcher: loadRoleSpecs: non-string entry in write_allowlist, skipping',
            );
            return false;
          },
        );
      } else {
        logger.warn(
          { file },
          'linear-dispatcher: loadRoleSpecs: write_allowlist is not an array, ignoring',
        );
      }
    }
    specs.set(label, {
      label,
      name: typeof data.name === 'string' ? data.name : file.replace('.md', ''),
      model: typeof data.model === 'string' ? data.model : undefined,
      content: body.trim(),
      writeAllowlist,
    });
  }
  return specs;
}

async function discoverTeamId(client: LinearClient): Promise<string> {
  const teams = await client.teams();
  if (teams.nodes.length === 0) {
    throw new FatalError('No Linear teams found');
  }
  const override = process.env.LINEAR_TEAM_ID;
  if (override) {
    const match = teams.nodes.find((t) => t.id === override);
    if (!match) {
      throw new FatalError(
        `LINEAR_TEAM_ID "${override}" not found among ${teams.nodes.length} teams`,
      );
    }
    return match.id;
  }
  if (teams.nodes.length > 1) {
    logger.warn(
      { count: teams.nodes.length },
      'linear-dispatcher: multiple teams found, using first. Set LINEAR_TEAM_ID to override',
    );
  }
  return teams.nodes[0].id;
}

export async function discoverWorkflowStates(
  client: LinearClient,
  teamId: string,
): Promise<Map<string, WorkflowState>> {
  const states = await client.workflowStates({
    filter: { team: { id: { eq: teamId } } },
  });
  const map = new Map<string, WorkflowState>();
  for (const s of states.nodes) {
    map.set(s.name, { id: s.id, name: s.name });
  }
  const required = ['Ready for Agent', 'Agent Working', 'In Review', 'Backlog'];
  if (!map.has('Done')) {
    logger.warn(
      'linear-dispatcher: workflow state "Done" not found — auto-merge will skip Done transition',
    );
  }
  if (!map.has('Todo')) {
    logger.warn(
      'linear-dispatcher: "Todo" state not found — enrichment gate will not fire',
    );
  }
  if (!map.has('Manual Review Required')) {
    logger.info(
      'linear-dispatcher: "Manual Review Required" state not found — circuit breaker will fall back to Backlog',
    );
  }
  for (const name of required) {
    if (!map.has(name)) {
      throw new FatalError(
        `linear-dispatcher: required workflow state "${name}" not found`,
      );
    }
  }
  logger.debug(
    { states: [...map.keys()] },
    'linear-dispatcher: discovered workflow states',
  );
  return map;
}

export function buildIssuePrompt(
  role: RoleSpec,
  issueTitle: string,
  issueIdentifier: string,
  issueDescription: string | undefined,
  comments: Array<{ author: string; body: string }>,
  failureDossier?: string | null,
): string {
  const parts = [
    `<role>\n${role.content}\n</role>`,
    `<issue>\nTitle: ${issueTitle}\nID: ${issueIdentifier}\n\n${issueDescription ?? '(no description)'}\n</issue>`,
  ];
  const truncated = truncateComments(comments);
  if (truncated.length > 0) {
    const commentBlock = truncated
      .map((c) => `[${c.author}]: ${c.body}`)
      .join('\n\n');
    parts.push(`<comments>\n${commentBlock}\n</comments>`);
  }
  if (failureDossier) {
    parts.push(failureDossier);
  }
  return parts.join('\n\n');
}

export function truncateComments(
  comments: Array<{ author: string; body: string }>,
  maxChars: number = 32000,
): Array<{ author: string; body: string }> {
  if (comments.length === 0) return [];

  const isGateComment = (c: { body: string }) => c.body.includes('**Warden:');

  const keepSet = new Set<number>();
  let regularCount = 0;
  for (let i = comments.length - 1; i >= 0; i--) {
    if (isGateComment(comments[i])) {
      keepSet.add(i);
    } else if (regularCount < 3) {
      keepSet.add(i);
      regularCount++;
    }
  }

  const preservedChars = [...keepSet].reduce(
    (sum, i) => sum + comments[i].author.length + comments[i].body.length,
    0,
  );

  // Determine which older regular comments fit in the remaining budget
  let remainingBudget = maxChars - preservedChars;
  const droppable = comments
    .map((c, i) => ({ idx: i, chars: c.author.length + c.body.length }))
    .filter(({ idx }) => !keepSet.has(idx));

  // Fill from newest droppable toward oldest
  const includedSet = new Set<number>();
  for (let i = droppable.length - 1; i >= 0; i--) {
    if (droppable[i].chars <= remainingBudget) {
      includedSet.add(droppable[i].idx);
      remainingBudget -= droppable[i].chars;
    }
  }

  const omitted = droppable.length - includedSet.size;
  const result: Array<{ author: string; body: string }> = [];
  if (omitted > 0) {
    result.push({
      author: 'System',
      body: `[${omitted} earlier comments omitted]`,
    });
  }

  // Emit kept comments in original chronological order
  for (let i = 0; i < comments.length; i++) {
    if (keepSet.has(i) || includedSet.has(i)) {
      result.push(comments[i]);
    }
  }
  return result;
}

export function extractScopeBlock(
  description: string,
  gateName = 'enrichment-gate',
): string {
  const startMarker = `<!-- gate:${gateName}:start -->`;
  const endMarker = `<!-- gate:${gateName}:end -->`;
  const startIdx = description.indexOf(startMarker);
  const endIdx = description.indexOf(endMarker);
  // Legacy fallback: remove once all issues migrated off agent-readiness-gate sentinels
  if (
    (startIdx === -1 || endIdx === -1) &&
    gateName !== 'agent-readiness-gate'
  ) {
    const legacyStart = '<!-- gate:agent-readiness-gate:start -->';
    const legacyEnd = '<!-- gate:agent-readiness-gate:end -->';
    const ls = description.indexOf(legacyStart);
    const le = description.indexOf(legacyEnd);
    if (ls !== -1 && le !== -1 && le > ls) {
      return description.slice(ls + legacyStart.length, le).trim();
    }
  }
  if (startIdx === -1 || endIdx === -1 || endIdx <= startIdx) {
    return description;
  }
  return description.slice(startIdx + startMarker.length, endIdx).trim();
}

function buildFailureDossier(issueId: string): string | null {
  const pr = getIssuePr(issueId);
  if (!pr || pr.auto_merge_state === 'merged') return null;

  const events = getPipelineEvents({
    issueId,
    eventType: 'automerge_failed',
  });
  const lastFailure = events.at(-1);
  if (!lastFailure) return null;

  const prNumber = pr.pr_url.match(/\/pull\/(\d+)/)?.[1] ?? '?';
  const branch = pr.branch ?? 'unknown';
  const ciDetail = lastFailure.detail ?? 'Unknown failure';

  const lines = [
    '<failure-context>',
    'A previous PR exists and CI failed. Fix the existing PR — do NOT create a new one.',
    `- PR: ${pr.pr_url}`,
    `- Branch: ${branch}`,
    `- CI failure: ${ciDetail}`,
  ];
  if (prNumber !== '?') {
    lines.push(
      `Checkout the branch, run \`gh pr checks ${prNumber}\` to see current CI status, diagnose and fix the failure, then push to the same branch.`,
    );
  } else {
    lines.push(
      'Checkout the branch, diagnose and fix the CI failure, then push to the same branch.',
    );
  }
  lines.push('</failure-context>');
  return lines.join('\n');
}

export function buildScopedIssuePrompt(
  issueTitle: string,
  issueIdentifier: string,
  issueDescription: string,
  comments: Array<{ author: string; body: string }>,
  failureDossier?: string | null,
): string {
  const scopeContent = extractScopeBlock(issueDescription);
  const parts = [
    `<task>\nYou are an autonomous software engineer. Implement the following issue.\nTitle: ${issueTitle}\nID: ${issueIdentifier}\n\n${scopeContent}\n</task>`,
  ];

  const hasReviseHistory = comments.some(
    (c) => c.body.includes('**Warden:') && c.body.includes('REVISE'),
  );
  const hasAgentHistory = comments.some(
    (c) =>
      c.body.includes('Agent run failed') ||
      c.body.includes('**Auto-merged**') ||
      c.body.includes('**Auto-merge failed**') ||
      c.body.includes('**Auto-merge blocked**') ||
      c.body.includes('PR URL in your response'),
  );

  // Scan above runs on full array — truncation may drop older slots containing these signals
  const truncated = truncateComments(comments);
  if (truncated.length > 0) {
    const commentBlock = truncated
      .map((c) => `[${c.author}]: ${c.body}`)
      .join('\n\n');
    parts.push(`<comments>\n${commentBlock}\n</comments>`);
  }

  let contextBlock = '';
  if (hasReviseHistory) {
    contextBlock +=
      'A quality gate REVISE\'d a previous attempt. The gate feedback is in the comments above (look for "**Warden: ... - REVISE**"). Your FIRST priority is to fix the specific issues mentioned in the REVISE feedback. After addressing those, verify remaining acceptance criteria are met. Do not start over from scratch.\n\n';
  }
  // Dossier supersedes generic history hint — more specific instruction wins
  if (hasAgentHistory && !failureDossier) {
    contextBlock +=
      'A previous agent attempt exists. Review the comments above — check what was already committed, pushed, or opened as a PR. Continue from where the last attempt stopped. Do not redo completed steps. If CI failed, check out the existing branch, read the CI logs, fix the failures, and push.\n\n';
  }

  if (failureDossier) {
    parts.push(failureDossier);
  }

  parts.push(
    `<instructions>\n${contextBlock}Read the scope block in the task description. Follow the implementation plan and satisfy all acceptance criteria.\n</instructions>`,
  );
  return parts.join('\n\n');
}

export async function executeAgentRun(
  ctx: LinearContext,
  runContext: RunContext,
): Promise<{ text: string; error: string }> {
  const resolvedBackend = ctx.deps.registry.resolve(ctx.dispatchGroup);
  const backend = resolvedBackend.name();
  const sessionRef: RuntimeSession = defaultSession('', backend);
  let result = '';
  let error = '';

  const eventSink: RuntimeEventSink = (event) => {
    if (event.type === 'output_text') {
      result += event.text;
    }
    if (event.type === 'error') {
      error = event.error;
    }
    // Signal container to exit after first result (one-shot gate runs)
    if (event.type === 'turn_complete') {
      try {
        const inputDir = path.join(
          DATA_DIR,
          'ipc',
          runContext.groupFolder,
          'input',
        );
        fs.mkdirSync(inputDir, { recursive: true });
        fs.writeFileSync(path.join(inputDir, '_close'), '');
      } catch {
        /* best-effort */
      }
    }
  };

  try {
    const runResult = await resolvedBackend.runTurn(
      runContext,
      sessionRef,
      eventSink,
    );
    if (runResult.status === 'error') {
      error = runResult.error || 'Unknown error';
    } else if (runResult.result && !result) {
      result = runResult.result;
    }
  } catch (err) {
    error = err instanceof Error ? err.message : String(err);
    logger.warn(
      { chatJid: runContext.chatJid, error },
      'linear: agent run failed',
    );
  }

  return { text: result, error };
}

function parseCommitMessage(groupDir: string, identifier: string): string {
  const statusFile = path.join(groupDir, `${identifier}-status.md`);
  try {
    if (fs.existsSync(statusFile)) {
      const content = fs.readFileSync(statusFile, 'utf-8');
      const match = content.match(/git commit -m "([^"]+)"/);
      if (match) return match[1];
    }
  } catch {
    /* best-effort */
  }
  return `${identifier}: auto-apply patch artifact`;
}

export async function applyPatchArtifact(
  groupDir: string,
  identifier: string,
  issueId: string,
  ctx: LinearContext,
  worktreePath?: string,
): Promise<{ prUrl: string | null; applied: boolean }> {
  const noResult = { prUrl: null, applied: false };
  const effectiveCwd = worktreePath ?? PROJECT_ROOT;
  const gitOpts = { cwd: effectiveCwd, timeout: GIT_TIMEOUT_MS };

  if (!(IS_MACOS || IS_LINUX)) return noResult;
  if (!fs.existsSync(groupDir)) return noResult;

  const patchFiles = fs
    .readdirSync(groupDir)
    .filter(
      (f) =>
        f.startsWith(identifier) &&
        f.endsWith('.patch') &&
        !f.endsWith('.applied') &&
        path.basename(f) === f,
    );

  if (patchFiles.length === 0) return noResult;

  if (patchFiles.length > 1) {
    logger.info(
      { identifier, count: patchFiles.length },
      'patch-applicator: multiple patches found, using newest',
    );
  }

  const sorted = patchFiles
    .map((f) => ({
      name: f,
      mtime: fs.statSync(path.join(groupDir, f)).mtimeMs,
    }))
    .sort((a, b) => b.mtime - a.mtime);
  const patchFileName = sorted[0].name;
  const patchFilePath = path.join(groupDir, patchFileName);
  const patchRelPath = path.relative(effectiveCwd, patchFilePath);
  // Validate before any path/branch construction that interpolates the identifier.
  validateLinearIdentifier(identifier);
  const commitMessage = parseCommitMessage(groupDir, identifier);
  const branchName = `feat/${identifier.toLowerCase()}-auto-patch`;

  // Acquire mutex
  const prev = _patchMutex;
  let releaseMutex!: () => void;
  _patchMutex = new Promise<void>((r) => {
    releaseMutex = r;
  });
  await prev;

  const patchBytes = fs.readFileSync(patchFilePath);
  const patchHash = createHash('sha256').update(patchBytes).digest('hex');

  // Global serialization: a slow build blocks other patches, but concurrent
  // git operations on one working tree corrupt state. Acceptable for the
  // expected throughput (1-2 patches/hour).
  async function cleanup(deleteBranch: boolean): Promise<void> {
    if (!worktreePath) {
      try {
        await execFileAsync('git', ['checkout', 'main'], gitOpts);
        if (deleteBranch) {
          await execFileAsync('git', ['branch', '-D', branchName], gitOpts);
        }
      } catch {
        /* best-effort */
      }
    }
    releaseMutex();
  }

  try {
    if (!worktreePath) {
      // Branch safety: refuse if not on main
      const { stdout: currentBranch } = await execFileAsync(
        'git',
        ['rev-parse', '--abbrev-ref', 'HEAD'],
        gitOpts,
      );
      if (currentBranch.trim() !== 'main') {
        await ctx.client.createComment({
          issueId,
          body: `**Patch auto-apply skipped** — working tree is on branch \`${currentBranch.trim()}\`, not \`main\`.\n\nApply manually:\n\`\`\`bash\ngit apply ${patchRelPath}\n\`\`\``,
        });
        fireAndForget(
          notifyPipelineStep(
            ctx,
            issueId,
            identifier,
            'patch_failed',
            `hash:${patchHash} not on main`,
          ),
          { name: 'linear-dispatcher.notify-pipeline' },
        );
        releaseMutex();
        return noResult;
      }

      // Dirty tree check
      const { stdout: status } = await execFileAsync(
        'git',
        ['status', '--porcelain'],
        gitOpts,
      );
      if (status.trim()) {
        await ctx.client.createComment({
          issueId,
          body: `**Patch auto-apply skipped** — working tree has uncommitted changes.\n\nApply manually:\n\`\`\`bash\ngit apply ${patchRelPath}\n\`\`\``,
        });
        fireAndForget(
          notifyPipelineStep(
            ctx,
            issueId,
            identifier,
            'patch_failed',
            `hash:${patchHash} dirty tree`,
          ),
          { name: 'linear-dispatcher.notify-pipeline' },
        );
        releaseMutex();
        return noResult;
      }

      // Pull latest main
      await execFileAsync('git', ['pull', '--ff-only'], gitOpts);

      // Create feature branch (-B creates or resets)
      await execFileAsync('git', ['checkout', '-B', branchName], gitOpts);
    }

    // Pre-flight: reject or warn on patches touching restricted paths.
    // DEUS_PATCH_BLOCKED_PATHS overrides HARD_BLOCKED_PATHS_DEFAULT only.
    const blockedPathsRaw = (process.env.DEUS_PATCH_BLOCKED_PATHS ?? '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    const effectiveHardBlockedPaths =
      blockedPathsRaw.length > 0 ? blockedPathsRaw : HARD_BLOCKED_PATHS_DEFAULT;
    try {
      const { stdout: statOut } = await execFileAsync(
        'git',
        ['apply', '--stat', patchFilePath],
        { ...gitOpts, timeout: 30_000 },
      );
      const touchedFiles = statOut
        .split('\n')
        .filter((line) => line.includes(' | '))
        .map((line) => line.split(' | ')[0].trim())
        .filter(Boolean);

      // Hard-blocked: exact-file or directory-prefix matches
      const hardBlockedFiles = touchedFiles.filter((f) =>
        effectiveHardBlockedPaths.some((entry) => matchesPath(f, entry)),
      );
      // Shell scripts outside container/ are always hard-blocked
      const shellViolations = touchedFiles.filter(
        (f) => f.endsWith('.sh') && !f.startsWith('container/'),
      );
      // Warn-only: patch applied but warning comment posted
      const warnOnlyFiles = touchedFiles.filter((f) =>
        WARN_ONLY_PATHS.some((entry) => matchesPath(f, entry)),
      );

      const allHardBlocked = [
        ...new Set([...hardBlockedFiles, ...shellViolations]),
      ];
      if (allHardBlocked.length > 0) {
        await ctx.client.createComment({
          issueId,
          body: `**Patch auto-apply blocked** — patch touches restricted paths: ${allHardBlocked.map((f) => `\`${f}\``).join(', ')}.\n\nApply manually after review.`,
        });
        fireAndForget(
          notifyPipelineStep(
            ctx,
            issueId,
            identifier,
            'patch_failed',
            `hash:${patchHash} blocked paths: ${allHardBlocked.join(',')}`,
          ),
          { name: 'linear-dispatcher.notify-pipeline' },
        );
        await cleanup(true);
        return noResult;
      }

      if (warnOnlyFiles.length > 0) {
        await ctx.client.createComment({
          issueId,
          body: `**Patch auto-apply warning** — patch touches sensitive paths: ${warnOnlyFiles.map((f) => `\`${f}\``).join(', ')}. Applying anyway — please review carefully.`,
        });
      }
    } catch (statErr) {
      logger.warn(
        { issueId, err: statErr },
        'patch-applicator: git apply --stat failed, rejecting patch',
      );
      fireAndForget(
        notifyPipelineStep(
          ctx,
          issueId,
          identifier,
          'patch_failed',
          `hash:${patchHash} stat pre-flight failed`,
        ),
        { name: 'linear-dispatcher.notify-pipeline' },
      );
      await cleanup(true);
      return noResult;
    }

    // Pre-flight: verify patch applies cleanly before attempting git am/apply
    try {
      await execFileAsync('git', ['apply', '--check', patchFilePath], {
        ...gitOpts,
        timeout: 30_000,
      });
    } catch (checkErr: any) {
      const checkMsg = checkErr?.stderr || checkErr?.message || 'Unknown error';
      await ctx.client.createComment({
        issueId,
        body: `**Patch auto-apply blocked** — patch is malformed or does not apply cleanly:\n\n\`\`\`\n${checkMsg.slice(0, 2000)}\n\`\`\`\n\nApply manually after review.`,
      });
      notifyPipelineStep(
        ctx,
        issueId,
        identifier,
        'patch_failed',
        `hash:${patchHash} malformed patch`,
      ).catch(() => {});
      await cleanup(true);
      return noResult;
    }

    // Try git am first (for format-patch output), fall back to git apply
    let applied = false;
    try {
      await execFileAsync('git', ['am', patchFilePath], {
        ...gitOpts,
        timeout: 60_000,
      });
      applied = true;
    } catch {
      try {
        await execFileAsync('git', ['am', '--abort'], gitOpts);
      } catch {
        /* may not be in am state */
      }

      try {
        await execFileAsync('git', ['apply', patchFilePath], {
          ...gitOpts,
          timeout: 60_000,
        });
        await execFileAsync('git', ['add', '-A'], gitOpts);
        await execFileAsync('git', ['commit', '-m', commitMessage], gitOpts);
        applied = true;
      } catch (applyErr) {
        const stderr =
          applyErr instanceof Error ? applyErr.message : String(applyErr);
        await ctx.client.createComment({
          issueId,
          body: `**Patch auto-apply failed** — \`git apply\` returned an error:\n\n\`\`\`\n${stderr.slice(0, 2000)}\n\`\`\`\n\nApply manually: \`git apply ${patchRelPath}\``,
        });
        fireAndForget(
          notifyPipelineStep(
            ctx,
            issueId,
            identifier,
            'patch_failed',
            `hash:${patchHash} git apply failed`,
          ),
          { name: 'linear-dispatcher.notify-pipeline' },
        );
        await cleanup(true);
        return noResult;
      }
    }

    if (!applied) {
      await cleanup(true);
      return noResult;
    }

    // Build verification (sanitized env, process-group kill on timeout)
    try {
      const buildEnv: Record<string, string | undefined> = {
        PATH: process.env.PATH,
        HOME: HOME_DIR,
        NODE_ENV: process.env.NODE_ENV ?? 'production',
        LANG: process.env.LANG,
        TZ: process.env.TZ,
        TERM: process.env.TERM,
      };
      await new Promise<void>((resolve, reject) => {
        const child = spawn('npm', ['run', 'build'], {
          cwd: effectiveCwd,
          env: buildEnv,
          detached: true,
          stdio: ['ignore', 'ignore', 'pipe'],
        });
        const stderrChunks: Buffer[] = [];
        child.stderr?.on('data', (d: Buffer) => stderrChunks.push(d));

        const killTimer = setTimeout(() => {
          if (child.pid) forceKillProcessGroup(child.pid);
          reject(new Error(`Build timed out after ${BUILD_TIMEOUT_MS}ms`));
        }, BUILD_TIMEOUT_MS);
        killTimer.unref();

        child.on('close', (code) => {
          clearTimeout(killTimer);
          if (code !== 0)
            reject(
              new Error(Buffer.concat(stderrChunks).toString().slice(0, 2000)),
            );
          else resolve();
        });
        child.on('error', (err) => {
          clearTimeout(killTimer);
          reject(err);
        });
      });
    } catch (buildErr) {
      const stderr =
        buildErr instanceof Error ? buildErr.message : String(buildErr);
      await ctx.client.createComment({
        issueId,
        body: `**Patch applied but build failed** — \`npm run build\` exited with error:\n\n\`\`\`\n${stderr.slice(0, 2000)}\n\`\`\`\n\nBranch \`${branchName}\` has been deleted. Fix build errors and reapply.`,
      });
      fireAndForget(
        notifyPipelineStep(
          ctx,
          issueId,
          identifier,
          'patch_failed',
          `hash:${patchHash} build failed`,
        ),
        { name: 'linear-dispatcher.notify-pipeline' },
      );
      await cleanup(true);
      return noResult;
    }

    // Bump pre-emptively so the pre-push hook finds no drift and doesn't abort
    try {
      const { stdout: baseRef } = await execFileAsync(
        'git',
        ['merge-base', 'HEAD', 'origin/main'],
        gitOpts,
      );
      await execFileAsync(
        PYTHON_BIN,
        ['scripts/drift_check.py', '--bump', '--base', baseRef.trim()],
        gitOpts,
      );
      const { stdout: bumpStatus } = await execFileAsync(
        'git',
        ['status', '--porcelain', '--', 'patterns/'],
        gitOpts,
      );
      if (bumpStatus.trim()) {
        await execFileAsync('git', ['add', 'patterns/'], gitOpts);
        await execFileAsync(
          'git',
          ['commit', '-m', 'chore(patterns): auto-bump drifted patterns'],
          gitOpts,
        );
      }
    } catch (err) {
      logger.warn({ err }, 'applyPatchArtifact: drift bump failed');
    }

    // Push
    await execFileAsync('git', ['push', '-u', 'origin', 'HEAD'], {
      ...gitOpts,
      timeout: PUSH_TIMEOUT_MS,
    });

    // Create PR
    let prUrl: string | null = null;
    try {
      const { stdout: prOutput } = await execFileAsync(
        'gh',
        [
          'pr',
          'create',
          '--title',
          `feat(${identifier}): auto-apply patch`,
          '--body',
          `Auto-applied from ${patchFileName}\n\nRef: ${identifier}`,
        ],
        { ...gitOpts, timeout: 60_000 },
      );
      prUrl = extractPrUrl(prOutput, ctx.repoSlug) ?? prOutput.trim();
    } catch (prErr) {
      // Push succeeded — leave branch for manual PR creation
      const stderr = prErr instanceof Error ? prErr.message : String(prErr);
      await ctx.client.createComment({
        issueId,
        body: `**Patch applied and pushed** but \`gh pr create\` failed:\n\n\`\`\`\n${stderr.slice(0, 1000)}\n\`\`\`\n\nBranch \`${branchName}\` is pushed — create PR manually.`,
      });
      fireAndForget(
        notifyPipelineStep(
          ctx,
          issueId,
          identifier,
          'patch_failed',
          `hash:${patchHash} gh pr create failed`,
        ),
        { name: 'linear-dispatcher.notify-pipeline' },
      );
      await cleanup(false);
      return noResult;
    }

    // Success — rename patch, notify, return
    try {
      fs.renameSync(patchFilePath, patchFilePath + '.applied');
    } catch {
      /* best-effort */
    }

    fireAndForget(
      notifyPipelineStep(
        ctx,
        issueId,
        identifier,
        'patch_applied',
        `hash:${patchHash} pr:${prUrl}`,
      ),
      { name: 'linear-dispatcher.notify-pipeline' },
    );
    logger.info(
      { issueId, prUrl, patchFileName },
      'patch-applicator: PR created from patch artifact',
    );

    await cleanup(false);
    return { prUrl, applied: true };
  } catch (err) {
    logger.warn(
      { issueId, err },
      'patch-applicator: unexpected error during patch application',
    );
    await cleanup(true);
    return noResult;
  }
}

// Checks live PR state via gh CLI and routes the issue accordingly.
// DB-first in triageIssue avoids the API call when we already know the answer;
// this function only runs when the DB record is stale or absent.
async function handlePrState(
  ctx: LinearContext,
  issueId: string,
  identifier: string,
  prUrl: string,
): Promise<'skip' | 'dispatch'> {
  const prState = await queryPrState(prUrl);
  if (!prState) return 'dispatch';

  if (prState.state === 'MERGED') {
    updatePrAutoMergeState(issueId, 'merged');
    const doneState = ctx.stateByName.get('Done');
    if (!doneState) {
      logger.warn(
        { issueId },
        'triage: "Done" state not found, falling back to dispatch',
      );
      return 'dispatch';
    }
    await ctx.client.updateIssue(issueId, { stateId: doneState.id });
    logPipelineEvent(issueId, identifier, 'triage_skip_merged', prUrl);
    logger.info({ issueId, prUrl }, 'triage: PR merged, moved to Done');
    return 'skip';
  }

  if (prState.state === 'OPEN') {
    const reviewState = ctx.stateByName.get('In Review');
    if (!reviewState) {
      logger.warn(
        { issueId },
        'triage: "In Review" state not found, falling back to dispatch',
      );
      return 'dispatch';
    }
    await ctx.client.updateIssue(issueId, { stateId: reviewState.id });
    logPipelineEvent(issueId, identifier, 'triage_skip_open_pr', prUrl);
    logger.info({ issueId, prUrl }, 'triage: PR open, moved to In Review');
    return 'skip';
  }

  // CLOSED: PR was abandoned/superseded. Dispatch a fresh agent to redo the work.
  logger.info({ issueId, prUrl }, 'triage: PR closed, dispatching fresh agent');
  return 'dispatch';
}

// Pre-dispatch triage: checks DB then live GH state then comment text scan.
// Returns 'skip' when the issue already has a merged/open PR (no agent needed).
async function triageIssue(
  ctx: LinearContext,
  issue: { id: string; identifier: string; description: string | null },
  comments: Array<{ author: string; body: string }>,
): Promise<'dispatch' | 'skip'> {
  const dbRecord = getIssuePr(issue.id);

  if (dbRecord?.auto_merge_state === 'merged') {
    const doneState = ctx.stateByName.get('Done');
    if (!doneState) {
      logger.warn(
        { issueId: issue.id },
        'triage: "Done" state not found, falling back to dispatch',
      );
      return 'dispatch';
    }
    await ctx.client.updateIssue(issue.id, { stateId: doneState.id });
    logPipelineEvent(
      issue.id,
      issue.identifier,
      'triage_skip_merged',
      dbRecord.pr_url,
    );
    logger.info(
      { issueId: issue.id },
      'triage: DB shows merged, moved to Done',
    );
    return 'skip';
  }

  if (dbRecord?.pr_url) {
    return handlePrState(ctx, issue.id, issue.identifier, dbRecord.pr_url);
  }

  // Fallback: scan issue text for PR URLs not yet recorded in DB
  const textToScan = [
    issue.description ?? '',
    ...comments.map((c) => c.body),
  ].join('\n');
  const foundUrl = extractPrUrl(textToScan, ctx.repoSlug);
  if (foundUrl) {
    upsertIssuePr(issue.id, foundUrl, undefined, issue.identifier);
    return handlePrState(ctx, issue.id, issue.identifier, foundUrl);
  }

  return 'dispatch';
}

async function ensureIssueWorktree(
  identifier: string,
): Promise<{ worktreePath: string; branchName: string } | null> {
  validateLinearIdentifier(identifier);
  if (!(IS_MACOS || IS_LINUX)) return null;
  const branchName = `feat/${identifier.toLowerCase()}-auto-patch`;
  const worktreePath = path.resolve(
    path.join(DATA_DIR, 'worktrees', identifier),
  );
  const sandboxRoot = path.resolve(path.join(DATA_DIR, 'worktrees'));
  if (!worktreePath.startsWith(sandboxRoot + path.sep)) {
    throw new Error(`Worktree path escapes sandbox: ${worktreePath}`);
  }
  if (fs.existsSync(worktreePath)) return { worktreePath, branchName };
  fs.mkdirSync(path.join(DATA_DIR, 'worktrees'), { recursive: true });
  await execFileAsync(
    'git',
    ['worktree', 'add', worktreePath, '-b', branchName],
    { cwd: PROJECT_ROOT, timeout: GIT_TIMEOUT_MS },
  );
  return { worktreePath, branchName };
}

async function cleanupWorktree(worktreePath: string): Promise<void> {
  try {
    await execFileAsync(
      'git',
      ['worktree', 'remove', '--force', worktreePath],
      { cwd: PROJECT_ROOT, timeout: GIT_TIMEOUT_MS },
    );
  } catch {
    try {
      fs.rmSync(worktreePath, { recursive: true, force: true });
    } catch {
      /* best-effort */
    }
    try {
      await execFileAsync('git', ['worktree', 'prune'], {
        cwd: PROJECT_ROOT,
        timeout: GIT_TIMEOUT_MS,
      });
    } catch {
      /* best-effort */
    }
  }
}

/**
 * Returns the list of files modified in the worktree (via `git diff --name-only HEAD`)
 * that are NOT covered by any glob in `allowlist`. An empty array means all changed
 * files are within scope. If git diff fails (e.g. no commits yet), returns [] and
 * logs a warning — the check is best-effort.
 */
export async function checkWriteAllowlist(
  worktreePath: string,
  allowlist: string[],
): Promise<string[]> {
  let stdout: string;
  try {
    const result = await execFileAsync('git', ['diff', '--name-only', 'HEAD'], {
      cwd: worktreePath,
      timeout: GIT_TIMEOUT_MS,
    });
    stdout = result.stdout;
  } catch (err) {
    logger.warn(
      { worktreePath, err },
      'linear-dispatcher: checkWriteAllowlist: git diff failed (new worktree?), skipping check',
    );
    return [];
  }

  const changedFiles = stdout
    .split('\n')
    .map((f) => f.trim())
    .filter((f) => f.length > 0);

  const violations = changedFiles.filter(
    (file) => !allowlist.some((glob) => minimatch(file, glob, { dot: true })),
  );
  return violations;
}

async function runIssue(
  ctx: LinearContext,
  issueId: string,
  identifier: string,
  runContext: RunContext,
  roleSpec?: RoleSpec,
): Promise<void> {
  const workingState = ctx.stateByName.get('Agent Working')!;
  try {
    await ctx.client.updateIssue(issueId, { stateId: workingState.id });
  } catch (err) {
    logger.warn(
      { issueId, err },
      'linear-dispatcher: failed to move issue to Agent Working',
    );
  }

  fireAndForget(notifyPipelineStep(ctx, issueId, identifier, 'agent_started'), {
    name: 'linear-dispatcher.notify-pipeline',
  });

  const { text, error } = await executeAgentRun(ctx, runContext);

  ctx.deps.queue.notifyIdle(runContext.chatJid);

  // Write-allowlist check: runs after agent completes, before patch application
  if (!error && runContext.worktreePath && roleSpec?.writeAllowlist?.length) {
    const violations = await checkWriteAllowlist(
      runContext.worktreePath,
      roleSpec.writeAllowlist,
    );
    if (violations.length > 0) {
      const fileList = violations.map((f) => `- \`${f}\``).join('\n');
      const reviseBody =
        `**Write-allowlist violation** — the agent modified files outside the role's permitted scope.\n\n` +
        `**Violating files** (${violations.length}):\n${fileList}\n\n` +
        `Allowed globs: ${roleSpec.writeAllowlist.map((g) => `\`${g}\``).join(', ')}\n\n` +
        `---\n*To retry: fix the agent role spec or the implementation, then move to **Ready for Agent**.*`;
      try {
        await ctx.client.createComment({ issueId, body: reviseBody });
        const reviseState =
          ctx.stateByName.get('Needs Revision') ??
          ctx.stateByName.get('Todo') ??
          ctx.stateByName.get('Backlog')!;
        await ctx.client.updateIssue(issueId, { stateId: reviseState.id });
        notifyPipelineStep(
          ctx,
          issueId,
          identifier,
          'agent_failed',
          `write-allowlist violation: ${violations.join(', ')}`,
        ).catch(() => {});
        logger.warn(
          { issueId, violations },
          'linear-dispatcher: write-allowlist violations detected, issue moved to revise state',
        );
      } catch (reviseErr) {
        logger.warn(
          { issueId, reviseErr },
          'linear-dispatcher: failed to post write-allowlist violation comment',
        );
      }
      ctx.inFlightDispatch.delete(issueId);
      if (runContext.worktreePath) {
        await cleanupWorktree(runContext.worktreePath).catch(() => {});
      }
      return;
    }
  }

  try {
    if (error) {
      await ctx.client.createComment({
        issueId,
        body: `**Agent run failed**\n\n\`\`\`\n${error}\n\`\`\`\n\n---\n*To retry: move to **Todo**, then to **Ready for Agent**.*`,
      });
      fireAndForget(
        notifyPipelineStep(ctx, issueId, identifier, 'agent_failed', error),
        { name: 'linear-dispatcher.notify-pipeline' },
      );
      const agentFailCount = getConsecutiveFailCount(issueId, 'agent_failed');
      if (agentFailCount >= CIRCUIT_BREAKER_THRESHOLD) {
        const manualReviewState = ctx.stateByName.get('Manual Review Required');
        const parkState = manualReviewState ?? ctx.stateByName.get('Backlog')!;
        await ctx.client.updateIssue(issueId, { stateId: parkState.id });
        await ctx.client.createComment({
          issueId,
          body: `**Circuit breaker tripped** — ${agentFailCount} consecutive agent failures. Moved to ${parkState.name}.\n\nTo retry: investigate the root cause, then move back to **Ready for Agent**.`,
        });
        logPipelineEvent(
          issueId,
          identifier,
          'circuit_breaker_tripped',
          `${agentFailCount} consecutive agent failures`,
        );
        logger.warn(
          { issueId, agentFailCount },
          'linear-dispatcher: circuit breaker tripped, parked issue',
        );
      } else {
        const backlogState = ctx.stateByName.get('Backlog')!;
        await ctx.client.updateIssue(issueId, { stateId: backlogState.id });
        logger.warn(
          { issueId },
          'linear-dispatcher: moved failed issue to Backlog',
        );
      }
    } else {
      const body = text || '(no output produced)';
      let prUrl = extractPrUrl(body, ctx.repoSlug);

      if (!prUrl) {
        try {
          const groupDir = path.join(GROUPS_DIR, DISPATCH_GROUP_JID);
          const patchResult = await applyPatchArtifact(
            groupDir,
            identifier,
            issueId,
            ctx,
            runContext.worktreePath,
          );
          if (patchResult.applied && patchResult.prUrl) {
            prUrl = patchResult.prUrl;
          }
        } catch (patchErr) {
          logger.warn(
            { issueId, patchErr },
            'linear-dispatcher: patch application failed',
          );
        }
      }

      if (prUrl) {
        upsertIssuePr(issueId, prUrl, undefined, identifier);
        logger.info({ issueId, prUrl }, 'linear-dispatcher: persisted PR URL');
      }
      await ctx.client.createComment({ issueId, body });

      if (prUrl) {
        notifyPipelineStep(ctx, issueId, identifier, 'pr_created', prUrl).catch(
          () => {},
        );
      }

      const reviewState = ctx.stateByName.get('In Review')!;
      await ctx.client.updateIssue(issueId, {
        stateId: reviewState.id,
        assigneeId: ctx.viewerId,
      });
      notifyPipelineStep(ctx, issueId, identifier, 'agent_completed').catch(
        () => {},
      );
      logPipelineEvent(
        issueId,
        identifier,
        'circuit_breaker_reset',
        'agent completed successfully',
      );
      logger.info({ issueId }, 'linear-dispatcher: issue moved to In Review');

      // Event-hub Phase 1: emit `agent.done` alongside the inline write above.
      // The LinearUpdater listener is dry-run (log-only) in Step 1, so the
      // inline block remains authoritative and behavior is unchanged. At the
      // Step-2 cutover the inline In-Review block (updateIssue + agent_completed
      // + circuit_breaker_reset) is deleted and this emit becomes the sole
      // driver via the live listener.
      await ctx.bus.emit({
        type: 'agent.done',
        source: 'linear-dispatcher',
        actor: 'bot',
        correlationId: { kind: 'issue', id: issueId, identifier },
        ts: new Date().toISOString(),
        payload: { output: body, prUrl: prUrl ?? undefined },
      });
    }
  } catch (err) {
    logger.warn(
      { issueId, err },
      'linear-dispatcher: failed to update Linear issue after run',
    );
  } finally {
    ctx.inFlightDispatch.delete(issueId);
    if (runContext.worktreePath) {
      await cleanupWorktree(runContext.worktreePath).catch((err) => {
        logger.error(
          { worktreePath: runContext.worktreePath, err },
          'linear-dispatcher: worktree cleanup failed',
        );
      });
    }
  }
}

async function pollLinear(): Promise<void> {
  if (!_ctx) return;
  const ctx = _ctx;

  // Conflict detection sweep — runs every poll alongside dispatch
  checkConflictingPrs(ctx).catch((err) => {
    logger.warn({ err }, 'linear-dispatcher: conflict check sweep failed');
  });

  const readyState = ctx.stateByName.get('Ready for Agent')!;

  // Bound the only per-tick network call. On timeout withTimeout throws
  // RetryableError, which _tick's onError logs as transient and the next interval
  // retries. If this times out the dispatch loop below is never entered, so the
  // in-loop SDK reads (labels/comments) need no wrapping. Note: this bounds the
  // await, not the underlying socket — the @linear/sdk high-level client methods
  // expose no per-call AbortSignal. Acceptable for a low-impact latent gap; upgrade
  // path is a per-poll client built with AbortSignal.timeout.
  const issues = await withTimeout(
    ctx.client.issues({
      filter: { state: { id: { eq: readyState.id } } },
    }),
    _fetchTimeoutMs,
    { name: 'linear.poll.issues' },
  );

  const sorted = [...issues.nodes].sort((a, b) => {
    // Priority first (1=urgent > 2=high > 3=medium > 4=low > 0=none)
    const pa = a.priority === 0 ? 99 : a.priority;
    const pb = b.priority === 0 ? 99 : b.priority;
    if (pa !== pb) return pa - pb;
    return a.sortOrder - b.sortOrder;
  });

  let dispatched = 0;
  const slots = ctx.deps.queue.availableSlots();

  for (const issue of sorted) {
    if (dispatched >= slots) break;
    if (ctx.inFlightDispatch.has(issue.id)) continue;

    const mergeFailCount = getConsecutiveFailCount(
      issue.id,
      'automerge_failed',
    );
    if (mergeFailCount > 0) {
      const lastFail = getLastFailTime(issue.id, 'automerge_failed');
      if (lastFail) {
        const backoffMs =
          mergeFailCount === 1 ? BACKOFF_FIRST_MS : BACKOFF_REPEAT_MS;
        const elapsed = Date.now() - new Date(lastFail).getTime();
        if (elapsed < backoffMs) {
          logger.debug(
            { issueId: issue.id, mergeFailCount, backoffMs, elapsed },
            'linear-dispatcher: skipping issue in CI backoff window',
          );
          continue;
        }
      }
    }

    const agentFailCount = getConsecutiveFailCount(issue.id, 'agent_failed');
    if (agentFailCount > 0) {
      const lastAgentFail = getLastFailTime(issue.id, 'agent_failed');
      if (lastAgentFail) {
        const backoffMs =
          agentFailCount === 1 ? BACKOFF_FIRST_MS : BACKOFF_REPEAT_MS;
        const elapsed = Date.now() - new Date(lastAgentFail).getTime();
        if (elapsed < backoffMs) {
          logger.debug(
            { issueId: issue.id, agentFailCount, backoffMs, elapsed },
            'linear-dispatcher: skipping issue in agent failure backoff window',
          );
          continue;
        }
      }
    }

    const labels = await issue.labels();
    const agentLabel = labels.nodes.find((l) => l.name.startsWith('agent:'));
    const isScoped = labels.nodes.some((l) => l.name === 'Scoped');

    let roleSpec: RoleSpec | undefined;
    if (agentLabel) {
      roleSpec = _roleSpecs.get(agentLabel.name);
      if (!roleSpec) {
        logger.warn(
          { issueId: issue.id, label: agentLabel.name },
          'linear-dispatcher: no role spec found for label, skipping',
        );
        continue;
      }
    } else if (!isScoped) {
      logger.debug(
        { issueId: issue.id },
        'linear-dispatcher: issue has no agent:* label and is not scoped, skipping',
      );
      continue;
    }

    // Add before triage to prevent concurrent polls from double-processing the same issue
    ctx.inFlightDispatch.add(issue.id);

    const issueComments = await issue.comments();
    const comments = await Promise.all(
      issueComments.nodes.map(async (c) => {
        const user = await c.user;
        return { author: user?.displayName ?? 'Unknown', body: c.body };
      }),
    );

    const triageResult = await triageIssue(
      ctx,
      {
        id: issue.id,
        identifier: issue.identifier,
        description: issue.description ?? null,
      },
      comments,
    );
    if (triageResult === 'skip') {
      ctx.inFlightDispatch.delete(issue.id);
      continue;
    }

    const failureDossier = buildFailureDossier(issue.id);
    let prompt: string;
    if (roleSpec) {
      prompt = buildIssuePrompt(
        roleSpec,
        issue.title,
        issue.identifier,
        issue.description ?? undefined,
        comments,
        failureDossier,
      );
    } else {
      prompt = buildScopedIssuePrompt(
        issue.title,
        issue.identifier,
        issue.description ?? '',
        comments,
        failureDossier,
      );
    }

    const chatJid = `linear-dispatch-${issue.id.slice(0, 8)}`;
    let worktreePath: string | undefined;
    try {
      const wt = await ensureIssueWorktree(issue.identifier);
      if (wt) worktreePath = wt.worktreePath;
    } catch (wtErr) {
      logger.warn(
        { issueId: issue.id, err: wtErr },
        'linear-dispatcher: failed to create worktree, dispatching without isolation',
      );
    }
    const runContext: RunContext = {
      prompt,
      groupFolder: DISPATCH_GROUP_JID,
      chatJid,
      isControlGroup: true,
      isScheduledTask: true,
      effort: 'high',
      ...(worktreePath && { worktreePath }),
    };

    fireAndForget(
      notifyPipelineStep(ctx, issue.id, issue.identifier, 'agent_dispatched'),
      { name: 'linear-dispatcher.notify-pipeline' },
    );

    ctx.deps.queue.enqueueTask(chatJid, issue.id, () =>
      runIssue(ctx, issue.id, issue.identifier, runContext, roleSpec),
    );

    dispatched++;
    logger.info(
      {
        issueId: issue.id,
        role: roleSpec?.name ?? 'scoped',
        chatJid,
        sortOrder: issue.sortOrder,
      },
      'linear-dispatcher: enqueued issue for agent run',
    );
  }

  if (sorted.length > dispatched) {
    logger.debug(
      { total: sorted.length, dispatched, slots },
      'linear-dispatcher: issues waiting in Ready for Agent (no slots)',
    );
  }
}

export async function initLinearContext(
  apiKey: string,
  deps: LinearDispatcherDependencies,
): Promise<LinearContext | null> {
  if (!/^[A-Za-z0-9_-]+$/.test(apiKey)) {
    throw new FatalError('linear: LINEAR_API_KEY contains invalid characters');
  }

  const client = new LinearClient({ apiKey });

  try {
    const teamId = await discoverTeamId(client);
    const stateByName = await discoverWorkflowStates(client, teamId);

    const stateById = new Map<string, WorkflowState>();
    for (const state of stateByName.values()) {
      stateById.set(state.id, state);
    }

    // Prune stale worktrees from previous crashes
    try {
      await execFileAsync('git', ['worktree', 'prune'], {
        cwd: PROJECT_ROOT,
        timeout: GIT_TIMEOUT_MS,
      });
    } catch {
      /* best-effort */
    }

    // Startup: cleanup here avoids separate scheduler; failures are caught silently
    try {
      await execFileAsync(
        PYTHON_BIN,
        ['scripts/cleanup_stale_branches.py', '--delete'],
        {
          cwd: PROJECT_ROOT,
          timeout: GIT_TIMEOUT_MS,
        },
      );
    } catch {
      /* non-fatal */
    }

    const viewer = await client.viewer;
    const botUserId = process.env.LINEAR_BOT_USER_ID || viewer.id;

    const existing = Object.values(deps.registeredGroups()).find(
      (g) => g.folder === DISPATCH_GROUP_JID,
    );
    let dispatchGroup: RegisteredGroup = existing
      ? { ...existing }
      : {
          name: 'Linear Dispatch',
          folder: DISPATCH_GROUP_JID,
          trigger: '',
          added_at: new Date().toISOString(),
          requiresTrigger: false,
          isControlGroup: true,
        };
    if (!existing) {
      deps.registerGroup(DISPATCH_GROUP_JID, dispatchGroup);
    }
    // Upgrade existing installs: dispatch group needs control-group privileges
    // for writable project mounts (agents create branches, edit files, commit)
    if (existing && !dispatchGroup.isControlGroup) {
      dispatchGroup = { ...dispatchGroup, isControlGroup: true };
      deps.registerGroup(DISPATCH_GROUP_JID, dispatchGroup);
      logger.info(
        'linear-dispatcher: upgraded dispatch group to control group',
      );
    }

    // Auto-associate project with dispatch group (find-or-create pattern)
    const dispatchProjectPath = process.env.LINEAR_DISPATCH_PROJECT;
    if (dispatchProjectPath && !dispatchGroup.projectId) {
      const resolved = path.resolve(
        dispatchProjectPath.replace(/^~/, HOME_DIR),
      );
      let realPath = '';
      try {
        realPath = fs.realpathSync(resolved);
      } catch {
        logger.warn(
          { path: resolved },
          'linear-dispatcher: LINEAR_DISPATCH_PROJECT path does not exist',
        );
      }
      if (realPath) {
        let project = getProjectByPath(realPath);
        if (!project) {
          try {
            project = registerProject(path.basename(realPath), realPath, {
              readonly: false,
            });
            logger.info(
              { projectId: project.id, path: realPath },
              'linear-dispatcher: auto-registered project',
            );
          } catch (err) {
            project = getProjectByPath(realPath);
            if (project) {
              logger.info(
                { projectId: project.id, path: realPath },
                'linear-dispatcher: project already registered (race)',
              );
            } else {
              const msg = err instanceof Error ? err.message : String(err);
              logger.warn(
                { path: realPath, error: msg },
                'linear-dispatcher: failed to register project',
              );
            }
          }
        }
        if (project) {
          dispatchGroup = { ...dispatchGroup, projectId: project.id };
          deps.registerGroup(DISPATCH_GROUP_JID, dispatchGroup);
          logger.info(
            { projectId: project.id },
            'linear-dispatcher: project associated with dispatch group',
          );
        }
      }
    }

    // Bootstrap group CLAUDE.md from template if not yet initialized
    const groupDir = path.join(GROUPS_DIR, DISPATCH_GROUP_JID);
    const claudeMdPath = path.join(groupDir, 'CLAUDE.md');
    const templatePath = path.join(
      GROUPS_DIR,
      DISPATCH_GROUP_JID,
      'CLAUDE.md.template',
    );
    if (!fs.existsSync(claudeMdPath) && fs.existsSync(templatePath)) {
      fs.copyFileSync(templatePath, claudeMdPath);
      logger.info('linear-dispatcher: initialized CLAUDE.md from template');
    }

    // Discover or create gate status labels for board-level visibility
    const gateLabels: GateLabels = { effort: {}, complexity: {} };
    const statusDefs: Array<{
      key: 'evaluating' | 'scoped' | 'revise' | 'error';
      name: string;
      color: string;
    }> = [
      { key: 'evaluating', name: 'Warden: Evaluating', color: '#f59e0b' },
      { key: 'scoped', name: 'Scoped', color: '#16a34a' },
      { key: 'revise', name: 'Warden: Revise', color: '#dc2626' },
      { key: 'error' as const, name: 'Warden: Error', color: '#ef4444' },
    ];
    // Effort 1-5: green→yellow→red gradient
    const effortColors = [
      '#16a34a',
      '#65a30d',
      '#ca8a04',
      '#ea580c',
      '#dc2626',
    ];
    // Complexity 1-5: blue gradient
    const complexityColors = [
      '#93c5fd',
      '#60a5fa',
      '#3b82f6',
      '#2563eb',
      '#1d4ed8',
    ];
    let labelMap = new Map<string, string>();
    try {
      const allLabels = await client.issueLabels();
      labelMap = new Map(allLabels.nodes.map((l) => [l.name, l.id]));
    } catch (err) {
      logger.error(
        { err },
        'linear: failed to fetch labels — gate labels will be unavailable',
      );
    }

    async function ensureLabel(
      key: string,
      name: string,
      color: string,
    ): Promise<string | undefined> {
      try {
        if (labelMap.has(name)) return labelMap.get(name);
        const created = await client.createIssueLabel({
          name,
          color,
          teamId,
        });
        const label = await created.issueLabel;
        return label?.id;
      } catch (err) {
        logger.error(
          { labelName: name, err },
          'linear: failed to create/discover label — partial label failure, remaining labels unaffected',
        );
        return undefined;
      }
    }

    for (const def of statusDefs) {
      const id = await ensureLabel(def.key, def.name, def.color);
      if (id) gateLabels[def.key] = id;
    }

    if (labelMap.has('warden:skip')) {
      gateLabels.wardenSkip = labelMap.get('warden:skip');
    }

    const bouncedDefs: Array<{
      key: 'blocked' | 'bouncedUnscoped' | 'bouncedStale' | 'bouncedNoContext';
      name: string;
      color: string;
    }> = [
      { key: 'blocked', name: 'warden:blocked', color: '#dc2626' },
      { key: 'bouncedUnscoped', name: 'bounced:unscoped', color: '#f97316' },
      { key: 'bouncedStale', name: 'bounced:stale', color: '#f97316' },
      {
        key: 'bouncedNoContext',
        name: 'bounced:no-context',
        color: '#f97316',
      },
    ];
    for (const def of bouncedDefs) {
      const id = await ensureLabel(def.key, def.name, def.color);
      if (id) gateLabels[def.key] = id;
    }

    const conflictId = await ensureLabel('conflict', 'conflict', '#b45309');
    if (conflictId) gateLabels.conflict = conflictId;

    await ensureLabel('done-pre', 'Done: Pre-implemented', '#6b7280');

    for (let i = 1; i <= 5; i++) {
      const eId = await ensureLabel(
        `effort-${i}`,
        `Effort: ${i}`,
        effortColors[i - 1],
      );
      if (eId) gateLabels.effort[i] = eId;
      const cId = await ensureLabel(
        `complexity-${i}`,
        `Complexity: ${i}`,
        complexityColors[i - 1],
      );
      if (cId) gateLabels.complexity[i] = cId;
    }

    const populatedKeys = Object.entries(gateLabels)
      .filter(
        ([k, v]) => k !== 'effort' && k !== 'complexity' && v !== undefined,
      )
      .map(([k]) => k);
    const missingKeys = ['evaluating', 'scoped', 'revise', 'error'].filter(
      (k) => !(gateLabels as unknown as Record<string, unknown>)[k],
    );
    if (missingKeys.length > 0) {
      logger.error(
        { missingKeys },
        'linear: some gate labels failed to populate — label operations will be no-ops for these',
      );
    } else {
      logger.info(
        { populated: populatedKeys.length },
        'linear: all gate labels populated',
      );
    }

    logger.info(
      { teamId, states: [...stateByName.keys()] },
      'linear: context initialized',
    );

    // Derive repo slug for PR URL scoping
    let repoSlug: string | undefined = process.env.GITHUB_REPO;
    if (!repoSlug) {
      try {
        const { execFileSync } = await import('child_process');
        const remoteUrl = execFileSync('git', ['remote', 'get-url', 'origin'], {
          encoding: 'utf-8',
          timeout: 5000,
        }).trim();
        const match = remoteUrl.match(
          /github\.com[:/]([^/]+\/[^/.]+?)(?:\.git)?$/,
        );
        if (match) repoSlug = match[1];
      } catch {
        logger.warn(
          'linear: could not derive GITHUB_REPO from git remote; PR URL scoping disabled',
        );
      }
    }

    return {
      client,
      bus: getBus(),
      stateByName,
      stateById,
      botUserId,
      viewerId: viewer.id,
      deps,
      dispatchGroup,
      inFlightDispatch: new Set(),
      inFlightGate: new Set(),
      gateLabels,
      teamId,
      repoSlug,
      vaultPath: resolveVaultPath(),
    };
  } catch (err) {
    if (err instanceof FatalError) {
      logger.warn({ err }, 'linear: initialization failed — dormant');
      return null;
    }
    throw err;
  }
}

export function startLinearDispatcher(ctx: LinearContext): void {
  if (_timer) {
    logger.warn('linear-dispatcher: already running');
    return;
  }

  _roleSpecs = loadRoleSpecs(AGENTS_DIR);
  if (_roleSpecs.size === 0) {
    logger.warn(
      'linear-dispatcher: no role specs with linear_label found — daemon dormant',
    );
    return;
  }
  logger.info(
    { labels: [..._roleSpecs.keys()] },
    'linear-dispatcher: loaded role specs',
  );

  _ctx = ctx;

  const parsedPollMs = parseInt(
    process.env.LINEAR_POLL_INTERVAL_MS || String(DEFAULT_POLL_MS),
    10,
  );
  const pollMs =
    isNaN(parsedPollMs) || parsedPollMs < 1000 ? DEFAULT_POLL_MS : parsedPollMs;
  _fetchTimeoutMs = deriveFetchTimeout(pollMs);

  _tick = () => {
    fireAndForget(() => pollLinear(), {
      name: 'linear.dispatch',
      onError: (err) => {
        if (err instanceof RetryableError) {
          logger.warn(
            { err },
            'linear-dispatcher: transient error, will retry',
          );
        } else {
          logger.error(
            { err },
            'linear-dispatcher: unexpected error during poll',
          );
        }
      },
    });
  };

  _tick();
  _timer = setInterval(_tick, pollMs);
  _timer.unref();
  logger.info(
    { pollMs, roles: _roleSpecs.size },
    'linear-dispatcher: daemon started',
  );
}

export function stopLinearDispatcher(): void {
  if (_timer) {
    clearInterval(_timer);
    _timer = null;
  }
  _tick = null;
  _ctx = null;
}

const MIN_POLL_MS = 5_000;
const MAX_POLL_MS = 300_000;

export function setPollInterval(ms: number): boolean {
  if (!_tick) return false;
  const clamped = Math.max(MIN_POLL_MS, Math.min(MAX_POLL_MS, ms));
  _fetchTimeoutMs = deriveFetchTimeout(clamped);
  if (_timer) clearInterval(_timer);
  _timer = setInterval(_tick, clamped);
  _timer.unref();
  logger.info({ pollMs: clamped }, 'linear-dispatcher: poll interval updated');
  return true;
}
