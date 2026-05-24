import fs from 'fs';
import path from 'path';
import { LinearClient } from '@linear/sdk';
import { DATA_DIR, HOME_DIR, GROUPS_DIR } from './config.js';
import { parse as parseYaml } from 'yaml';
import { logger } from './logger.js';
import { PROJECT_ROOT } from './config.js';
import { getProjectByPath, registerProject } from './project-registry.js';
import { FatalError, RetryableError } from './errors/index.js';
import { fireAndForget } from './async/index.js';
import { extractPrUrl } from './pr-url-extractor.js';
import {
  CIRCUIT_BREAKER_THRESHOLD,
  getConsecutiveFailCount,
  getIssuePr,
  getLastFailTime,
  getPipelineEvents,
  logPipelineEvent,
  upsertIssuePr,
} from './db.js';
import { notifyPipelineStep } from './linear-notifications.js';
import { resolveVaultPath } from './solutions/store.js';
import { defaultSession } from './agent-runtimes/types.js';
import type {
  RunContext,
  RuntimeEventSink,
  RuntimeSession,
} from './agent-runtimes/types.js';
import type { RuntimeRegistry } from './agent-runtimes/registry.js';
import type { GroupQueue } from './group-queue.js';
import type { RegisteredGroup } from './types.js';

const DEFAULT_POLL_MS = 30_000;
const DISPATCH_GROUP_JID = 'linear-dispatch';
const BACKOFF_FIRST_MS = 5 * 60_000;
const BACKOFF_REPEAT_MS = 10 * 60_000;
const AGENTS_DIR = path.join(PROJECT_ROOT, '.claude', 'agents');

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
}

export interface WorkflowState {
  id: string;
  name: string;
}

export interface GateLabels {
  evaluating?: string;
  scoped?: string;
  revise?: string;
  wardenSkip?: string;
  effort: Record<number, string>;
  complexity: Record<number, string>;
}

export interface LinearContext {
  client: LinearClient;
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
let _roleSpecs: Map<string, RoleSpec> = new Map();
let _ctx: LinearContext | null = null;

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
    specs.set(label, {
      label,
      name: typeof data.name === 'string' ? data.name : file.replace('.md', ''),
      model: typeof data.model === 'string' ? data.model : undefined,
      content: body.trim(),
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

export function extractScopeBlock(description: string): string {
  const startMarker = '<!-- gate:agent-readiness-gate:start -->';
  const endMarker = '<!-- gate:agent-readiness-gate:end -->';
  const startIdx = description.indexOf(startMarker);
  const endIdx = description.indexOf(endMarker);
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

async function runIssue(
  ctx: LinearContext,
  issueId: string,
  identifier: string,
  runContext: RunContext,
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

  notifyPipelineStep(ctx, issueId, identifier, 'agent_started').catch(() => {});

  const { text, error } = await executeAgentRun(ctx, runContext);

  ctx.deps.queue.notifyIdle(runContext.chatJid);

  try {
    if (error) {
      await ctx.client.createComment({
        issueId,
        body: `**Agent run failed**\n\n\`\`\`\n${error}\n\`\`\`\n\n---\n*To retry: move to **Todo**, then to **Ready for Agent**.*`,
      });
      notifyPipelineStep(ctx, issueId, identifier, 'agent_failed', error).catch(
        () => {},
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
      const prUrl = extractPrUrl(body, ctx.repoSlug);
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
    }
  } catch (err) {
    logger.warn(
      { issueId, err },
      'linear-dispatcher: failed to update Linear issue after run',
    );
  } finally {
    ctx.inFlightDispatch.delete(issueId);
  }
}

async function pollLinear(): Promise<void> {
  if (!_ctx) return;
  const ctx = _ctx;

  const readyState = ctx.stateByName.get('Ready for Agent')!;

  const issues = await ctx.client.issues({
    filter: { state: { id: { eq: readyState.id } } },
  });

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

    ctx.inFlightDispatch.add(issue.id);

    const issueComments = await issue.comments();
    const comments = await Promise.all(
      issueComments.nodes.map(async (c) => {
        const user = await c.user;
        return { author: user?.displayName ?? 'Unknown', body: c.body };
      }),
    );

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
    const runContext: RunContext = {
      prompt,
      groupFolder: DISPATCH_GROUP_JID,
      chatJid,
      isControlGroup: true,
      isScheduledTask: true,
      effort: 'high',
    };

    notifyPipelineStep(
      ctx,
      issue.id,
      issue.identifier,
      'agent_dispatched',
    ).catch(() => {});

    ctx.deps.queue.enqueueTask(chatJid, issue.id, () =>
      runIssue(ctx, issue.id, issue.identifier, runContext),
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
      key: 'evaluating' | 'scoped' | 'revise';
      name: string;
      color: string;
    }> = [
      { key: 'evaluating', name: 'Warden: Evaluating', color: '#f59e0b' },
      { key: 'scoped', name: 'Scoped', color: '#16a34a' },
      { key: 'revise', name: 'Warden: Revise', color: '#dc2626' },
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
    try {
      const allLabels = await client.issueLabels();
      const labelMap = new Map(allLabels.nodes.map((l) => [l.name, l.id]));

      for (const def of statusDefs) {
        if (labelMap.has(def.name)) {
          gateLabels[def.key] = labelMap.get(def.name);
        } else {
          const created = await client.createIssueLabel({
            name: def.name,
            color: def.color,
            teamId,
          });
          const label = await created.issueLabel;
          if (label) gateLabels[def.key] = label.id;
        }
      }

      if (labelMap.has('warden:skip')) {
        gateLabels.wardenSkip = labelMap.get('warden:skip');
      }

      for (let i = 1; i <= 5; i++) {
        const eName = `Effort: ${i}`;
        const cName = `Complexity: ${i}`;
        if (labelMap.has(eName)) {
          gateLabels.effort[i] = labelMap.get(eName)!;
        } else {
          const created = await client.createIssueLabel({
            name: eName,
            color: effortColors[i - 1],
            teamId,
          });
          const label = await created.issueLabel;
          if (label) gateLabels.effort[i] = label.id;
        }
        if (labelMap.has(cName)) {
          gateLabels.complexity[i] = labelMap.get(cName)!;
        } else {
          const created = await client.createIssueLabel({
            name: cName,
            color: complexityColors[i - 1],
            teamId,
          });
          const label = await created.issueLabel;
          if (label) gateLabels.complexity[i] = label.id;
        }
      }
    } catch (err) {
      logger.warn({ err }, 'linear: failed to setup gate status labels');
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

  const tick = () => {
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

  tick();
  _timer = setInterval(tick, pollMs);
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
  _ctx = null;
}
