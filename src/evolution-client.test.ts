import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// Mock child_process BEFORE importing the module under test.
vi.mock('child_process', () => ({
  spawn: vi.fn(),
  execFile: vi.fn(),
}));

// Cross-platform kill helper — mocked so the kill-timer tests can assert on it
// without sending a real signal (SIGKILL is unsupported on Windows).
vi.mock('./platform.js', () => ({
  forceKillProcess: vi.fn(),
  isInteractiveTerminal: () => false,
}));

import { spawn } from 'child_process';
import { forceKillProcess } from './platform.js';
import {
  logReactionSignal,
  logInteraction,
  parsePositiveMsEnv,
} from './evolution-client.js';

const mockSpawn = vi.mocked(spawn);

function _fakeChild() {
  return {
    pid: 12345,
    stderr: { on: vi.fn() },
    on: vi.fn(),
    unref: vi.fn(),
  } as unknown as ReturnType<typeof spawn>;
}

const mockForceKill = vi.mocked(forceKillProcess);

beforeEach(() => {
  mockSpawn.mockReset();
  mockSpawn.mockImplementation(() => _fakeChild());
  delete process.env.EVOLUTION_ENABLED;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('logReactionSignal', () => {
  it('spawns log_interaction with positive signal for 👍', () => {
    logReactionSignal({ emoji: '👍', groupFolder: 'whatsapp_main' });

    expect(mockSpawn).toHaveBeenCalledOnce();
    const args = mockSpawn.mock.calls[0][1] as string[];
    const payload = JSON.parse(args[args.length - 1]);
    expect(payload.user_signal).toBe('positive');
    expect(payload.group_folder).toBe('whatsapp_main');
    expect(payload.prompt).toBe('[reaction]');
    expect(payload.response).toBe('');
  });

  it('spawns log_interaction with negative signal for 👎', () => {
    logReactionSignal({ emoji: '👎', groupFolder: 'telegram_main' });

    expect(mockSpawn).toHaveBeenCalledOnce();
    const args = mockSpawn.mock.calls[0][1] as string[];
    const payload = JSON.parse(args[args.length - 1]);
    expect(payload.user_signal).toBe('negative');
  });

  it('does NOT spawn for neutral emoji', () => {
    logReactionSignal({ emoji: '😂', groupFolder: 'whatsapp_main' });
    expect(mockSpawn).not.toHaveBeenCalled();
  });

  it('does NOT spawn for empty emoji (reaction-removed)', () => {
    logReactionSignal({ emoji: '', groupFolder: 'whatsapp_main' });
    expect(mockSpawn).not.toHaveBeenCalled();
  });

  it('forwards sessionId when provided', () => {
    logReactionSignal({
      emoji: '❤️',
      groupFolder: 'whatsapp_main',
      sessionId: 'sess_abc123',
    });

    const args = mockSpawn.mock.calls[0][1] as string[];
    const payload = JSON.parse(args[args.length - 1]);
    expect(payload.session_id).toBe('sess_abc123');
    expect(payload.user_signal).toBe('positive');
  });

  it('generates a fresh UUID id for each call', () => {
    logReactionSignal({ emoji: '🔥', groupFolder: 'whatsapp_main' });
    logReactionSignal({ emoji: '🔥', groupFolder: 'whatsapp_main' });

    const id1 = JSON.parse(
      (mockSpawn.mock.calls[0][1] as string[]).slice(-1)[0],
    ).id;
    const id2 = JSON.parse(
      (mockSpawn.mock.calls[1][1] as string[]).slice(-1)[0],
    ).id;
    expect(id1).not.toBe(id2);
    expect(id1).toMatch(/^[0-9a-f-]{36}$/);
  });
});

describe('parsePositiveMsEnv (LIA-235)', () => {
  const FB = 60 * 60 * 1000;

  it('returns the fallback when the env var is unset', () => {
    expect(parsePositiveMsEnv(undefined, FB, 'X')).toBe(FB);
  });

  it('returns a valid positive number', () => {
    expect(parsePositiveMsEnv('300000', FB, 'X')).toBe(300000);
  });

  it.each(['', 'abc', '0', '-5', 'NaN'])(
    'falls back to the default on malformed/non-positive value %j',
    (bad) => {
      // The trap this guards: Number('') === 0 and Number('abc') === NaN, either
      // of which would make setTimeout fire on the next tick.
      expect(parsePositiveMsEnv(bad, FB, 'X')).toBe(FB);
    },
  );
});

describe('logInteraction child lifecycle (LIA-235)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mockForceKill.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // Far beyond any sane LOG_INTERACTION_TIMEOUT_MS default (1h).
  const PAST_CEILING_MS = 25 * 60 * 60 * 1000;

  function lastChild() {
    return mockSpawn.mock.results[mockSpawn.mock.results.length - 1]
      .value as unknown as {
      unref: ReturnType<typeof vi.fn>;
      kill: ReturnType<typeof vi.fn>;
      on: ReturnType<typeof vi.fn>;
    };
  }

  /** Invoke the handler the code registered for a given child event. */
  function fireChildEvent(child: { on: ReturnType<typeof vi.fn> }, ev: string) {
    const entry = child.on.mock.calls.find(([e]) => e === ev);
    if (!entry) throw new Error(`no '${ev}' handler registered`);
    (entry[1] as () => void)();
  }

  it('unrefs the fire-and-forget child so it cannot pin host shutdown', () => {
    logInteraction({
      id: 'i1',
      prompt: 'p',
      response: null,
      groupFolder: 'whatsapp_main',
    });
    expect(mockSpawn).toHaveBeenCalledOnce();
    expect(lastChild().unref).toHaveBeenCalledOnce();
  });

  it('SIGKILLs a child that overruns the ceiling', () => {
    logInteraction({
      id: 'i2',
      prompt: 'p',
      response: null,
      groupFolder: 'whatsapp_main',
    });
    lastChild();
    expect(mockForceKill).not.toHaveBeenCalled();
    vi.advanceTimersByTime(PAST_CEILING_MS);
    expect(mockForceKill).toHaveBeenCalledWith(12345); // the fake child's pid
  });

  it('does NOT kill a child that exits before the ceiling', () => {
    logInteraction({
      id: 'i3',
      prompt: 'p',
      response: null,
      groupFolder: 'whatsapp_main',
    });
    const child = lastChild();
    fireChildEvent(child, 'exit'); // child finished its work → clears the timer
    vi.advanceTimersByTime(PAST_CEILING_MS);
    expect(mockForceKill).not.toHaveBeenCalled();
  });
});

describe('getActivePrompt (LIA-131 Phase 2)', () => {
  // The flag/enabled gates are module-load consts, so each case sets env then
  // re-imports the module fresh and reads execFile from the fresh child_process.
  async function load() {
    vi.resetModules();
    const cp = await import('child_process');
    const execFileMock = vi.mocked(cp.execFile);
    execFileMock.mockReset();
    const { getActivePrompt } = await import('./evolution-client.js');
    return { getActivePrompt, execFileMock };
  }

  /** Make execFile invoke its callback (its last arg) with err + stdout. */
  function stubExecFile(
    execFileMock: ReturnType<typeof vi.fn>,
    err: unknown,
    stdout: string,
  ) {
    execFileMock.mockImplementation((...args: unknown[]) => {
      const cb = args[args.length - 1] as (e: unknown, out: string) => void;
      cb(err, stdout);
      return undefined as never;
    });
  }

  beforeEach(() => {
    delete process.env.EVOLUTION_ENABLED;
    delete process.env.EVOLUTION_OPTIMIZED_PROMPTS;
  });

  it('default-OFF: returns empty block and does NOT spawn Python', async () => {
    // EVOLUTION_OPTIMIZED_PROMPTS unset → off.
    const { getActivePrompt, execFileMock } = await load();
    const res = await getActivePrompt('qa');
    expect(res).toEqual({ block: '' });
    expect(execFileMock).not.toHaveBeenCalled();
  });

  it('combined-flag: EVOLUTION_ENABLED=0 suppresses even when prompt flag is ON', async () => {
    process.env.EVOLUTION_ENABLED = '0';
    process.env.EVOLUTION_OPTIMIZED_PROMPTS = '1';
    const { getActivePrompt, execFileMock } = await load();
    const res = await getActivePrompt('qa');
    expect(res).toEqual({ block: '' });
    expect(execFileMock).not.toHaveBeenCalled();
  });

  it('flag-ON: parses the sanitized block and metadata from the helper', async () => {
    process.env.EVOLUTION_OPTIMIZED_PROMPTS = '1';
    const { getActivePrompt, execFileMock } = await load();
    stubExecFile(
      execFileMock,
      null,
      JSON.stringify({
        block:
          '<stored-output source="dspy-artifact" module="qa">\nBe concise.\n</stored-output>',
        artifact_id: 'art-1',
        baseline_score: 0.7,
        optimized_score: 0.88,
        sample_count: 42,
      }),
    );
    const res = await getActivePrompt('qa');
    expect(execFileMock).toHaveBeenCalledOnce();
    const calledArgs = execFileMock.mock.calls[0][1] as string[];
    expect(calledArgs).toContain('get_active_prompt');
    expect(calledArgs).toContain('qa');
    expect(res.block).toContain('Be concise.');
    expect(res.artifactId).toBe('art-1');
    expect(res.optimizedScore).toBe(0.88);
    expect(res.sampleCount).toBe(42);
  });

  it('flag-ON: empty helper result ({}) is a no-op empty block', async () => {
    process.env.EVOLUTION_OPTIMIZED_PROMPTS = '1';
    const { getActivePrompt, execFileMock } = await load();
    stubExecFile(execFileMock, null, '{}');
    const res = await getActivePrompt('qa');
    expect(res).toEqual({ block: '' });
  });

  it('flag-ON: a subprocess error fails safe to an empty block', async () => {
    process.env.EVOLUTION_OPTIMIZED_PROMPTS = '1';
    const { getActivePrompt, execFileMock } = await load();
    stubExecFile(execFileMock, new Error('boom'), '');
    const res = await getActivePrompt('qa');
    expect(res).toEqual({ block: '' });
  });
});
