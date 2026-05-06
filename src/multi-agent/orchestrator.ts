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
import { UserError } from '../errors/index.js';
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

function parseStatusMarker(
  rawOutput: string,
  taskId: string,
): Pick<SubagentResult, 'status' | 'concerns' | 'blockedReason'> & {
  cleanOutput: string;
} {
  const tail = rawOutput.slice(-200);
  const match = STATUS_MARKER_RE.exec(tail);

  if (!match) {
    logger.info(
      { taskId },
      'No status marker found in subagent output — defaulting to DONE',
    );
    return { status: 'DONE', cleanOutput: rawOutput };
  }

  const markerBody = match[1];
  const markerStart = rawOutput.length - 200 + (match.index ?? 0);
  const actualStart = Math.max(0, markerStart);
  const cleanOutput = (
    rawOutput.slice(0, actualStart) +
    rawOutput.slice(actualStart).replace(match[0], '')
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
    const runContext = {
      prompt,
      groupFolder: group.folder,
      chatJid: `multi-agent-${task.id}`,
      isControlGroup: false,
      isScheduledTask: false,
    };

    const outputParts: string[] = [];

    const eventSink: RuntimeEventSink = async (event: RuntimeEvent) => {
      if (event.type === 'output_text') {
        outputParts.push(event.text);
      }
      if (event.type === 'error') {
        throw new Error(event.error);
      }
    };

    const session = await runtime.startOrResume(runContext);
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
  }
}
