import type { LinearClient } from '@linear/sdk';
import {
  discoverWorkflowStates,
  type WorkflowState,
} from './linear-dispatcher.js';
import { logPipelineEvent } from './db.js';
import { logger } from './logger.js';
import { openBrowser } from './platform.js';

export interface ActionContext {
  client: LinearClient;
  teamId: string;
  stateByName: Map<string, WorkflowState>;
  wardenSkipLabelId: string | null;
  scopedLabelId: string | null;
}

export interface ActionResult {
  ok: boolean;
  message: string;
}

const RERUN_BOUNCE: Record<string, [string, string]> = {
  'Ready for Agent': ['Todo', 'Ready for Agent'],
  'In Review': ['Agent Working', 'In Review'],
};

export async function initActionContext(
  client: LinearClient,
  teamId: string,
): Promise<ActionContext | null> {
  try {
    const stateByName = await discoverWorkflowStates(client, teamId);

    let wardenSkipLabelId: string | null = null;
    let scopedLabelId: string | null = null;
    const labels = await client.issueLabels();
    for (const label of labels.nodes) {
      if (label.name === 'warden:skip') wardenSkipLabelId = label.id;
      if (label.name === 'Scoped') scopedLabelId = label.id;
      if (wardenSkipLabelId && scopedLabelId) break;
    }

    return { client, teamId, stateByName, wardenSkipLabelId, scopedLabelId };
  } catch (err) {
    logger.warn({ err }, 'linear-actions: init failed, actions disabled');
    return null;
  }
}

export function handleOpenInBrowser(
  url: string,
  identifier: string,
): ActionResult {
  const opened = openBrowser(url);
  if (opened) {
    return { ok: true, message: `Opening ${identifier}…` };
  }
  return { ok: true, message: `URL: ${url}` };
}

async function retryWithBackoff<T>(
  fn: () => Promise<T>,
  maxAttempts = 3,
): Promise<T> {
  let lastErr: unknown;
  for (let i = 0; i < maxAttempts; i++) {
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (i < maxAttempts - 1) {
        await new Promise((r) => setTimeout(r, (i + 1) * 2000));
      }
    }
  }
  throw lastErr;
}

export async function toggleWardenSkip(
  ctx: ActionContext,
  issueId: string,
  identifier: string,
  currentLabelIds: string[],
): Promise<ActionResult> {
  if (!ctx.wardenSkipLabelId) {
    return { ok: false, message: 'warden:skip label not in workspace' };
  }
  const hasSkip = currentLabelIds.includes(ctx.wardenSkipLabelId);

  try {
    if (hasSkip) {
      await retryWithBackoff(() =>
        ctx.client.updateIssue(issueId, {
          removedLabelIds: [ctx.wardenSkipLabelId!],
        }),
      );
      logPipelineEvent(
        issueId,
        identifier,
        'label_toggled',
        'warden:skip removed',
      );
      return { ok: true, message: `warden:skip removed from ${identifier}` };
    } else {
      await retryWithBackoff(() =>
        ctx.client.updateIssue(issueId, {
          addedLabelIds: [ctx.wardenSkipLabelId!],
        }),
      );
      logPipelineEvent(
        issueId,
        identifier,
        'label_toggled',
        'warden:skip added',
      );
      return { ok: true, message: `warden:skip added to ${identifier}` };
    }
  } catch (err) {
    return {
      ok: false,
      message: `Label toggle failed: ${err instanceof Error ? err.message : err}`,
    };
  }
}

export async function triggerGateRerun(
  ctx: ActionContext,
  issueId: string,
  identifier: string,
  currentStateName: string,
): Promise<ActionResult> {
  const bounce = RERUN_BOUNCE[currentStateName];
  if (!bounce) {
    return { ok: false, message: `No gate configured for ${currentStateName}` };
  }

  const [intermediate, target] = bounce;
  const intermediateState = ctx.stateByName.get(intermediate);
  const targetState = ctx.stateByName.get(target);

  if (!intermediateState) {
    return { ok: false, message: `State "${intermediate}" not found` };
  }
  if (!targetState) {
    return { ok: false, message: `State "${target}" not found` };
  }

  try {
    await ctx.client.updateIssue(issueId, { stateId: intermediateState.id });
    logPipelineEvent(
      issueId,
      identifier,
      'state_changed',
      `→ ${intermediate} (gate rerun)`,
    );

    await new Promise((r) => setTimeout(r, 500));

    await ctx.client.updateIssue(issueId, { stateId: targetState.id });
    logPipelineEvent(
      issueId,
      identifier,
      'state_changed',
      `→ ${target} (gate rerun)`,
    );

    return { ok: true, message: `Gate re-run triggered for ${identifier}` };
  } catch (err) {
    return {
      ok: false,
      message: `Gate re-run failed: ${err instanceof Error ? err.message : err}`,
    };
  }
}

export async function moveIssueState(
  ctx: ActionContext,
  issueId: string,
  identifier: string,
  toStateName: string,
): Promise<ActionResult> {
  const state =
    ctx.stateByName.get(toStateName) ??
    [...ctx.stateByName.values()].find(
      (s) => s.name.toLowerCase() === toStateName.toLowerCase(),
    );
  if (!state) {
    const valid = [...ctx.stateByName.keys()].join(', ');
    return {
      ok: false,
      message: `State "${toStateName}" not found. Valid: ${valid}`,
    };
  }

  try {
    await ctx.client.updateIssue(issueId, { stateId: state.id });
    logPipelineEvent(issueId, identifier, 'state_changed', `→ ${state.name}`);
    return { ok: true, message: `${identifier} → ${state.name}` };
  } catch (err) {
    return {
      ok: false,
      message: `Move failed: ${err instanceof Error ? err.message : err}`,
    };
  }
}

export const STARTABLE_STATES = new Set(['Backlog', 'Todo']);

export async function startIssueOrchestration(
  ctx: ActionContext,
  issueId: string,
  identifier: string,
  currentState: string,
): Promise<ActionResult> {
  if (!STARTABLE_STATES.has(currentState)) {
    return {
      ok: false,
      message: `Can only start from Backlog/Todo (current: ${currentState})`,
    };
  }

  const rfaState = ctx.stateByName.get('Ready for Agent');
  if (!rfaState) {
    return { ok: false, message: 'Ready for Agent state not found' };
  }

  try {
    if (currentState === 'Backlog') {
      const todoState = ctx.stateByName.get('Todo');
      if (todoState) {
        await ctx.client.updateIssue(issueId, { stateId: todoState.id });
        logPipelineEvent(
          issueId,
          identifier,
          'state_changed',
          '→ Todo (start orchestration)',
        );
      }
    }

    const updatePayload: { stateId: string; addedLabelIds?: string[] } = {
      stateId: rfaState.id,
    };
    if (ctx.scopedLabelId) {
      updatePayload.addedLabelIds = [ctx.scopedLabelId];
    }

    await ctx.client.updateIssue(issueId, updatePayload);
    logPipelineEvent(
      issueId,
      identifier,
      'state_changed',
      '→ Ready for Agent (start orchestration)',
    );

    return {
      ok: true,
      message: `${identifier} → Ready for Agent${ctx.scopedLabelId ? ' + Scoped' : ''}`,
    };
  } catch (err) {
    return {
      ok: false,
      message: `Start failed: ${err instanceof Error ? err.message : err}`,
    };
  }
}
