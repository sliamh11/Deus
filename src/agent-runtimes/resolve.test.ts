import { describe, expect, it, afterEach, vi } from 'vitest';

import { resolveAgentRuntime, resolveAgentEffort } from './resolve.js';
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

  it('resolves the llama-cpp task override', () => {
    const group = makeGroup({
      containerConfig: { agentBackend: 'claude' },
    });
    const task = makeTask({ agent_backend: 'llama-cpp' });

    expect(resolveAgentRuntime(group, task)).toBe('llama-cpp');
  });

  it('resolves the llama-cpp group override', () => {
    const group = makeGroup({
      containerConfig: { agentBackend: 'llama-cpp' },
    });

    expect(resolveAgentRuntime(group)).toBe('llama-cpp');
  });
});

describe('resolveAgentEffort', () => {
  afterEach(() => {
    delete process.env.DEUS_AGENT_EFFORT;
  });

  it('prefers the scheduled task effort override', () => {
    const group = makeGroup({
      containerConfig: { agentEffort: 'medium' },
    });
    const task = makeTask({ agent_effort: 'max' });

    expect(resolveAgentEffort(group, task)).toBe('max');
  });

  it('falls back to the group effort override', () => {
    const group = makeGroup({
      containerConfig: { agentEffort: 'high' },
    });

    expect(resolveAgentEffort(group)).toBe('high');
  });

  it('falls back to DEUS_AGENT_EFFORT env var', () => {
    process.env.DEUS_AGENT_EFFORT = 'medium';
    expect(resolveAgentEffort(makeGroup())).toBe('medium');
  });

  it('ignores invalid env var values', () => {
    process.env.DEUS_AGENT_EFFORT = 'turbo';
    expect(resolveAgentEffort(makeGroup())).toBe('low');
  });

  it('is case-insensitive for env var', () => {
    process.env.DEUS_AGENT_EFFORT = 'HIGH';
    expect(resolveAgentEffort(makeGroup())).toBe('high');
  });

  it('defaults to low when no override exists', () => {
    expect(resolveAgentEffort(makeGroup())).toBe('low');
  });
});
