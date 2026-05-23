import fs from 'fs';
import path from 'path';
import { LinearClient } from '@linear/sdk';
import { DATA_DIR } from './config.js';
import { parse as parseYaml } from 'yaml';
import { logger } from './logger.js';
import { PROJECT_ROOT } from './config.js';
import { FatalError, RetryableError } from './errors/index.js';
import { fireAndForget } from './async/index.js';
import { extractPrUrl } from './pr-url-extractor.js';
import { upsertIssuePr } from './db.js';
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

export function buildScopedIssuePrompt(
  issueTitle: string,
  issueIdentifier: string,
  issueDescription: string,
  comments: Array<{ author: string; body: string }>,
): string {
  const parts = [
    `<task>\nYou are an autonomous software engineer. Implement the following issue.\nTitle: ${issueTitle}\nID: ${issueIdentifier}\n\n${issueDescription}\n</task>`,
  ];
  if (comments.length > 0) {
    const commentBlock = comments
      .map((c) => `[${c.author}]: ${c.body}`)
      .join('\n\n');
    parts.push(`<comments>\n${commentBlock}\n</comments>`);
  }
  const hasAgentHistory = comments.some(
    (c) =>
      c.body.includes('Agent run failed') ||
      c.body.includes('**Auto-merged**') ||
      c.body.includes('**Auto-merge failed**') ||
      c.body.includes('**Auto-merge blocked**') ||
      c.body.includes('PR URL in your response'),
  );

  parts.push(
    `<instructions>
${hasAgentHistory ? 'A previous agent attempt exists. Review the comments above — check what was already committed, pushed, or opened as a PR. Continue from where the last attempt stopped. Do not redo completed steps. If CI failed, check out the existing branch, read the CI logs, fix the failures, and push.\n\n' : ''}Read the scope block in the task description. Follow the implementation plan and satisfy all acceptance criteria.

## Git Workflow
1. Create a feature branch: \`git checkout -b feat/${issueIdentifier.toLowerCase().replace(/[^a-z0-9-]/g, '')}-<short-desc>\`
2. Implement the changes, run tests, commit.
3. Push the branch using the tool proxy:
   curl -s -X POST "$DEUS_TOOL_PROXY_URL/tool/deus-git-push" \\
     -H "Content-Type: application/json" \\
     -H "x-deus-proxy-token: $DEUS_PROXY_TOKEN" \\
     -d '{"args": ["-u", "origin", "HEAD"]}'
4. Create a PR using the tool proxy:
   curl -s -X POST "$DEUS_TOOL_PROXY_URL/tool/gh" \\
     -H "Content-Type: application/json" \\
     -H "x-deus-proxy-token: $DEUS_PROXY_TOKEN" \\
     -d '{"args": ["pr", "create", "--fill", "--body", "Closes ${issueIdentifier.replace(/[^A-Za-z0-9-]/g, '')}"]}'
5. Report what you did and include the PR URL in your response.
</instructions>`,
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
      const backlogState = ctx.stateByName.get('Backlog')!;
      await ctx.client.updateIssue(issueId, { stateId: backlogState.id });
      logger.warn(
        { issueId },
        'linear-dispatcher: moved failed issue to Backlog',
      );
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

    let prompt: string;
    if (roleSpec) {
      prompt = buildIssuePrompt(
        roleSpec,
        issue.title,
        issue.identifier,
        issue.description ?? undefined,
        comments,
      );
    } else {
      prompt = buildScopedIssuePrompt(
        issue.title,
        issue.identifier,
        issue.description ?? '',
        comments,
      );
    }

    const chatJid = `linear-dispatch-${issue.id.slice(0, 8)}`;
    const runContext: RunContext = {
      prompt,
      groupFolder: DISPATCH_GROUP_JID,
      chatJid,
      isControlGroup: false,
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
