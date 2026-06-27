/**
 * Pure message-layer ↔ MultiAgentOrchestrator helpers (no I/O).
 *  - parseTaskBlock returns SubagentTask[] | null | MALFORMED_TASK_BLOCK — null means
 *    NO block (caller falls through to single-agent), MALFORMED means present-but-invalid
 *    (caller surfaces a notice instead of silently dropping or re-feeding raw JSON).
 *  - formatMultiAgentResult does deliverable-presence artifact verification: a DONE task
 *    with empty output renders as a concern, never a silent success.
 */
import { stripInternalTags } from '../router.js';
import type { OrchestratorResult, SubagentTask } from './types.js';

/** Sentinel: a deus-tasks fence was present but could not be parsed/validated. */
export const MALFORMED_TASK_BLOCK = 'malformed' as const;

const TASK_BLOCK_RE = /```deus-tasks\s*\n([\s\S]*?)```/;

const VALID_MODES = new Set(['read', 'write']);

function isValidTask(value: unknown): value is SubagentTask {
  if (typeof value !== 'object' || value === null) return false;
  const t = value as Record<string, unknown>;
  // id is a slug: it flows into chatJid / IPC keys downstream, so constrain the
  // charset to close any path/key-injection surface before it can matter.
  if (typeof t.id !== 'string' || !/^[A-Za-z0-9_-]+$/.test(t.id)) return false;
  if (typeof t.role !== 'string' || !t.role.trim()) return false;
  if (typeof t.goal !== 'string' || !t.goal.trim()) return false;
  if (typeof t.prompt !== 'string' || !t.prompt.trim()) return false;
  if (typeof t.mode !== 'string' || !VALID_MODES.has(t.mode)) return false;
  if (typeof t.backstory !== 'string') return false; // required by type; empty allowed
  if (t.contextFrom !== undefined) {
    if (!Array.isArray(t.contextFrom)) return false;
    if (!t.contextFrom.every((c) => typeof c === 'string')) return false;
  }
  return true;
}

/**
 * Parse a single ```deus-tasks fenced JSON block into a validated SubagentTask[].
 *
 * Returns:
 *  - SubagentTask[]        when a block is present and fully valid
 *  - null                  when NO block is present (caller falls through to single-agent)
 *  - MALFORMED_TASK_BLOCK  when a block IS present but invalid (caller surfaces a notice)
 */
export function parseTaskBlock(
  prompt: string,
): SubagentTask[] | null | typeof MALFORMED_TASK_BLOCK {
  // Fast pre-check: skip the O(n) regex on the common no-block prompt.
  if (!prompt.includes('deus-tasks')) return null;
  const match = TASK_BLOCK_RE.exec(prompt);
  if (!match) return null; // no block → single-agent path

  let parsed: unknown;
  try {
    parsed = JSON.parse(match[1].trim());
  } catch {
    return MALFORMED_TASK_BLOCK;
  }

  if (!Array.isArray(parsed) || parsed.length === 0)
    return MALFORMED_TASK_BLOCK;
  if (!parsed.every(isValidTask)) return MALFORMED_TASK_BLOCK;

  const tasks = parsed as SubagentTask[];

  // Cross-reference validation: unique ids, and every contextFrom id resolves.
  const ids = new Set<string>();
  for (const t of tasks) {
    if (ids.has(t.id)) return MALFORMED_TASK_BLOCK; // duplicate id
    ids.add(t.id);
  }
  for (const t of tasks) {
    for (const dep of t.contextFrom ?? []) {
      if (!ids.has(dep)) return MALFORMED_TASK_BLOCK; // dangling dependency
    }
  }

  // Reject cyclic contextFrom graphs. dispatch's topologicalSort would throw a
  // deterministic UserError on a cycle; catching it here makes a cyclic block a
  // parse-time MALFORMED notice (consumed) instead of an infinite dispatch-retry
  // loop. DFS three-color cycle detection.
  const adj = new Map(tasks.map((t) => [t.id, t.contextFrom ?? []]));
  const color = new Map<string, 0 | 1 | 2>(); // 0=unvisited 1=visiting 2=done
  const hasCycle = (id: string): boolean => {
    const c = color.get(id) ?? 0;
    if (c === 1) return true;
    if (c === 2) return false;
    color.set(id, 1);
    for (const dep of adj.get(id) ?? []) {
      if (hasCycle(dep)) return true;
    }
    color.set(id, 2);
    return false;
  };
  for (const t of tasks) {
    if (hasCycle(t.id)) return MALFORMED_TASK_BLOCK;
  }

  return tasks;
}

/**
 * Render an OrchestratorResult into one user-facing reply.
 *
 * Artifact verification (deliverable presence): a task reporting DONE /
 * DONE_WITH_CONCERNS but with empty output is shown as a concern ("produced no
 * deliverable"), never a silent ✓.
 *
 * `results` are positionally aligned to `tasks` (orchestrator aggregation maps
 * tasks.map(t => results.get(t.id))), so index i pairs task i with result i.
 */
export function formatMultiAgentResult(
  res: OrchestratorResult,
  tasks: SubagentTask[],
): string {
  const lines: string[] = [];
  const concerns = [...res.concerns];

  // Strip <internal>...</internal> reasoning before anything reaches the user —
  // mirrors the single-agent outbound path (message-orchestrator.ts) so the
  // multi-agent path cannot leak internal content. A task whose output is
  // ENTIRELY internal then reads as "produced no deliverable".
  const visible = res.results.map((r) => stripInternalTags(r.output));

  res.results.forEach((r, i) => {
    const task = tasks[i];
    const label = task?.id ?? `task-${i + 1}`;
    const noDeliverable =
      (r.status === 'DONE' || r.status === 'DONE_WITH_CONCERNS') &&
      !visible[i].trim();

    if (r.status === 'BLOCKED') {
      lines.push(
        `✗ ${label}: blocked — ${r.blockedReason ?? 'no reason given'}`,
      );
    } else if (noDeliverable) {
      // A claimed-DONE task that emitted nothing is surfaced, never a silent ✓.
      lines.push(`⚠ ${label}: produced no deliverable`);
      concerns.push(`${label}: produced no deliverable`);
    } else if (r.status === 'DONE_WITH_CONCERNS') {
      lines.push(`⚠ ${label}: done with concerns`);
    } else {
      lines.push(`✓ ${label}: done`);
    }
  });

  const header = lines.join('\n');

  const outputs = res.results
    .map((r, i) => ({ r, out: visible[i], task: tasks[i] }))
    .filter(({ r, out }) => r.status !== 'BLOCKED' && out.trim())
    .map(({ out, task }) => `### ${task?.id ?? 'task'}\n${out.trim()}`)
    .join('\n\n');

  const concernBlock = concerns.length
    ? `\n\n**Concerns:**\n${concerns.map((c) => `- ${c}`).join('\n')}`
    : '';

  return [header, outputs].filter(Boolean).join('\n\n') + concernBlock;
}
