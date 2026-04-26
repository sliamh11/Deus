import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { _initTestDatabase, createTask, getTaskById } from './db.js';
import {
  _resetSchedulerLoopForTests,
  computeNextRun,
  startSchedulerLoop,
} from './task-scheduler.js';
import type {
  BackendSessionRef,
  RunContext,
  RuntimeEventSink,
} from './agent-backends/types.js';
import type { SchedulerDependencies } from './task-scheduler.js';
import { BackendRegistry } from './agent-backends/registry.js';
import type { RunResult } from './agent-backends/types.js';

type RunTurnFn = (
  ctx: RunContext,
  session: BackendSessionRef,
  sink: RuntimeEventSink,
) => Promise<RunResult>;

function makeStubRegistry(runTurnOverride?: RunTurnFn): BackendRegistry {
  const registry = new BackendRegistry();
  const defaultRunTurn: RunTurnFn = async (_ctx, _session, sink) => {
    await sink({ type: 'output_text', text: 'Agent response' });
    await sink({
      type: 'session',
      sessionRef: { backend: 'claude', session_id: 'sess-1' },
    });
    await sink({ type: 'turn_complete' });
    return {
      status: 'success',
      result: 'Agent response',
      sessionRef: { backend: 'claude', session_id: 'sess-1' },
    };
  };
  const runTurn = runTurnOverride ?? defaultRunTurn;
  const stub = (name: 'claude' | 'openai') => ({
    name: () => name,
    capabilities: () => ({
      shell: true,
      filesystem: true,
      web: true,
      multimodal: true,
      handoffs: false,
      persistent_sessions: true,
      tool_streaming: name === 'claude',
    }),
    startOrResume: async () => ({ backend: name, session_id: '' }),
    runTurn,
    close: async () => {},
  });
  registry.register(stub('claude'));
  registry.register(stub('openai'));
  return registry;
}

vi.mock('./container-runner.js', () => ({
  writeTasksSnapshot: vi.fn(),
}));

vi.mock('fs', async (importOriginal) => {
  const actual = await importOriginal<typeof import('fs')>();
  return {
    ...actual,
    default: {
      ...actual,
      mkdirSync: vi.fn(),
    },
  };
});

describe('task scheduler', () => {
  beforeEach(() => {
    _initTestDatabase();
    _resetSchedulerLoopForTests();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('pauses due tasks with invalid group folders to prevent retry churn', async () => {
    createTask({
      id: 'task-invalid-folder',
      group_folder: '../../outside',
      chat_jid: 'bad@g.us',
      prompt: 'run',
      schedule_type: 'once',
      schedule_value: '2026-02-22T00:00:00.000Z',
      context_mode: 'isolated',
      next_run: new Date(Date.now() - 60_000).toISOString(),
      status: 'active',
      created_at: '2026-02-22T00:00:00.000Z',
    });

    const enqueueTask = vi.fn(
      (_groupJid: string, _taskId: string, fn: () => Promise<void>) => {
        void fn();
      },
    );

    startSchedulerLoop({
      registeredGroups: () => ({}),
      getSessions: () => ({}),
      registry: makeStubRegistry(),
      queue: { enqueueTask } as any,
      sendMessage: async () => {},
    });

    await vi.advanceTimersByTimeAsync(10);

    const task = getTaskById('task-invalid-folder');
    expect(task?.status).toBe('paused');
  });

  it('computeNextRun anchors interval tasks to scheduled time to prevent drift', () => {
    const scheduledTime = new Date(Date.now() - 2000).toISOString(); // 2s ago
    const task = {
      id: 'drift-test',
      group_folder: 'test',
      chat_jid: 'test@g.us',
      prompt: 'test',
      schedule_type: 'interval' as const,
      schedule_value: '60000', // 1 minute
      context_mode: 'isolated' as const,
      next_run: scheduledTime,
      last_run: null,
      last_result: null,
      status: 'active' as const,
      created_at: '2026-01-01T00:00:00.000Z',
    };

    const nextRun = computeNextRun(task);
    expect(nextRun).not.toBeNull();

    // Should be anchored to scheduledTime + 60s, NOT Date.now() + 60s
    const expected = new Date(scheduledTime).getTime() + 60000;
    expect(new Date(nextRun!).getTime()).toBe(expected);
  });

  it('computeNextRun returns null for once-tasks', () => {
    const task = {
      id: 'once-test',
      group_folder: 'test',
      chat_jid: 'test@g.us',
      prompt: 'test',
      schedule_type: 'once' as const,
      schedule_value: '2026-01-01T00:00:00.000Z',
      context_mode: 'isolated' as const,
      next_run: new Date(Date.now() - 1000).toISOString(),
      last_run: null,
      last_result: null,
      status: 'active' as const,
      created_at: '2026-01-01T00:00:00.000Z',
    };

    expect(computeNextRun(task)).toBeNull();
  });

  it('computeNextRun skips missed intervals without infinite loop', () => {
    // Task was due 10 intervals ago (missed)
    const ms = 60000;
    const missedBy = ms * 10;
    const scheduledTime = new Date(Date.now() - missedBy).toISOString();

    const task = {
      id: 'skip-test',
      group_folder: 'test',
      chat_jid: 'test@g.us',
      prompt: 'test',
      schedule_type: 'interval' as const,
      schedule_value: String(ms),
      context_mode: 'isolated' as const,
      next_run: scheduledTime,
      last_run: null,
      last_result: null,
      status: 'active' as const,
      created_at: '2026-01-01T00:00:00.000Z',
    };

    const nextRun = computeNextRun(task);
    expect(nextRun).not.toBeNull();
    // Must be in the future
    expect(new Date(nextRun!).getTime()).toBeGreaterThan(Date.now());
    // Must be aligned to the original schedule grid
    const offset =
      (new Date(nextRun!).getTime() - new Date(scheduledTime).getTime()) % ms;
    expect(offset).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// startSchedulerLoop — execution path tests
// ---------------------------------------------------------------------------
describe('startSchedulerLoop execution path', () => {
  beforeEach(() => {
    _initTestDatabase();
    _resetSchedulerLoopForTests();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  /** Builds a minimal valid ScheduledTask overdue by 1 minute. */
  function makeTask(
    overrides: Partial<Parameters<typeof createTask>[0]> = {},
  ): Parameters<typeof createTask>[0] {
    return {
      id: 'task-1',
      group_folder: 'testgroup',
      chat_jid: 'test@g.us',
      prompt: 'hello',
      schedule_type: 'interval',
      schedule_value: '60000',
      context_mode: 'isolated',
      next_run: new Date(Date.now() - 60_000).toISOString(),
      status: 'active',
      created_at: '2026-01-01T00:00:00.000Z',
      ...overrides,
    };
  }

  /** Minimal deps where everything is a no-op by default. */
  function makeDeps(
    overrides: Partial<{
      registeredGroups: Record<string, import('./types.js').RegisteredGroup>;
      sessions: Record<string, string | BackendSessionRef>;
      getSession: SchedulerDependencies['getSession'];
      setSession: SchedulerDependencies['setSession'];
      enqueueTask: ReturnType<typeof vi.fn>;
      sendMessage: ReturnType<typeof vi.fn>;
      notifyIdle: ReturnType<typeof vi.fn>;
      closeStdin: ReturnType<typeof vi.fn>;
      runTurn: RunTurnFn;
    }> = {},
  ): SchedulerDependencies {
    const enqueueTask =
      overrides.enqueueTask ??
      vi.fn((_jid: string, _taskId: string, fn: () => Promise<void>) => {
        void fn();
      });
    const notifyIdle = overrides.notifyIdle ?? vi.fn();
    const closeStdin = overrides.closeStdin ?? vi.fn();
    const sendMessage = overrides.sendMessage ?? vi.fn(async () => {});

    const registeredGroups = overrides.registeredGroups ?? {
      'test@g.us': {
        name: 'Test Group',
        folder: 'testgroup',
        trigger: 'test',
        added_at: '2026-01-01T00:00:00.000Z',
      },
    };

    return {
      registeredGroups: () => registeredGroups,
      getSessions: () => overrides.sessions ?? {},
      getSession: overrides.getSession,
      setSession: overrides.setSession,
      registry: makeStubRegistry(overrides.runTurn),
      queue: { enqueueTask, notifyIdle, closeStdin } as any,
      sendMessage: sendMessage as unknown as (
        jid: string,
        text: string,
      ) => Promise<void>,
    };
  }

  // 1. getDueTasks() returns tasks → all get enqueued via deps.queue.enqueueTask()
  it('enqueues all due tasks returned by getDueTasks', async () => {
    createTask(makeTask({ id: 'task-a', chat_jid: 'a@g.us' }));
    createTask(
      makeTask({
        id: 'task-b',
        chat_jid: 'b@g.us',
        group_folder: 'testgroup',
      }),
    );

    const enqueueTask = vi.fn();
    const deps = makeDeps({ enqueueTask });

    startSchedulerLoop(deps);
    await vi.advanceTimersByTimeAsync(10);

    expect(enqueueTask).toHaveBeenCalledTimes(2);
    const enqueuedIds = enqueueTask.mock.calls.map(
      (call: any[]) => call[1] as string,
    );
    expect(enqueuedIds).toContain('task-a');
    expect(enqueuedIds).toContain('task-b');
  });

  // 2. Task status re-check — paused/cancelled tasks are skipped
  it('skips tasks that are paused by the time of the re-check', async () => {
    createTask(makeTask({ id: 'task-paused' }));

    // Pause it immediately so getTaskById returns paused status
    const { updateTask } = await import('./db.js');
    updateTask('task-paused', { status: 'paused' });

    const enqueueTask = vi.fn();
    startSchedulerLoop(makeDeps({ enqueueTask }));
    await vi.advanceTimersByTimeAsync(10);

    expect(enqueueTask).not.toHaveBeenCalled();
  });

  // 3. Streaming output → sendMessage delivers result to user
  it('delivers streamed result to the user via sendMessage', async () => {
    createTask(makeTask({ id: 'task-stream' }));

    const runTurn: RunTurnFn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'streamed answer' });
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: 'streamed answer' };
    };

    const sendMessage = vi.fn(async () => {});
    startSchedulerLoop(makeDeps({ sendMessage, runTurn }));
    await vi.advanceTimersByTimeAsync(10);

    expect(sendMessage).toHaveBeenCalledWith('test@g.us', 'streamed answer');
  });

  // 4. notifyIdle called on success
  it('calls queue.notifyIdle on successful task run', async () => {
    createTask(makeTask({ id: 'task-idle' }));

    const runTurn: RunTurnFn = async (_ctx, _session, sink) => {
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: null };
    };

    const notifyIdle = vi.fn();
    startSchedulerLoop(makeDeps({ notifyIdle, runTurn }));
    await vi.advanceTimersByTimeAsync(10);

    expect(notifyIdle).toHaveBeenCalledWith('test@g.us');
  });

  // 5. backend.runTurn() failure → logged with logTaskRun
  it('logs an error run when backend.runTurn throws', async () => {
    createTask(makeTask({ id: 'task-fail' }));

    const runTurn: RunTurnFn = async () => {
      throw new Error('container exploded');
    };

    const logTaskRunSpy = vi.spyOn(await import('./db.js'), 'logTaskRun');

    startSchedulerLoop(makeDeps({ runTurn }));
    await vi.advanceTimersByTimeAsync(10);

    expect(logTaskRunSpy).toHaveBeenCalledWith(
      expect.objectContaining({
        task_id: 'task-fail',
        status: 'error',
        error: 'container exploded',
      }),
    );
  });

  // 6. updateTaskAfterRun called after run with nextRun and resultSummary
  it('calls updateTaskAfterRun with nextRun and result summary after run', async () => {
    createTask(makeTask({ id: 'task-update' }));

    const runTurn: RunTurnFn = async (_ctx, _session, sink) => {
      await sink({ type: 'output_text', text: 'task output' });
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: 'task output' };
    };

    const updateTaskAfterRunSpy = vi.spyOn(
      await import('./db.js'),
      'updateTaskAfterRun',
    );

    startSchedulerLoop(makeDeps({ runTurn }));
    await vi.advanceTimersByTimeAsync(10);

    expect(updateTaskAfterRunSpy).toHaveBeenCalledWith(
      'task-update',
      expect.any(String), // nextRun (ISO string for interval task)
      expect.stringContaining('task output'),
    );
  });

  // 7. Guard — second call to startSchedulerLoop returns early
  it('returns early if scheduler is already running', async () => {
    const enqueueTask = vi.fn();
    const deps = makeDeps({ enqueueTask });

    startSchedulerLoop(deps);
    startSchedulerLoop(deps); // second call — should be a no-op

    createTask(makeTask({ id: 'task-guard' }));
    await vi.advanceTimersByTimeAsync(10);

    // Only one loop is running, so enqueueTask called at most once per task
    // (not doubled because two loops are polling)
    const callCount = enqueueTask.mock.calls.length;
    expect(callCount).toBeLessThanOrEqual(1);
  });

  // 8. Empty due tasks list — loop continues without crash or enqueue
  it('does not crash and does not enqueue when no tasks are due', async () => {
    // No tasks inserted — getDueTasks returns []
    const enqueueTask = vi.fn();
    startSchedulerLoop(makeDeps({ enqueueTask }));
    await vi.advanceTimersByTimeAsync(10);

    expect(enqueueTask).not.toHaveBeenCalled();
  });

  // 9a. context_mode: 'isolated' — session_id is empty string (no prior session)
  it('passes empty session for isolated context tasks', async () => {
    createTask(makeTask({ id: 'task-isolated', context_mode: 'isolated' }));

    const capturedCtx: RunContext[] = [];
    const capturedSession: BackendSessionRef[] = [];
    const runTurn: RunTurnFn = async (ctx, session, sink) => {
      capturedCtx.push(ctx);
      capturedSession.push(session);
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: null };
    };

    startSchedulerLoop(makeDeps({ runTurn }));
    await vi.advanceTimersByTimeAsync(10);

    expect(capturedSession[0].session_id).toBe('');
  });

  // 9b. context_mode: 'group' — session from sessions map
  it('passes group session for group context tasks', async () => {
    createTask(makeTask({ id: 'task-group', context_mode: 'group' }));

    const capturedSession: BackendSessionRef[] = [];
    const runTurn: RunTurnFn = async (_ctx, session, sink) => {
      capturedSession.push(session);
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: null };
    };

    const sessions = { testgroup: 'session-abc-123' };
    startSchedulerLoop(makeDeps({ sessions, runTurn }));
    await vi.advanceTimersByTimeAsync(10);

    expect(capturedSession[0].session_id).toBe('session-abc-123');
  });

  it('uses backend-specific group sessions for scheduled task overrides', async () => {
    createTask(
      makeTask({
        id: 'task-openai',
        context_mode: 'group',
        agent_backend: 'openai',
      }),
    );

    const capturedSession: BackendSessionRef[] = [];
    const runTurn: RunTurnFn = async (_ctx, session, sink) => {
      capturedSession.push(session);
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: null };
    };

    const getSession: NonNullable<SchedulerDependencies['getSession']> = vi.fn(
      (): BackendSessionRef => ({
        backend: 'openai',
        session_id: 'resp-session-123',
      }),
    );
    const sessions = {
      testgroup: {
        backend: 'claude',
        session_id: 'claude-session-123',
      } satisfies BackendSessionRef,
    };

    startSchedulerLoop(makeDeps({ sessions, getSession, runTurn }));
    await vi.advanceTimersByTimeAsync(10);

    expect(getSession).toHaveBeenCalledWith('testgroup', 'openai');
    expect(capturedSession[0].backend).toBe('openai');
    expect(capturedSession[0].session_id).toBe('resp-session-123');
  });

  it('stores new backend session refs produced by scheduled tasks', async () => {
    createTask(makeTask({ id: 'task-save-session', context_mode: 'group' }));

    const runTurn: RunTurnFn = async (_ctx, _session, sink) => {
      await sink({
        type: 'session',
        sessionRef: { backend: 'claude', session_id: 'claude-session-next' },
      });
      await sink({ type: 'turn_complete' });
      return {
        status: 'success',
        result: null,
        sessionRef: { backend: 'claude', session_id: 'claude-session-next' },
      };
    };

    const setSession = vi.fn();
    startSchedulerLoop(makeDeps({ setSession, runTurn }));
    await vi.advanceTimersByTimeAsync(10);

    expect(setSession).toHaveBeenCalledWith('testgroup', {
      backend: 'claude',
      session_id: 'claude-session-next',
    });
  });
});
