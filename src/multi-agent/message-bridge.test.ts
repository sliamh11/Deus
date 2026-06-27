/**
 * Unit tests for the message↔orchestrator bridge (LIA-127):
 * parseTaskBlock (validation pipeline) and formatMultiAgentResult
 * (pure transform + deliverable-presence artifact verification).
 */
import { describe, it, expect } from 'vitest';

import {
  parseTaskBlock,
  formatMultiAgentResult,
  MALFORMED_TASK_BLOCK,
} from './message-bridge.js';
import type { OrchestratorResult, SubagentTask } from './types.js';

const validBlock = (body: string) => '```deus-tasks\n' + body + '\n```';

const TWO_TASKS = JSON.stringify([
  {
    id: 'a',
    role: 'researcher',
    goal: 'g',
    backstory: '',
    prompt: 'p',
    mode: 'read',
  },
  {
    id: 'synth',
    role: 'writer',
    goal: 'g2',
    backstory: '',
    prompt: 'p2',
    mode: 'read',
    contextFrom: ['a'],
  },
]);

describe('parseTaskBlock', () => {
  it('returns null when no block is present (falls through to single-agent)', () => {
    expect(parseTaskBlock('just a normal prompt with no fence')).toBeNull();
  });

  it('parses a valid block into SubagentTask[]', () => {
    const tasks = parseTaskBlock(`do this\n${validBlock(TWO_TASKS)}`);
    expect(Array.isArray(tasks)).toBe(true);
    expect((tasks as SubagentTask[]).map((t) => t.id)).toEqual(['a', 'synth']);
  });

  it('flags a present-but-invalid-JSON block as malformed', () => {
    expect(parseTaskBlock(validBlock('{not json'))).toBe(MALFORMED_TASK_BLOCK);
  });

  it('an XML-escaped block is malformed — callers MUST parse raw, not formatMessages output', () => {
    // Regression guard (LIA-127): formatMessages XML-escapes content (" → &quot;).
    // Parsing that escaped text fails JSON.parse → MALFORMED. The message loop must
    // feed parseTaskBlock the RAW message content, never the escaped prompt.
    const escaped = TWO_TASKS.replace(/"/g, '&quot;');
    expect(parseTaskBlock(validBlock(escaped))).toBe(MALFORMED_TASK_BLOCK);
  });

  it('flags an empty array as malformed', () => {
    expect(parseTaskBlock(validBlock('[]'))).toBe(MALFORMED_TASK_BLOCK);
  });

  it('rejects a task missing required fields', () => {
    const bad = JSON.stringify([
      { id: 'a', role: 'r', goal: 'g', mode: 'read' },
    ]); // no prompt/backstory
    expect(parseTaskBlock(validBlock(bad))).toBe(MALFORMED_TASK_BLOCK);
  });

  it('rejects an invalid mode', () => {
    const bad = JSON.stringify([
      {
        id: 'a',
        role: 'r',
        goal: 'g',
        backstory: '',
        prompt: 'p',
        mode: 'delete',
      },
    ]);
    expect(parseTaskBlock(validBlock(bad))).toBe(MALFORMED_TASK_BLOCK);
  });

  it('rejects a task id with non-slug characters (chatJid/IPC-key safety)', () => {
    const bad = JSON.stringify([
      {
        id: '../escape',
        role: 'r',
        goal: 'g',
        backstory: '',
        prompt: 'p',
        mode: 'read',
      },
    ]);
    expect(parseTaskBlock(validBlock(bad))).toBe(MALFORMED_TASK_BLOCK);
  });

  it('rejects duplicate task ids', () => {
    const dup = JSON.stringify([
      {
        id: 'a',
        role: 'r',
        goal: 'g',
        backstory: '',
        prompt: 'p',
        mode: 'read',
      },
      {
        id: 'a',
        role: 'r',
        goal: 'g',
        backstory: '',
        prompt: 'p',
        mode: 'read',
      },
    ]);
    expect(parseTaskBlock(validBlock(dup))).toBe(MALFORMED_TASK_BLOCK);
  });

  it('rejects a cyclic contextFrom graph (would deterministically throw in dispatch)', () => {
    const cyclic = JSON.stringify([
      {
        id: 'a',
        role: 'r',
        goal: 'g',
        backstory: '',
        prompt: 'p',
        mode: 'read',
        contextFrom: ['b'],
      },
      {
        id: 'b',
        role: 'r',
        goal: 'g',
        backstory: '',
        prompt: 'p',
        mode: 'read',
        contextFrom: ['a'],
      },
    ]);
    expect(parseTaskBlock(validBlock(cyclic))).toBe(MALFORMED_TASK_BLOCK);
  });

  it('rejects a dangling contextFrom dependency', () => {
    const dangling = JSON.stringify([
      {
        id: 'a',
        role: 'r',
        goal: 'g',
        backstory: '',
        prompt: 'p',
        mode: 'read',
        contextFrom: ['nonexistent'],
      },
    ]);
    expect(parseTaskBlock(validBlock(dangling))).toBe(MALFORMED_TASK_BLOCK);
  });
});

describe('formatMultiAgentResult', () => {
  const tasks: SubagentTask[] = [
    { id: 'a', role: 'r', goal: 'g', backstory: '', prompt: 'p', mode: 'read' },
    { id: 'b', role: 'r', goal: 'g', backstory: '', prompt: 'p', mode: 'read' },
  ];

  it('renders done tasks with their output', () => {
    const res: OrchestratorResult = {
      status: 'success',
      results: [
        { status: 'DONE', output: 'answer A' },
        { status: 'DONE', output: 'answer B' },
      ],
      concerns: [],
    };
    const out = formatMultiAgentResult(res, tasks);
    expect(out).toContain('✓ a: done');
    expect(out).toContain('answer A');
    expect(out).toContain('answer B');
  });

  it('flags a DONE task with EMPTY output as a no-deliverable concern (artifact verification)', () => {
    const res: OrchestratorResult = {
      status: 'success',
      results: [
        { status: 'DONE', output: '   ' }, // explicit DONE but no deliverable
        { status: 'DONE', output: 'real output' },
      ],
      concerns: [],
    };
    const out = formatMultiAgentResult(res, tasks);
    expect(out).toContain('⚠ a: produced no deliverable');
    expect(out).toContain('produced no deliverable'); // also surfaced in concerns
    expect(out).not.toContain('✓ a: done');
  });

  it('renders blocked tasks with their reason', () => {
    const res: OrchestratorResult = {
      status: 'partial',
      results: [
        { status: 'DONE', output: 'ok' },
        { status: 'BLOCKED', output: '', blockedReason: 'missing creds' },
      ],
      concerns: [],
    };
    const out = formatMultiAgentResult(res, tasks);
    expect(out).toContain('✗ b: blocked — missing creds');
  });

  it('surfaces orchestrator concerns', () => {
    const res: OrchestratorResult = {
      status: 'success',
      results: [
        { status: 'DONE_WITH_CONCERNS', output: 'x', concerns: ['flaky'] },
        { status: 'DONE', output: 'y' },
      ],
      concerns: ['flaky'],
    };
    const out = formatMultiAgentResult(res, tasks);
    expect(out).toContain('**Concerns:**');
    expect(out).toContain('- flaky');
  });

  it('strips <internal>...</internal> reasoning before delivery (no leak on the multi-agent path)', () => {
    const res: OrchestratorResult = {
      status: 'success',
      results: [
        {
          status: 'DONE',
          output: '<internal>secret plan</internal>Visible answer A',
        },
        { status: 'DONE', output: 'answer B' },
      ],
      concerns: [],
    };
    const out = formatMultiAgentResult(res, tasks);
    expect(out).toContain('Visible answer A');
    expect(out).not.toContain('secret plan');
    expect(out).not.toContain('<internal>');
  });

  it('treats an internal-only output as no deliverable (nothing visible after stripping)', () => {
    const res: OrchestratorResult = {
      status: 'success',
      results: [
        {
          status: 'DONE',
          output: '<internal>only reasoning, no answer</internal>',
        },
        { status: 'DONE', output: 'real output' },
      ],
      concerns: [],
    };
    const out = formatMultiAgentResult(res, tasks);
    expect(out).toContain('⚠ a: produced no deliverable');
    expect(out).not.toContain('only reasoning');
    expect(out).not.toContain('✓ a: done');
  });
});
