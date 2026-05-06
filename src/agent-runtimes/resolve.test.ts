import { describe, expect, it } from 'vitest';

import { resolveAgentRuntime } from './resolve.js';
import type { RegisteredGroup, ScheduledTask } from '../types.js';

function makeGroup(overrides: Partial<RegisteredGroup> = {}): RegisteredGroup {
  return {
    name: 'Test Group',
    folder: 'test-group',
    trigger: '@Deus',
    added_at: '2026-04-22T00:00:00.000Z',
    ...overrides,
  };
}

function makeTask(overrides: Partial<ScheduledTask> = {}): ScheduledTask {
  return {
    id: 'task-1',
    group_folder: 'test-group',
    chat_jid: 'group@g.us',
    prompt: 'Run something',
    schedule_type: 'once',
    schedule_value: '2026-04-22T10:00:00',
    context_mode: 'group',
    next_run: null,
    last_run: null,
    last_result: null,
    status: 'active',
    created_at: '2026-04-22T00:00:00.000Z',
    ...overrides,
  };
}

describe('resolveAgentRuntime', () => {
  it('prefers the scheduled task backend override', () => {
    const group = makeGroup({
      containerConfig: { agentBackend: 'claude' },
    });
    const task = makeTask({ agent_backend: 'openai' });

    expect(resolveAgentRuntime(group, task)).toBe('openai');
  });

  it('falls back to the group backend override', () => {
    const group = makeGroup({
      containerConfig: { agentBackend: 'openai' },
    });

    expect(resolveAgentRuntime(group)).toBe('openai');
  });

  it('falls back to the global default when no override exists', () => {
    expect(resolveAgentRuntime(makeGroup())).toBe('claude');
  });
});
