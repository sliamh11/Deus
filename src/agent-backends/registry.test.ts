import { describe, it, expect, beforeEach } from 'vitest';

import { BackendRegistry } from './registry.js';
import type {
  AgentBackend,
  AgentBackendName,
  BackendCapabilities,
  BackendSessionRef,
  RunContext,
  RunResult,
  RuntimeEventSink,
} from './types.js';
import type { RegisteredGroup } from '../types.js';

function stubBackend(backendName: AgentBackendName): AgentBackend {
  return {
    name: () => backendName,
    capabilities: (): BackendCapabilities => ({
      shell: true,
      filesystem: true,
      web: true,
      multimodal: false,
      handoffs: false,
      persistent_sessions: true,
      tool_streaming: false,
    }),
    startOrResume: async () => ({
      backend: backendName,
      session_id: '',
    }),
    runTurn: async (
      _ctx: RunContext,
      _ref: BackendSessionRef,
      _sink: RuntimeEventSink,
    ): Promise<RunResult> => ({
      status: 'success',
      result: null,
    }),
    close: async () => {},
  };
}

function stubGroup(overrides: Partial<RegisteredGroup> = {}): RegisteredGroup {
  return {
    name: 'test-group',
    folder: 'test-folder',
    trigger: 'deus',
    added_at: new Date().toISOString(),
    ...overrides,
  };
}

describe('BackendRegistry', () => {
  let registry: BackendRegistry;

  beforeEach(() => {
    registry = new BackendRegistry();
  });

  it('registers and retrieves a backend', () => {
    const claude = stubBackend('claude');
    registry.register(claude);

    expect(registry.get('claude')).toBe(claude);
    expect(registry.has('claude')).toBe(true);
  });

  it('throws on unknown backend', () => {
    expect(() => registry.get('openai')).toThrow(/No backend registered/);
  });

  it('lists registered backends', () => {
    registry.register(stubBackend('claude'));
    registry.register(stubBackend('openai'));

    expect(registry.list()).toEqual(
      expect.arrayContaining(['claude', 'openai']),
    );
    expect(registry.list()).toHaveLength(2);
  });

  it('resolves backend from group config', () => {
    registry.register(stubBackend('claude'));
    registry.register(stubBackend('openai'));

    const group = stubGroup({
      containerConfig: { agentBackend: 'openai' },
    });

    const backend = registry.resolve(group);
    expect(backend.name()).toBe('openai');
  });

  it('resolves to default when group has no override', () => {
    registry.register(stubBackend('claude'));

    const group = stubGroup();
    const backend = registry.resolve(group);
    expect(backend.name()).toBe('claude');
  });

  it('task override takes precedence over group config', () => {
    registry.register(stubBackend('claude'));
    registry.register(stubBackend('openai'));

    const group = stubGroup({
      containerConfig: { agentBackend: 'claude' },
    });
    const task = {
      id: 'task-1',
      group_folder: 'test-folder',
      chat_jid: 'test@g.us',
      prompt: 'test',
      schedule_type: 'once' as const,
      schedule_value: '',
      context_mode: 'isolated' as const,
      next_run: null,
      last_run: null,
      last_result: null,
      status: 'active' as const,
      created_at: new Date().toISOString(),
      agent_backend: 'openai' as AgentBackendName,
    };

    const backend = registry.resolve(group, task);
    expect(backend.name()).toBe('openai');
  });

  it('has returns false for unregistered backend', () => {
    expect(registry.has('openai')).toBe(false);
  });
});
