/**
 * Thin multi-agent orchestrator — dispatches parallel container runs for
 * read-heavy tasks, behind `DEUS_MULTI_AGENT=1` env flag.
 *
 * Topologically sorts subagent tasks by their `contextFrom` dependencies,
 * executes independent tasks in parallel (capped by `maxParallel`), and
 * injects prior outputs into downstream prompts via prompt-templates.
 *
 * See docs/decisions/multi-agent-orchestration-research.md for design rationale.
 */

import type { RuntimeRegistry } from '../agent-runtimes/registry.js';
import type {
  AgentRuntime,
  RuntimeEvent,
  RuntimeEventSink,
} from '../agent-runtimes/types.js';
import * as fs from 'fs';
import * as path from 'path';

import { UserError } from '../errors/index.js';
import { resolveGroupIpcPath } from '../group-folder.js';
import { logger } from '../logger.js';
import { buildPrompt } from './prompt-templates.js';
import type { RegisteredGroup } from '../types.js';
import type {
  OrchestratorResult,
  SubagentResult,
  SubagentTask,
} from './types.js';

const STATUS_MARKER_RE =
  /\[STATUS:(DONE_WITH_CONCERNS:[^\]]*|DONE|BLOCKED:[^\]]*)\]/;

/** Synthetic concern raised when a sub-agent omits its instructed status marker. */
const NO_MARKER_CONCERN = 'no [STATUS] marker emitted — completion unverified';

function parseStatusMarker(
  rawOutput: string,
  taskId: string,
): Pick<SubagentResult, 'status' | 'concerns' | 'blockedReason'> & {
  cleanOutput: string;
} {
  // Scan the FULL output for the LAST [STATUS:...] marker. buildPrompt instructs the
  // agent to end with exactly one marker; last-match is robust to trailing chatter that
  // the previous tail-200 window silently dropped. STATUS_MARKER_RE has no `g` flag, so
  // a fresh global copy is required for matchAll (a non-global regex never advances
  // lastIndex). Same edge as the old tail-window: a post-marker quoted marker wins.
  const matches = [
    ...rawOutput.matchAll(new RegExp(STATUS_MARKER_RE.source, 'g')),
  ];
  const match = matches[matches.length - 1];

  if (!match) {
    const trimmed = rawOutput.trim();
    // The agent was told to emit a marker and did not. Never assume success — the old
    // silent-DONE default masked truncated, errored, or non-compliant runs.
    if (!trimmed) {
      logger.warn(
        { taskId },
        'Subagent produced no output and no status marker — treating as BLOCKED',
      );
      return {
        status: 'BLOCKED',
        cleanOutput: '',
        blockedReason: 'no deliverable and no status marker',
      };
    }
    logger.info(
      { taskId },
      'No status marker in subagent output — DONE_WITH_CONCERNS (unverified)',
    );
    return {
      status: 'DONE_WITH_CONCERNS',
      concerns: [NO_MARKER_CONCERN],
      cleanOutput: trimmed,
    };
  }

  const markerBody = match[1];
  const idx = match.index ?? 0;
  const cleanOutput = (
    rawOutput.slice(0, idx) + rawOutput.slice(idx + match[0].length)
  ).trim();

  if (markerBody === 'DONE') {
    return { status: 'DONE', cleanOutput };
  }

  if (markerBody.startsWith('DONE_WITH_CONCERNS:')) {
    const concernsRaw = markerBody.slice('DONE_WITH_CONCERNS:'.length);
    const concerns = concernsRaw
      .split(';')
      .map((c) => c.trim())
      .filter(Boolean);
    return { status: 'DONE_WITH_CONCERNS', concerns, cleanOutput };
  }

  if (markerBody.startsWith('BLOCKED:')) {
    const reason = markerBody.slice('BLOCKED:'.length).trim();
    return { status: 'BLOCKED', blockedReason: reason, cleanOutput };
  }

  // Fallback (should not reach here given the regex)
  return { status: 'DONE', cleanOutput: rawOutput };
}

// Throws UserError on circular dependencies.
function topologicalSort(tasks: SubagentTask[]): string[][] {
  const taskMap = new Map<string, SubagentTask>();
  const inDegree = new Map<string, number>();
  const adjacency = new Map<string, string[]>();

  for (const task of tasks) {
    taskMap.set(task.id, task);
    inDegree.set(task.id, 0);
    adjacency.set(task.id, []);
  }

  for (const task of tasks) {
    if (task.contextFrom) {
      for (const depId of task.contextFrom) {
        if (!taskMap.has(depId)) {
          // Dependency references a non-existent task — treat as error
          throw new UserError(
            `Task "${task.id}" depends on non-existent task "${depId}"`,
            { context: { taskId: task.id, depId } },
          );
        }
        // depId → task.id edge (depId must finish before task.id)
        adjacency.get(depId)!.push(task.id);
        inDegree.set(task.id, (inDegree.get(task.id) ?? 0) + 1);
      }
    }
  }

  const tiers: string[][] = [];
  let queue = tasks.filter((t) => inDegree.get(t.id) === 0).map((t) => t.id);
  let processed = 0;

  while (queue.length > 0) {
    tiers.push([...queue]);
    processed += queue.length;

    const nextQueue: string[] = [];
    for (const nodeId of queue) {
      for (const neighbor of adjacency.get(nodeId) ?? []) {
        const newDegree = (inDegree.get(neighbor) ?? 1) - 1;
        inDegree.set(neighbor, newDegree);
        if (newDegree === 0) {
          nextQueue.push(neighbor);
        }
      }
    }
    queue = nextQueue;
  }

  if (processed < tasks.length) {
    const remaining = tasks
      .filter((t) => (inDegree.get(t.id) ?? 0) > 0)
      .map((t) => t.id);
    throw new UserError('Circular dependency in subagent tasks', {
      context: { taskIds: remaining },
    });
  }

  return tiers;
}

export class MultiAgentOrchestrator {
  private registry: RuntimeRegistry;
  private maxParallel: number;

  constructor(registry: RuntimeRegistry, maxParallel: number = 3) {
    this.registry = registry;
    this.maxParallel = maxParallel;
  }

  async dispatch(
    tasks: SubagentTask[],
    group: RegisteredGroup,
  ): Promise<OrchestratorResult> {
    const runtime = this.registry.resolve(group);
    const tiers = topologicalSort(tasks);
    const taskMap = new Map(tasks.map((t) => [t.id, t]));
    const results = new Map<string, SubagentResult>();
    const allConcerns: string[] = [];

    for (const tier of tiers) {
      // Check for tasks whose dependencies failed/blocked — mark them BLOCKED
      // without launching a runtime call.
      const runnableTasks: string[] = [];
      for (const taskId of tier) {
        const task = taskMap.get(taskId)!;
        const blockedDep = task.contextFrom?.find((depId) => {
          const depResult = results.get(depId);
          return depResult?.status === 'BLOCKED';
        });

        if (blockedDep) {
          results.set(taskId, {
            status: 'BLOCKED',
            output: '',
            blockedReason: `dependency ${blockedDep} failed`,
          });
        } else {
          runnableTasks.push(taskId);
        }
      }

      // Execute runnable tasks in batches of maxParallel
      for (let i = 0; i < runnableTasks.length; i += this.maxParallel) {
        const batch = runnableTasks.slice(i, i + this.maxParallel);
        const settled = await Promise.allSettled(
          batch.map((taskId) =>
            this.executeTask(taskMap.get(taskId)!, group, runtime, results),
          ),
        );

        for (let j = 0; j < batch.length; j++) {
          const taskId = batch[j];
          const outcome = settled[j];

          if (outcome.status === 'fulfilled') {
            results.set(taskId, outcome.value);
            if (outcome.value.concerns?.length) {
              allConcerns.push(...outcome.value.concerns);
            }
          } else {
            // Promise rejected — treat as BLOCKED
            results.set(taskId, {
              status: 'BLOCKED',
              output: '',
              blockedReason:
                outcome.reason instanceof Error
                  ? outcome.reason.message
                  : String(outcome.reason),
            });
          }
        }
      }
    }

    // Aggregate final status
    const allResults = tasks.map((t) => results.get(t.id)!);
    const doneCount = allResults.filter(
      (r) => r.status === 'DONE' || r.status === 'DONE_WITH_CONCERNS',
    ).length;
    const blockedCount = allResults.filter(
      (r) => r.status === 'BLOCKED',
    ).length;

    let status: OrchestratorResult['status'];
    if (blockedCount === 0) {
      status = 'success';
    } else if (doneCount > 0) {
      status = 'partial';
    } else {
      status = 'error';
    }

    return {
      status,
      results: allResults,
      concerns: allConcerns,
    };
  }

  private async executeTask(
    task: SubagentTask,
    group: RegisteredGroup,
    runtime: AgentRuntime,
    priorOutputs: Map<string, SubagentResult>,
  ): Promise<SubagentResult> {
    const prompt = buildPrompt(task, priorOutputs);
    const chatJid = `multi-agent-${task.id}`;
    const runContext = {
      prompt,
      groupFolder: group.folder,
      chatJid,
      isControlGroup: false,
      isScheduledTask: false,
      // Per-task IPC namespace (slug-only id → safe, unique key) so concurrent
      // sibling subagents don't collide on the one-shot `_close` sentinel.
      ipcRunKey: chatJid,
    };

    const outputParts: string[] = [];

    const eventSink: RuntimeEventSink = async (event: RuntimeEvent) => {
      if (event.type === 'output_text') {
        outputParts.push(event.text);
      }
      if (event.type === 'error') {
        throw new Error(event.error);
      }
      // One-shot: signal the container to exit after its first result. Without
      // this the container idles until IDLE_TIMEOUT (~30s) before runTurn
      // resolves (container-runner resolves on exit). Mirrors the single-agent
      // path and linear-dispatcher.executeAgentRun. Best-effort.
      if (event.type === 'turn_complete') {
        try {
          const inputDir = path.join(
            resolveGroupIpcPath(group.folder, chatJid),
            'input',
          );
          fs.mkdirSync(inputDir, { recursive: true });
          fs.writeFileSync(path.join(inputDir, '_close'), '');
        } catch {
          /* best-effort — close sentinel is an optimization, not correctness */
        }
      }
    };

    const session = await runtime.startOrResume(runContext);
    try {
      const runResult = await runtime.runTurn(runContext, session, eventSink);

      const rawOutput = outputParts.join('');
      const parsed = parseStatusMarker(rawOutput, task.id);

      if (runResult.status === 'error') {
        return {
          status: 'BLOCKED',
          output: rawOutput,
          blockedReason: runResult.error ?? 'Runtime error',
        };
      }

      return {
        status: parsed.status,
        output: parsed.cleanOutput,
        concerns: parsed.concerns,
        blockedReason: parsed.blockedReason,
      };
    } finally {
      await runtime.close(session).catch((err) => {
        logger.warn(
          { err, taskId: task.id },
          'Failed to close subagent session',
        );
      });
    }
  }
}
