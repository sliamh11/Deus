import fs from 'fs';
import path from 'path';
import { LinearClient } from '@linear/sdk';
import { parse as parseYaml } from 'yaml';
import { logger } from './logger.js';
import { PROJECT_ROOT } from './config.js';
import { FatalError, RetryableError } from './errors/index.js';
import { fireAndForget } from './async/index.js';
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

interface WorkflowState {
  id: string;
  name: string;
}

export interface LinearContext {
  client: LinearClient;
  stateByName: Map<string, WorkflowState>;
  stateById: Map<string, WorkflowState>;
  botUserId: string;
  deps: LinearDispatcherDependencies;
  dispatchGroup: RegisteredGroup;
  inFlightDispatch: Set<string>;
  inFlightGate: Set<string>;
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

async function discoverWorkflowStates(
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
): string {
  const parts = [
    `<role>\n${role.content}\n</role>`,
    `<issue>\nTitle: ${issueTitle}\nID: ${issueIdentifier}\n\n${issueDescription ?? '(no description)'}\n</issue>`,
  ];
  if (comments.length > 0) {
    const commentBlock = comments
      .map((c) => `[${c.author}]: ${c.body}`)
      .join('\n\n');
    parts.push(`<comments>\n${commentBlock}\n</comments>`);
  }
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
  runContext: RunContext,
): Promise<void> {
  const { text, error } = await executeAgentRun(ctx, runContext);

  ctx.deps.queue.notifyIdle(runContext.chatJid);

  try {
    if (error) {
      await ctx.client.createComment({
        issueId,
        body: `**Agent run failed**\n\n\`\`\`\n${error}\n\`\`\``,
      });
      const backlogState = ctx.stateByName.get('Backlog')!;
      await ctx.client.updateIssue(issueId, { stateId: backlogState.id });
      logger.warn(
        { issueId },
        'linear-dispatcher: moved failed issue to Backlog',
      );
    } else {
      const body = text || '(no output produced)';
      await ctx.client.createComment({ issueId, body });
      const reviewState = ctx.stateByName.get('In Review')!;
      await ctx.client.updateIssue(issueId, { stateId: reviewState.id });
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
  const workingState = ctx.stateByName.get('Agent Working')!;

  const issues = await ctx.client.issues({
    filter: { state: { id: { eq: readyState.id } } },
  });

  for (const issue of issues.nodes) {
    if (ctx.inFlightDispatch.has(issue.id)) continue;

    const labels = await issue.labels();
    const agentLabel = labels.nodes.find((l) => l.name.startsWith('agent:'));
    if (!agentLabel) {
      logger.debug(
        { issueId: issue.id },
        'linear-dispatcher: issue has no agent:* label, skipping',
      );
      continue;
    }

    const roleSpec = _roleSpecs.get(agentLabel.name);
    if (!roleSpec) {
      logger.warn(
        { issueId: issue.id, label: agentLabel.name },
        'linear-dispatcher: no role spec found for label, skipping',
      );
      continue;
    }

    try {
      await ctx.client.updateIssue(issue.id, { stateId: workingState.id });
    } catch (err) {
      logger.warn(
        { issueId: issue.id, err },
        'linear-dispatcher: failed to move issue to Agent Working',
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

    const prompt = buildIssuePrompt(
      roleSpec,
      issue.title,
      issue.identifier,
      issue.description ?? undefined,
      comments,
    );

    const chatJid = `linear-dispatch-${issue.id.slice(0, 8)}`;
    const runContext: RunContext = {
      prompt,
      groupFolder: DISPATCH_GROUP_JID,
      chatJid,
      isControlGroup: false,
      isScheduledTask: true,
      effort: 'high',
    };

    ctx.deps.queue.enqueueTask(chatJid, issue.id, () =>
      runIssue(ctx, issue.id, runContext),
    );

    logger.info(
      { issueId: issue.id, role: roleSpec.name, chatJid },
      'linear-dispatcher: enqueued issue for agent run',
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
    const dispatchGroup: RegisteredGroup = existing || {
      name: 'Linear Dispatch',
      folder: DISPATCH_GROUP_JID,
      trigger: '',
      added_at: new Date().toISOString(),
      requiresTrigger: false,
      isControlGroup: false,
    };
    if (!existing) {
      deps.registerGroup(DISPATCH_GROUP_JID, dispatchGroup);
    }

    logger.info(
      { teamId, states: [...stateByName.keys()] },
      'linear: context initialized',
    );

    return {
      client,
      stateByName,
      stateById,
      botUserId,
      deps,
      dispatchGroup,
      inFlightDispatch: new Set(),
      inFlightGate: new Set(),
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
