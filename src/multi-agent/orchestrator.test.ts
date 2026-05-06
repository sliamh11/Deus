/**
 * Unit tests for MultiAgentOrchestrator — parallel subagent dispatch,
 * topological sorting, status marker parsing, and concern propagation.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

import type {
  AgentRuntime,
  RunContext,
  RunResult,
  RuntimeCapabilities,
  RuntimeEventSink,
  RuntimeSession,
} from '../agent-runtimes/types.js';
import type { RuntimeRegistry } from '../agent-runtimes/registry.js';
import type { RegisteredGroup } from '../types.js';
import type { SubagentTask, OrchestratorResult } from './types.js';
import { MultiAgentOrchestrator } from './orchestrator.js';
import { UserError } from '../errors/index.js';

// ── Mocks ──────────────────────────────────────────────────────────────

vi.mock('../logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

// ── Test helpers ───────────────────────────────────────────────────────

const makeGroup = (
  overrides: Partial<RegisteredGroup> = {},
): RegisteredGroup => ({
  name: 'Test Group',
  folder: 'test-group',
  trigger: '@Deus',
  added_at: '2024-01-01T00:00:00.000Z',
  ...overrides,
});

const makeTask = (overrides: Partial<SubagentTask> = {}): SubagentTask => ({
  id: 'task-1',
  role: 'researcher',
  goal: 'Find relevant information',
  backstory: 'You are a thorough researcher',
  prompt: 'Research topic X',
  mode: 'read',
  ...overrides,
});

const defaultSession: RuntimeSession = {
  backend: 'claude',
  session_id: 'test-session',
};

function createMockRuntime(
  outputText: string = 'Done. [STATUS:DONE]',
  runResultOverrides: Partial<RunResult> = {},
): AgentRuntime {
  return {
    name: () => 'claude' as const,
    capabilities: () =>
      ({
        shell: true,
        filesystem: true,
        web: false,
        multimodal: false,
        handoffs: false,
        persistent_sessions: true,
        tool_streaming: false,
      }) as RuntimeCapabilities,
    startOrResume: vi.fn(async () => defaultSession),
    runTurn: vi.fn(
      async (
        _ctx: RunContext,
        _session: RuntimeSession,
        eventSink: RuntimeEventSink,
      ): Promise<RunResult> => {
        await eventSink({ type: 'output_text', text: outputText });
        await eventSink({ type: 'turn_complete' });
        return {
          status: 'success',
          result: outputText,
          sessionRef: defaultSession,
          ...runResultOverrides,
        };
      },
    ),
    close: vi.fn(async () => {}),
  };
}

function createMockRegistry(runtime: AgentRuntime): RuntimeRegistry {
  return {
    resolve: () => runtime,
    register: vi.fn(),
    get: vi.fn(() => runtime),
    has: vi.fn(() => true),
    list: vi.fn(() => ['claude']),
  } as unknown as RuntimeRegistry;
}

// ── Tests ──────────────────────────────────────────────────────────────

describe('MultiAgentOrchestrator', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  describe('fan-out with mock runtimes', () => {
    it('executes parallel tasks and aggregates results', async () => {
      const runtime = createMockRuntime('Research complete. [STATUS:DONE]');
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const tasks = [
        makeTask({ id: 'task-a', prompt: 'Research A' }),
        makeTask({ id: 'task-b', prompt: 'Research B' }),
        makeTask({ id: 'task-c', prompt: 'Research C' }),
      ];

      const result = await orchestrator.dispatch(tasks, group);

      expect(result.status).toBe('success');
      expect(result.results).toHaveLength(3);
      expect(result.results.every((r) => r.status === 'DONE')).toBe(true);
      // Runtime should have been called for each task
      expect(runtime.runTurn).toHaveBeenCalledTimes(3);
    });
  });

  describe('DONE_WITH_CONCERNS', () => {
    it('collects concerns into OrchestratorResult', async () => {
      const runtime = createMockRuntime(
        'Output here. [STATUS:DONE_WITH_CONCERNS:perf regression;flaky test]',
      );
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const result = await orchestrator.dispatch([makeTask()], group);

      expect(result.status).toBe('success');
      expect(result.concerns).toEqual(['perf regression', 'flaky test']);
      expect(result.results[0].status).toBe('DONE_WITH_CONCERNS');
      expect(result.results[0].concerns).toEqual([
        'perf regression',
        'flaky test',
      ]);
    });
  });

  describe('BLOCKED subagent', () => {
    it('returns partial status when some tasks are blocked', async () => {
      let callCount = 0;
      const runtime = createMockRuntime();
      (runtime.runTurn as ReturnType<typeof vi.fn>).mockImplementation(
        async (
          _ctx: RunContext,
          _session: RuntimeSession,
          eventSink: RuntimeEventSink,
        ) => {
          callCount++;
          if (callCount === 2) {
            await eventSink({
              type: 'output_text',
              text: 'Cannot proceed. [STATUS:BLOCKED:missing API key]',
            });
            await eventSink({ type: 'turn_complete' });
            return { status: 'success', result: null } as RunResult;
          }
          await eventSink({
            type: 'output_text',
            text: 'Done. [STATUS:DONE]',
          });
          await eventSink({ type: 'turn_complete' });
          return { status: 'success', result: null } as RunResult;
        },
      );
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const tasks = [
        makeTask({ id: 'a', prompt: 'A' }),
        makeTask({ id: 'b', prompt: 'B' }),
      ];

      const result = await orchestrator.dispatch(tasks, group);

      expect(result.status).toBe('partial');
      expect(result.results[0].status).toBe('DONE');
      expect(result.results[1].status).toBe('BLOCKED');
    });
  });

  describe('tier-0 fails -> tier-1 blocked with dependency reason', () => {
    it('blocks dependent tasks when dependency fails', async () => {
      const runtime = createMockRuntime(
        'Failed. [STATUS:BLOCKED:out of memory]',
      );
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const tasks = [
        makeTask({ id: 'fetcher', prompt: 'Fetch data' }),
        makeTask({
          id: 'analyzer',
          prompt: 'Analyze data',
          contextFrom: ['fetcher'],
        }),
      ];

      const result = await orchestrator.dispatch(tasks, group);

      expect(result.status).toBe('error');
      // Tier-0 task ran and returned BLOCKED
      expect(result.results[0].status).toBe('BLOCKED');
      expect(result.results[0].blockedReason).toBe('out of memory');
      // Tier-1 task was never launched — BLOCKED with dependency reason
      expect(result.results[1].status).toBe('BLOCKED');
      expect(result.results[1].blockedReason).toBe('dependency fetcher failed');
      // Only the tier-0 task should have been called
      expect(runtime.runTurn).toHaveBeenCalledTimes(1);
    });

    it('blocks transitive dependents (A→B→C, A blocked → B and C blocked)', async () => {
      const runtime = createMockRuntime(
        'Failed. [STATUS:BLOCKED:out of memory]',
      );
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const tasks = [
        makeTask({ id: 'A', prompt: 'Step A' }),
        makeTask({ id: 'B', prompt: 'Step B', contextFrom: ['A'] }),
        makeTask({ id: 'C', prompt: 'Step C', contextFrom: ['B'] }),
      ];

      const result = await orchestrator.dispatch(tasks, group);

      expect(result.status).toBe('error');
      expect(result.results[0].status).toBe('BLOCKED');
      expect(result.results[1].status).toBe('BLOCKED');
      expect(result.results[1].blockedReason).toBe('dependency A failed');
      expect(result.results[2].status).toBe('BLOCKED');
      expect(result.results[2].blockedReason).toBe('dependency B failed');
      // Only task A should have been launched
      expect(runtime.runTurn).toHaveBeenCalledTimes(1);
    });
  });

  describe('circular dependency', () => {
    it('throws UserError for circular deps', async () => {
      const runtime = createMockRuntime();
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const tasks = [
        makeTask({ id: 'A', prompt: 'A', contextFrom: ['B'] }),
        makeTask({ id: 'B', prompt: 'B', contextFrom: ['A'] }),
      ];

      await expect(orchestrator.dispatch(tasks, group)).rejects.toThrow(
        UserError,
      );
      await expect(orchestrator.dispatch(tasks, group)).rejects.toThrow(
        'Circular dependency in subagent tasks',
      );
    });
  });

  describe('mock throws -> allSettled catches -> BLOCKED', () => {
    it('catches thrown errors and marks as BLOCKED', async () => {
      const runtime = createMockRuntime();
      (runtime.runTurn as ReturnType<typeof vi.fn>).mockRejectedValue(
        new Error('Container crashed'),
      );
      // startOrResume still succeeds so the failure is from runTurn
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const result = await orchestrator.dispatch([makeTask()], group);

      expect(result.status).toBe('error');
      expect(result.results[0].status).toBe('BLOCKED');
      expect(result.results[0].blockedReason).toBe('Container crashed');
    });
  });

  describe('status mapping', () => {
    it('all DONE → success', async () => {
      const runtime = createMockRuntime('OK [STATUS:DONE]');
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const result = await orchestrator.dispatch(
        [makeTask({ id: 'a' }), makeTask({ id: 'b' })],
        group,
      );
      expect(result.status).toBe('success');
    });

    it('mixed DONE and BLOCKED → partial', async () => {
      let callIdx = 0;
      const runtime = createMockRuntime();
      (runtime.runTurn as ReturnType<typeof vi.fn>).mockImplementation(
        async (
          _ctx: RunContext,
          _session: RuntimeSession,
          eventSink: RuntimeEventSink,
        ) => {
          callIdx++;
          const text =
            callIdx === 1
              ? 'OK [STATUS:DONE]'
              : 'Blocked [STATUS:BLOCKED:reason]';
          await eventSink({ type: 'output_text', text });
          await eventSink({ type: 'turn_complete' });
          return { status: 'success', result: null } as RunResult;
        },
      );
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const result = await orchestrator.dispatch(
        [makeTask({ id: 'a' }), makeTask({ id: 'b' })],
        group,
      );
      expect(result.status).toBe('partial');
    });

    it('all failed → error', async () => {
      const runtime = createMockRuntime('[STATUS:BLOCKED:nope]');
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const result = await orchestrator.dispatch([makeTask()], group);
      expect(result.status).toBe('error');
    });
  });

  describe('status marker parsing', () => {
    it('parses [STATUS:DONE] and strips it from output', async () => {
      const runtime = createMockRuntime('The answer is 42. [STATUS:DONE]');
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const result = await orchestrator.dispatch([makeTask()], group);

      expect(result.results[0].status).toBe('DONE');
      expect(result.results[0].output).not.toContain('[STATUS:DONE]');
      expect(result.results[0].output).toContain('The answer is 42');
    });

    it('defaults to DONE when no marker present', async () => {
      const runtime = createMockRuntime('Just some output without markers.');
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const result = await orchestrator.dispatch([makeTask()], group);

      expect(result.results[0].status).toBe('DONE');
      expect(result.results[0].output).toBe(
        'Just some output without markers.',
      );
    });

    it('parses DONE_WITH_CONCERNS and extracts concerns', async () => {
      const runtime = createMockRuntime(
        'Result. [STATUS:DONE_WITH_CONCERNS:memory leak;race condition]',
      );
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const result = await orchestrator.dispatch([makeTask()], group);

      expect(result.results[0].status).toBe('DONE_WITH_CONCERNS');
      expect(result.results[0].concerns).toEqual([
        'memory leak',
        'race condition',
      ]);
    });

    it('parses BLOCKED marker', async () => {
      const runtime = createMockRuntime(
        'Cannot. [STATUS:BLOCKED:missing credentials]',
      );
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const result = await orchestrator.dispatch([makeTask()], group);

      expect(result.results[0].status).toBe('BLOCKED');
      expect(result.results[0].blockedReason).toBe('missing credentials');
    });
  });

  describe('maxParallel cap', () => {
    it('limits concurrent executions to maxParallel', async () => {
      let concurrentCount = 0;
      let maxConcurrent = 0;

      const runtime = createMockRuntime();
      (runtime.startOrResume as ReturnType<typeof vi.fn>).mockImplementation(
        async () => defaultSession,
      );
      (runtime.runTurn as ReturnType<typeof vi.fn>).mockImplementation(
        async (
          _ctx: RunContext,
          _session: RuntimeSession,
          eventSink: RuntimeEventSink,
        ) => {
          concurrentCount++;
          maxConcurrent = Math.max(maxConcurrent, concurrentCount);
          // Simulate async work
          await new Promise((resolve) => setTimeout(resolve, 10));
          await eventSink({
            type: 'output_text',
            text: 'Done. [STATUS:DONE]',
          });
          await eventSink({ type: 'turn_complete' });
          concurrentCount--;
          return { status: 'success', result: null } as RunResult;
        },
      );
      const registry = createMockRegistry(runtime);
      // maxParallel = 2
      const orchestrator = new MultiAgentOrchestrator(registry, 2);
      const group = makeGroup();

      const tasks = [
        makeTask({ id: 'a' }),
        makeTask({ id: 'b' }),
        makeTask({ id: 'c' }),
        makeTask({ id: 'd' }),
        makeTask({ id: 'e' }),
      ];

      const result = await orchestrator.dispatch(tasks, group);

      expect(result.status).toBe('success');
      expect(result.results).toHaveLength(5);
      // Concurrency should never exceed maxParallel
      expect(maxConcurrent).toBeLessThanOrEqual(2);
      expect(runtime.runTurn).toHaveBeenCalledTimes(5);
    });
  });

  describe('concerns propagation', () => {
    it('tier-0 concerns appear in tier-1 prompt context', async () => {
      let tier1Prompt = '';
      let callIdx = 0;

      const runtime = createMockRuntime();
      (runtime.runTurn as ReturnType<typeof vi.fn>).mockImplementation(
        async (
          ctx: RunContext,
          _session: RuntimeSession,
          eventSink: RuntimeEventSink,
        ) => {
          callIdx++;
          if (callIdx === 1) {
            // Tier-0: return with concerns
            await eventSink({
              type: 'output_text',
              text: 'Findings here. [STATUS:DONE_WITH_CONCERNS:data may be stale;API rate limited]',
            });
          } else {
            // Tier-1: capture the prompt for verification
            tier1Prompt = ctx.prompt;
            await eventSink({
              type: 'output_text',
              text: 'Analysis done. [STATUS:DONE]',
            });
          }
          await eventSink({ type: 'turn_complete' });
          return { status: 'success', result: null } as RunResult;
        },
      );
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const tasks = [
        makeTask({ id: 'fetcher', prompt: 'Fetch data' }),
        makeTask({
          id: 'analyzer',
          prompt: 'Analyze data',
          contextFrom: ['fetcher'],
        }),
      ];

      const result = await orchestrator.dispatch(tasks, group);

      expect(result.status).toBe('success');
      // Tier-1 prompt should include prior concerns
      expect(tier1Prompt).toContain('Concerns from fetcher');
      expect(tier1Prompt).toContain('data may be stale');
      expect(tier1Prompt).toContain('API rate limited');
    });
  });

  describe('runtime error status', () => {
    it('treats RunResult status=error as BLOCKED', async () => {
      const runtime = createMockRuntime('Some output', {
        status: 'error',
        error: 'Timeout',
      });
      const registry = createMockRegistry(runtime);
      const orchestrator = new MultiAgentOrchestrator(registry);
      const group = makeGroup();

      const result = await orchestrator.dispatch([makeTask()], group);

      expect(result.results[0].status).toBe('BLOCKED');
      expect(result.results[0].blockedReason).toBe('Timeout');
    });
  });
});
