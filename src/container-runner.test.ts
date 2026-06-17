import {
  describe,
  it,
  expect,
  beforeAll,
  beforeEach,
  vi,
  afterEach,
} from 'vitest';
import { EventEmitter } from 'events';
import { PassThrough } from 'stream';
import path from 'path';

import { IS_WINDOWS } from './platform.js';

// buildVolumeMounts tests use hardcoded Unix paths (/tmp, /home) that
// path.resolve() converts to drive-letter paths on Windows. These tests
// exercise Docker mount logic (Linux containers), not Windows behavior.
const onWindows = IS_WINDOWS;

// Sentinel markers must match container-runner.ts
const OUTPUT_START_MARKER = '---DEUS_OUTPUT_START---';
const OUTPUT_END_MARKER = '---DEUS_OUTPUT_END---';

// Mock config
vi.mock('./config.js', () => ({
  CONTAINER_IMAGE: 'deus-agent:latest',
  CONTAINER_MAX_OUTPUT_SIZE: 10485760,
  CONTAINER_TIMEOUT: 1800000, // 30min
  CONFIG_DIR: '/tmp/deus-test-config',
  CONTEXT_AUTO_COMPACT_PCT: 75,
  CONTEXT_WARN_PCT: 70,
  CREDENTIAL_PROXY_PORT: 3001,
  DATA_DIR: '/tmp/deus-test-data',
  DEUS_CONTEXT_FILE_MAX_CHARS: '12345',
  DEUS_OPENAI_MODEL: 'gpt-test-model',
  GROUPS_DIR: '/tmp/deus-test-groups',
  HOME_DIR: '/tmp/deus-test-home',
  IDLE_TIMEOUT: 1800000, // 30min
  LLAMA_CPP_AGENT_MODEL: 'llama-test-model',
  LLAMA_CPP_MODEL: 'llama-test-model',
  LLAMA_CPP_PORT: '8765',
  TIMEZONE: 'America/Los_Angeles',
  TOOL_PROXY_PORT: 3003,
}));

vi.mock('./group-tokens.js', () => ({
  getOrCreateGroupToken: (folder?: string) =>
    `token-for-${folder ?? '_anonymous'}`,
}));
const TEST_PROXY_TOKEN = 'token-for-main-group';

// Mock logger
vi.mock('./logger.js', () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

// Mock fs
vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      existsSync: vi.fn(() => false),
      mkdirSync: vi.fn(),
      writeFileSync: vi.fn(),
      readFileSync: vi.fn(() => ''),
      readdirSync: vi.fn(() => []),
      statSync: vi.fn(() => ({ isDirectory: () => false })),
      copyFileSync: vi.fn(),
    },
  };
});

// Mock mount-security
vi.mock('./mount-security.js', () => ({
  validateAdditionalMounts: vi.fn(() => []),
}));

// Mock evolution-client (spawns Python subprocess with 3s timeout — incompatible with fake timers)
vi.mock('./evolution-client.js', () => ({
  getReflections: vi.fn(async () => ({ block: '', reflectionIds: [] })),
  getActivePrompt: vi.fn(async () => ({ block: '' })),
  logInteraction: vi.fn(),
}));

// Mock domain-presets and user-signal
vi.mock('./domain-presets.js', () => ({
  detectDomains: vi.fn(() => []),
  detectDomainsWithFallback: vi.fn(() => Promise.resolve([])),
  parseCustomDomains: vi.fn(() => []),
  getAllDomainNames: vi.fn(() => [
    'engineering',
    'marketing',
    'strategy',
    'study',
    'writing',
  ]),
}));

vi.mock('./user-signal.js', () => ({
  detectUserSignal: vi.fn(() => null),
}));

// Mock project-registry (must export SENSITIVE_FILE_PATTERNS and SENSITIVE_DIR_PATTERNS
// which container-runner.ts imports to shadow credentials inside project mounts)
vi.mock('./project-registry.js', () => ({
  getProjectById: vi.fn(() => null),
  SENSITIVE_FILE_PATTERNS: [
    '.env',
    '.env.local',
    '.env.development',
    '.env.production',
    '.env.staging',
    '.env.test',
  ],
  SENSITIVE_DIR_PATTERNS: ['credentials', 'secrets'],
}));

// Create a controllable fake ChildProcess
function createFakeProcess() {
  const proc = new EventEmitter() as EventEmitter & {
    stdin: PassThrough;
    stdout: PassThrough;
    stderr: PassThrough;
    kill: ReturnType<typeof vi.fn>;
    pid: number;
  };
  proc.stdin = new PassThrough();
  proc.stdout = new PassThrough();
  proc.stderr = new PassThrough();
  proc.kill = vi.fn();
  proc.pid = 12345;
  return proc;
}

let fakeProc: ReturnType<typeof createFakeProcess>;

// Mock child_process.spawn
vi.mock('child_process', async () => {
  const actual =
    await vi.importActual<typeof import('child_process')>('child_process');
  return {
    ...actual,
    spawn: vi.fn(() => fakeProc),
    exec: vi.fn(
      (_cmd: string, _opts: unknown, cb?: (err: Error | null) => void) => {
        if (cb) cb(null);
        return new EventEmitter();
      },
    ),
  };
});

import {
  runContainerAgent,
  ContainerOutput,
  readToolCalls,
  readAvailableTools,
  boundParseBuffer,
} from './container-runner.js';
import {
  getActivePrompt,
  getReflections,
  logInteraction,
} from './evolution-client.js';
import type { RegisteredGroup } from './types.js';

const testGroup: RegisteredGroup = {
  name: 'Test Group',
  folder: 'test-group',
  trigger: '@Deus',
  added_at: new Date().toISOString(),
};

const testInput = {
  prompt: 'Hello',
  groupFolder: 'test-group',
  chatJid: 'test@g.us',
  isControlGroup: false,
};

function emitOutputMarker(
  proc: ReturnType<typeof createFakeProcess>,
  output: ContainerOutput,
) {
  const json = JSON.stringify(output);
  proc.stdout.push(`${OUTPUT_START_MARKER}\n${json}\n${OUTPUT_END_MARKER}\n`);
}

// Mock db (getProjectById is imported from db.js in container-runner.ts)
vi.mock('./db.js', () => ({
  getProjectById: vi.fn(() => undefined),
}));

// Mock credential-proxy
vi.mock('./credential-proxy.js', () => ({
  detectAuthMode: vi.fn(() => 'api-key'),
}));

// Mock container-runtime
vi.mock('./container-runtime.js', () => ({
  CONTAINER_HOST_GATEWAY: 'host.docker.internal',
  CONTAINER_RUNTIME_BIN: 'docker',
  hostGatewayArgs: vi.fn(() => []),
  readonlyMountArgs: vi.fn((hostPath: string, containerPath: string) => [
    '-v',
    `${hostPath}:${containerPath}:ro`,
  ]),
}));

describe('container-runner timeout behavior', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    fakeProc = createFakeProcess();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('timeout after output resolves as success', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    // Emit output with a result
    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'Here is my response',
      newSessionId: 'session-123',
    });

    // Let output processing settle
    await vi.advanceTimersByTimeAsync(10);

    // Fire the hard timeout (IDLE_TIMEOUT + 30s = 1830000ms)
    await vi.advanceTimersByTimeAsync(1830000);

    // Emit close event (as if container was stopped by the timeout)
    fakeProc.emit('close', 137);

    // Let the promise resolve
    await vi.advanceTimersByTimeAsync(10);

    const result = await resultPromise;
    expect(result.status).toBe('success');
    expect(result.newSessionId).toBe('session-123');
    expect(onOutput).toHaveBeenCalledWith(
      expect.objectContaining({ result: 'Here is my response' }),
    );
  });

  it('timeout with no output resolves as error', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    // No output emitted — fire the hard timeout
    await vi.advanceTimersByTimeAsync(1830000);

    // Emit close event
    fakeProc.emit('close', 137);

    await vi.advanceTimersByTimeAsync(10);

    const result = await resultPromise;
    expect(result.status).toBe('error');
    expect(result.error).toContain('timed out');
    expect(onOutput).not.toHaveBeenCalled();
  });

  it('normal exit after output resolves as success', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    // Emit output
    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'Done',
      newSessionId: 'session-456',
    });

    await vi.advanceTimersByTimeAsync(10);

    // Normal exit (no timeout)
    fakeProc.emit('close', 0);

    await vi.advanceTimersByTimeAsync(10);

    const result = await resultPromise;
    expect(result.status).toBe('success');
    expect(result.newSessionId).toBe('session-456');
  });
});

// ---------------------------------------------------------------------------
// parseBuffer must stay bounded (LIA-234). stdout/stderr accumulators are
// capped at CONTAINER_MAX_OUTPUT_SIZE, but the stream parser's buffer was not:
// a torn frame (START, never an END) or marker-free stdout noise could grow
// the host heap for the container's full lifetime.
// ---------------------------------------------------------------------------
describe('boundParseBuffer (LIA-234)', () => {
  // OUTPUT_START_MARKER = '---DEUS_OUTPUT_START---' (23 chars) → keep tail 22.
  const KEEP = OUTPUT_START_MARKER.length - 1; // 22

  it('trims marker-free noise to the last markerLen-1 bytes', () => {
    const out = boundParseBuffer('x'.repeat(100), 50);
    expect(out.droppedTornFrame).toBe(false);
    expect(out.buffer).toBe('x'.repeat(KEEP));
  });

  it('returns a buffer shorter than the marker unchanged (boundary)', () => {
    const buf = 'y'.repeat(KEEP); // exactly 22 < markerLen → cannot hold a START
    const out = boundParseBuffer(buf, 50);
    expect(out.droppedTornFrame).toBe(false);
    expect(out.buffer).toBe(buf);
  });

  it('preserves a partial START prefix split across a chunk boundary', () => {
    const partial = '---DEUS_OUTPUT_ST'; // 17-char prefix, not a full START
    const buf = 'a'.repeat(20) + partial; // 37 bytes, no full START present
    const out = boundParseBuffer(buf, 50);
    expect(out.droppedTornFrame).toBe(false);
    expect(out.buffer.length).toBe(KEEP);
    expect(out.buffer.endsWith(partial)).toBe(true);
  });

  it('drops a torn frame (START with no END) once it exceeds the cap', () => {
    const buf = OUTPUT_START_MARKER + 'x'.repeat(100); // START, no END
    const out = boundParseBuffer(buf, 50);
    expect(out.droppedTornFrame).toBe(true);
    expect(out.buffer).toBe('');
  });

  it('leaves a legit pending frame under the cap unchanged', () => {
    const buf = OUTPUT_START_MARKER + '{"partial'; // START, no END, small
    const out = boundParseBuffer(buf, 1000);
    expect(out.droppedTornFrame).toBe(false);
    expect(out.buffer).toBe(buf);
  });
});

describe('streaming parse resilience to noise + split frames (LIA-234)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    fakeProc = createFakeProcess();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('still parses a valid frame after marker-free stdout noise', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    fakeProc.stdout.push('random stdout noise with no markers\n');
    emitOutputMarker(fakeProc, { status: 'success', result: 'hello' });
    await vi.advanceTimersByTimeAsync(10);

    expect(onOutput).toHaveBeenCalledTimes(1);
    expect(onOutput).toHaveBeenCalledWith(
      expect.objectContaining({ result: 'hello' }),
    );

    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);
    await resultPromise;
  });

  it('still parses a frame split across two chunks (mid-marker)', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    const json = JSON.stringify({ status: 'success', result: 'split' });
    const full = `${OUTPUT_START_MARKER}\n${json}\n${OUTPUT_END_MARKER}\n`;
    // Split inside the START marker so the first chunk holds only a partial
    // START — exercises the marker-free trim's prefix-preservation path.
    fakeProc.stdout.push(full.slice(0, 10));
    await vi.advanceTimersByTimeAsync(10);
    fakeProc.stdout.push(full.slice(10));
    await vi.advanceTimersByTimeAsync(10);

    expect(onOutput).toHaveBeenCalledTimes(1);
    expect(onOutput).toHaveBeenCalledWith(
      expect.objectContaining({ result: 'split' }),
    );

    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);
    await resultPromise;
  });
});

// ---------------------------------------------------------------------------
// A rejected onOutput must NOT wedge the group (LIA-212).
//
// onOutput rejection is reachable in production: container-backend.ts's onOutput
// awaits eventSink with no try/catch, and the orchestrator's eventSink awaits
// channel.sendMessage — a transient WhatsApp/Telegram send failure rejects.
// Before the fix, that poisoned outputChain: (1) later streamed outputs were
// silently dropped, and (2) every streaming close seam gated its only resolve()
// behind outputChain.then(...), so on a rejected chain the dispatch promise
// never settled — the group died and leaked a concurrency slot until restart.
// ---------------------------------------------------------------------------

describe('rejected onOutput resilience (LIA-212)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    fakeProc = createFakeProcess();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('a rejected onOutput still lets the dispatch promise settle (no wedge)', async () => {
    // Rejects on the (only) marker — mimics a transient channel.sendMessage
    // failure bubbling up through the eventSink.
    const onOutput = vi.fn(async (output: ContainerOutput) => {
      if (output.result === 'boom') {
        throw new Error('transient channel.sendMessage failure');
      }
    });

    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    // Capture settlement WITHOUT awaiting: pre-fix the promise never settles,
    // so `await resultPromise` would hang the suite instead of failing the
    // assertion. The .then tap lets us assert settlement and fail fast.
    let settled: Awaited<ReturnType<typeof runContainerAgent>> | undefined;
    void resultPromise.then((r) => {
      settled = r;
    });

    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'boom',
      newSessionId: 'session-wedge',
    });
    await vi.advanceTimersByTimeAsync(10);

    // Normal exit → the streaming-success close seam waits on outputChain.
    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);

    // Pre-fix: the rejected chain gates the only resolve() → never settles.
    expect(settled).toBeDefined();
    expect(settled!.status).toBe('success');
    expect(onOutput).toHaveBeenCalledTimes(1);
  });

  it('a rejected onOutput does not drop subsequent streamed outputs', async () => {
    const seen: Array<string | null> = [];
    const onOutput = vi.fn(async (output: ContainerOutput) => {
      seen.push(output.result ?? null);
      if (output.result === 'first') {
        throw new Error('transient channel.sendMessage failure');
      }
    });

    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );
    let settled = false;
    void resultPromise.then(() => {
      settled = true;
    });

    // First marker rejects (poisons the chain pre-fix) ...
    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'first',
      newSessionId: 'session-drop',
    });
    await vi.advanceTimersByTimeAsync(10);

    // ... the second marker must still reach onOutput.
    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'second',
      newSessionId: 'session-drop',
    });
    await vi.advanceTimersByTimeAsync(10);

    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);

    // Pre-fix: chain poisoned by the 'first' rejection → onOutput('second')
    // never runs and the dispatch never settles.
    expect(seen).toContain('second');
    expect(settled).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Streaming-variant markers (Web UI live output): the parse loop must accept the
// transient 'partial'/'activity' variants and surface their payloads to onOutput,
// without disturbing the terminal 'success'/'streamed' handling.
// ---------------------------------------------------------------------------

describe('streaming variant markers', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    fakeProc = createFakeProcess();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('parses partial + activity markers and a streamed terminal in order', async () => {
    const seen: ContainerOutput[] = [];
    const onOutput = vi.fn(async (o: ContainerOutput) => {
      seen.push(o);
    });
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );
    void resultPromise.then(() => {});

    emitOutputMarker(fakeProc, { status: 'activity', text: 'Running grep' });
    emitOutputMarker(fakeProc, { status: 'partial', delta: 'Hel' });
    emitOutputMarker(fakeProc, { status: 'partial', delta: 'lo' });
    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'Hello',
      streamed: true,
      newSessionId: 'session-stream',
    });
    await vi.advanceTimersByTimeAsync(10);
    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);

    expect(seen.map((o) => o.status)).toEqual([
      'activity',
      'partial',
      'partial',
      'success',
    ]);
    expect(seen[0].text).toBe('Running grep');
    expect(seen[1].delta).toBe('Hel');
    expect(seen[2].delta).toBe('lo');
    expect(seen[3].streamed).toBe(true);
    expect(seen[3].result).toBe('Hello');
  });
});

// ---------------------------------------------------------------------------
// logInteraction fires on EVERY output-producing completion path (LIA-196).
// Before LIA-196 the idle-after-output and non-zero-after-output paths resolved
// success but never logged. These tests pin logInteraction to all four close
// paths and guard the per-site hasCode derivation + the reaped-latency anchor.
// ---------------------------------------------------------------------------

describe('logInteraction on all output-producing paths (LIA-196)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    fakeProc = createFakeProcess();
    vi.mocked(logInteraction).mockClear();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('idle timeout after output still logs the interaction (reaped path)', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'streamed answer',
      newSessionId: 'session-idle',
    });
    await vi.advanceTimersByTimeAsync(10);

    // Idle period elapses → the hard timeout reaps the container after output.
    await vi.advanceTimersByTimeAsync(1830000);
    fakeProc.emit('close', 137);
    await vi.advanceTimersByTimeAsync(10);

    const result = await resultPromise;
    expect(result.status).toBe('success');

    expect(vi.mocked(logInteraction)).toHaveBeenCalledTimes(1);
    const call = vi.mocked(logInteraction).mock.calls[0][0];
    expect(call.response).toBeNull();
    expect(call.hasCode).toBe(false);
    expect(call.groupFolder).toBe('test-group');
    // Latency anchors to the last output, NOT the ~30min reaped lifetime.
    expect(call.latencyMs).toBeLessThan(60000);
  });

  it('non-zero exit after output still logs the interaction (reaped path)', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'streamed answer',
      newSessionId: 'session-nonzero',
    });
    await vi.advanceTimersByTimeAsync(10);

    // Container killed externally (non-zero) AFTER producing output — no timeout.
    fakeProc.emit('close', 137);
    await vi.advanceTimersByTimeAsync(10);

    const result = await resultPromise;
    expect(result.status).toBe('success');

    expect(vi.mocked(logInteraction)).toHaveBeenCalledTimes(1);
    const call = vi.mocked(logInteraction).mock.calls[0][0];
    expect(call.response).toBeNull();
    expect(call.hasCode).toBe(false);
  });

  it('streaming success logs exactly once (no double-log on a single close)', async () => {
    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'streamed answer',
      newSessionId: 'session-clean',
    });
    await vi.advanceTimersByTimeAsync(10);

    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);

    await resultPromise;
    expect(vi.mocked(logInteraction)).toHaveBeenCalledTimes(1);
  });

  it('legacy path derives hasCode=true from a code-bearing result', async () => {
    // No onOutput → legacy (accumulate stdout + parse-on-close) path.
    const resultPromise = runContainerAgent(testGroup, testInput, () => {});

    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'Here you go:\n```js\nconsole.log("hi there");\n```',
      newSessionId: 'session-legacy-code',
    });
    await vi.advanceTimersByTimeAsync(10);

    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);

    const result = await resultPromise;
    expect(result.status).toBe('success');

    expect(vi.mocked(logInteraction)).toHaveBeenCalledTimes(1);
    const call = vi.mocked(logInteraction).mock.calls[0][0];
    expect(call.hasCode).toBe(true);
    expect(call.response).toContain('console.log');
  });

  it('legacy path derives hasCode=false from a plain result', async () => {
    const resultPromise = runContainerAgent(testGroup, testInput, () => {});

    emitOutputMarker(fakeProc, {
      status: 'success',
      result: 'just a plain text answer, nothing fenced here',
      newSessionId: 'session-legacy-plain',
    });
    await vi.advanceTimersByTimeAsync(10);

    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);

    await resultPromise;
    expect(vi.mocked(logInteraction)).toHaveBeenCalledTimes(1);
    const call = vi.mocked(logInteraction).mock.calls[0][0];
    expect(call.hasCode).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// ContainerOutputSchema Zod validation tests
// ---------------------------------------------------------------------------

import { ContainerOutputSchema } from './ipc-protocol.js';

describe('ContainerOutputSchema Zod validation', () => {
  it('accepts a valid ContainerOutput object', () => {
    expect(() =>
      ContainerOutputSchema.parse({ status: 'success', result: 'ok' }),
    ).not.toThrow();
  });

  it('accepts contextStats with null tokens/pct (SDK omits usage → NaN→null, LIA-194)', () => {
    const parsed = ContainerOutputSchema.parse({
      status: 'success',
      result: 'ok',
      contextStats: { tokens: null, limit: 200000, pct: null },
    });
    expect(parsed.contextStats?.tokens).toBeNull();
    expect(parsed.contextStats?.pct).toBeNull();
    expect(parsed.contextStats?.limit).toBe(200000);
  });

  it('throws on schema-mismatched input (missing required fields)', () => {
    expect(() =>
      ContainerOutputSchema.parse({ notAValidField: true }),
    ).toThrow();
  });

  it('throws when status is not a valid enum value', () => {
    expect(() =>
      ContainerOutputSchema.parse({ status: 'unknown', result: null }),
    ).toThrow();
  });

  // ── Output contract + optional-block resilience (LIA-196) ──────────────────

  it('output contract: parses a real CC-2.1.168 DEUS_OUTPUT shape', () => {
    // captured from a live dispatch (docker logs), incl. the null-token usage shape
    const real = {
      status: 'success',
      result: 'Hey! What is up?',
      newSessionRef: {
        backend: 'claude',
        session_id: 'a4258741-addf-4f66-bbd4-3c966bbafb96',
      },
      newSessionId: 'a4258741-addf-4f66-bbd4-3c966bbafb96',
      contextStats: { tokens: null, limit: 200000, pct: null },
    };
    const parsed = ContainerOutputSchema.parse(real);
    expect(parsed.status).toBe('success');
    expect(parsed.result).toBe('Hey! What is up?');
    expect(parsed.newSessionId).toBe('a4258741-addf-4f66-bbd4-3c966bbafb96');
  });

  it('output contract: a malformed OPTIONAL contextStats degrades to undefined, marker still parses', () => {
    const parsed = ContainerOutputSchema.parse({
      status: 'success',
      result: 'ok',
      newSessionId: 's1',
      contextStats: { tokens: 'not-a-number', limit: 'bad' }, // garbage
    });
    // core fields preserved; the bad optional block dropped to undefined (LIA-196)
    expect(parsed.status).toBe('success');
    expect(parsed.result).toBe('ok');
    expect(parsed.newSessionId).toBe('s1');
    expect(parsed.contextStats).toBeUndefined();
  });

  it('output contract: a malformed OPTIONAL compactionEvent degrades to undefined', () => {
    const parsed = ContainerOutputSchema.parse({
      status: 'success',
      result: 'ok',
      compactionEvent: { trigger: 'not-a-valid-trigger' }, // garbage enum
    });
    expect(parsed.result).toBe('ok');
    expect(parsed.compactionEvent).toBeUndefined();
  });

  it('output contract: load-bearing fields stay strict (missing status still throws)', () => {
    expect(() => ContainerOutputSchema.parse({ result: 'ok' })).toThrow();
  });

  it('streaming parse: schema-mismatched output chunk logs error and does not call onOutput', async () => {
    vi.useFakeTimers();
    fakeProc = createFakeProcess();

    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    // Emit a chunk that is valid JSON but does NOT match ContainerOutputSchema
    const badJson = JSON.stringify({ notStatus: 'oops', result: null });
    fakeProc.stdout.push(
      `${OUTPUT_START_MARKER}\n${badJson}\n${OUTPUT_END_MARKER}\n`,
    );

    await vi.advanceTimersByTimeAsync(10);

    // Should have logged an error (schema parse failed)
    const mockLogger = (await import('./logger.js')).logger;
    expect(mockLogger.error).toHaveBeenCalledWith(
      expect.objectContaining({ group: testGroup.name }),
      'Failed to parse streamed output chunk',
    );

    // onOutput should NOT have been called with invalid data
    expect(onOutput).not.toHaveBeenCalled();

    // Clean up
    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);
    await resultPromise;
    vi.useRealTimers();
  });

  it('streaming parse: marker with null tokens/pct is NOT dropped → onOutput called (LIA-194 regression)', async () => {
    vi.useFakeTimers();
    fakeProc = createFakeProcess();

    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    // The real-world failing marker: SDK omitted usage → tokens/pct null on wire.
    const nullStatsJson = JSON.stringify({
      status: 'success',
      result: 'hello',
      contextStats: { tokens: null, limit: 200000, pct: null },
    });
    fakeProc.stdout.push(
      `${OUTPUT_START_MARKER}\n${nullStatsJson}\n${OUTPUT_END_MARKER}\n`,
    );

    await vi.advanceTimersByTimeAsync(10);

    // Marker must parse (not be dropped) → onOutput called with the real result.
    expect(onOutput).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'success', result: 'hello' }),
    );

    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);
    await resultPromise;
    vi.useRealTimers();
  });

  it('streaming parse: a garbage OPTIONAL contextStats does NOT drop the marker → onOutput fires (LIA-194 recurrence guard)', async () => {
    vi.useFakeTimers();
    fakeProc = createFakeProcess();

    const onOutput = vi.fn(async () => {});
    const resultPromise = runContainerAgent(
      testGroup,
      testInput,
      () => {},
      onOutput,
    );

    // The LIA-194 vector, generalized: a malformed OPTIONAL sub-block on the wire.
    // Before the schema .catch (LIA-196) the strict parse threw, the marker was
    // discarded, hadStreamingOutput stayed false, and the dispatch was
    // mis-classified as "timed out with no output" → no onOutput, no logging.
    // Raw push: garbage contextStats can't satisfy the ContainerOutput type, so
    // emitOutputMarker is unusable here.
    const garbageStatsJson = JSON.stringify({
      status: 'success',
      result: 'hello',
      contextStats: { tokens: 'not-a-number', limit: 'bad' },
    });
    fakeProc.stdout.push(
      `${OUTPUT_START_MARKER}\n${garbageStatsJson}\n${OUTPUT_END_MARKER}\n`,
    );

    await vi.advanceTimersByTimeAsync(10);

    // Marker still parses (bad block degraded to undefined) → onOutput fires.
    expect(onOutput).toHaveBeenCalledTimes(1);
    expect(onOutput).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'success', result: 'hello' }),
    );

    fakeProc.emit('close', 0);
    await vi.advanceTimersByTimeAsync(10);
    await resultPromise;
    vi.useRealTimers();
  });
});

// ---------------------------------------------------------------------------
// buildVolumeMounts tests
//
// buildVolumeMounts is not exported, so we exercise it via runContainerAgent.
// We capture the args passed to spawn (which encode all mounts) and the fs
// side-effect calls (mkdirSync, writeFileSync, cpSync) to assert behaviour.
// ---------------------------------------------------------------------------

import * as childProcess from 'child_process';
import * as fsMod from 'fs';
import { getProjectById } from './db.js';

// Helpers to parse mounts out of spawn args
// spawn receives: ['run', '-i', '--rm', '--name', name, ...mountArgs, image]
// Mounts appear as: ['-v', 'host:container'] (writable)
//                or ['-v', 'host:container:ro'] (readonly)
function parseMountsFromSpawnArgs(
  spawnArgs: string[],
): Array<{ hostPath: string; containerPath: string; readonly: boolean }> {
  const mounts: Array<{
    hostPath: string;
    containerPath: string;
    readonly: boolean;
  }> = [];
  for (let i = 0; i < spawnArgs.length; i++) {
    if (spawnArgs[i] === '-v' && i + 1 < spawnArgs.length) {
      const parts = spawnArgs[i + 1].split(':');
      // Windows drive-letter path: C:\path\to\host:/container/path[:ro]
      // split(':') produces ['C', '\\path', '/container/path'] or ['C', '\\path', '/container/path', 'ro']
      if (
        parts.length >= 3 &&
        parts[0].length === 1 &&
        /^[a-zA-Z]$/.test(parts[0])
      ) {
        mounts.push({
          hostPath: parts[0] + ':' + parts[1],
          containerPath: parts[2],
          readonly: parts[3] === 'ro',
        });
      } else if (parts.length === 3 && parts[2] === 'ro') {
        mounts.push({
          hostPath: parts[0],
          containerPath: parts[1],
          readonly: true,
        });
      } else if (parts.length === 2) {
        mounts.push({
          hostPath: parts[0],
          containerPath: parts[1],
          readonly: false,
        });
      }
      i++;
    }
  }
  return mounts;
}

// Run runContainerAgent with real timers, emit close(0) immediately after spawn
// to resolve the promise without waiting for a real container.
async function runAndCaptureMounts(
  group: RegisteredGroup,
  isControlGroup: boolean,
): Promise<
  Array<{ hostPath: string; containerPath: string; readonly: boolean }>
> {
  fakeProc = createFakeProcess();
  // Wire up the spawn mock BEFORE running so it returns the new proc
  vi.mocked(childProcess.spawn).mockReturnValue(
    fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
  );

  // Emit close immediately after the agent sets up listeners (next tick)
  const localProc = fakeProc;
  setImmediate(() => {
    localProc.emit('close', 0);
  });

  const input = {
    prompt: 'test',
    groupFolder: group.folder,
    chatJid: 'test@g.us',
    isControlGroup,
  };

  await runContainerAgent(group, input, () => {});

  const spawnMock = vi.mocked(childProcess.spawn);
  const lastCall = spawnMock.mock.calls[spawnMock.mock.calls.length - 1];
  const args = lastCall[1] as string[];
  return parseMountsFromSpawnArgs(args);
}

describe.skipIf(onWindows)('buildVolumeMounts — main group', () => {
  beforeEach(() => {
    vi.useRealTimers();
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
    fsMocked.existsSync.mockReset();
    fsMocked.existsSync.mockReturnValue(false);
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();
    fsMocked.statSync.mockReturnValue({
      isDirectory: () => false,
    } as ReturnType<typeof fsMod.statSync>);
    if ('cpSync' in fsMocked) {
      (fsMocked as unknown as Record<string, ReturnType<typeof vi.fn>>).cpSync =
        vi.fn();
    }

    vi.mocked(getProjectById).mockReturnValue(undefined);
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );
  });

  it('mounts project root read-only at /workspace/project', async () => {
    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isControlGroup: true,
    };

    const mounts = await runAndCaptureMounts(group, true);

    const projectMount = mounts.find(
      (m) => m.containerPath === '/workspace/project',
    );
    expect(projectMount).toBeDefined();
    expect(projectMount!.readonly).toBe(true);
    expect(projectMount!.hostPath).toBe(process.cwd());
  });

  it('mounts group folder writable at /workspace/group', async () => {
    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isControlGroup: true,
    };

    const mounts = await runAndCaptureMounts(group, true);

    const groupMount = mounts.find(
      (m) => m.containerPath === '/workspace/group',
    );
    expect(groupMount).toBeDefined();
    expect(groupMount!.readonly).toBe(false);
    expect(groupMount!.hostPath).toContain('main-group');
  });

  it('shadows .env with /dev/null when it exists', async () => {
    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
    fsMocked.existsSync.mockImplementation((p: unknown) => {
      if (typeof p === 'string' && p.endsWith('/.env')) return true;
      return false;
    });

    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isControlGroup: true,
    };

    const mounts = await runAndCaptureMounts(group, true);

    const envShadow = mounts.find(
      (m) => m.containerPath === '/workspace/project/.env',
    );
    expect(envShadow).toBeDefined();
    expect(envShadow!.hostPath).toBe('/dev/null');
    expect(envShadow!.readonly).toBe(true);
  });

  it('does not shadow .env when it does not exist', async () => {
    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isControlGroup: true,
    };

    // existsSync returns false by default (set in beforeEach)
    const mounts = await runAndCaptureMounts(group, true);

    const envShadow = mounts.find(
      (m) => m.containerPath === '/workspace/project/.env',
    );
    expect(envShadow).toBeUndefined();
  });

  it('creates settings.json with correct feature flags', async () => {
    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
    // settings file does not exist yet — trigger writeFileSync
    fsMocked.existsSync.mockReturnValue(false);

    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isControlGroup: true,
    };

    fakeProc = createFakeProcess();
    setImmediate(() => fakeProc.emit('close', 0));
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );

    await runContainerAgent(
      group,
      {
        prompt: 'test',
        groupFolder: group.folder,
        chatJid: 'x@g.us',
        isControlGroup: true,
      },
      () => {},
    );

    const writeFileCalls = fsMocked.writeFileSync.mock.calls;
    const settingsCall = writeFileCalls.find(
      (args: unknown[]) =>
        typeof args[0] === 'string' &&
        (args[0] as string).endsWith('settings.json'),
    );
    expect(settingsCall).toBeDefined();

    const content = JSON.parse(settingsCall![1] as string);
    expect(content.env.CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS).toBe('1');
    expect(content.env.CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD).toBe('1');
    expect(content.env.CLAUDE_CODE_DISABLE_AUTO_MEMORY).toBe('0');
  });

  it('syncs skills directory when container/skills/ exists', async () => {
    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
    const skillsSrc = path.join(process.cwd(), 'container', 'skills');

    fsMocked.existsSync.mockImplementation((p: unknown) => {
      if (p === skillsSrc) return true;
      return false;
    });
    fsMocked.readdirSync.mockImplementation((p: unknown) => {
      if (p === skillsSrc)
        return ['pdf-skill' as unknown as import('fs').Dirent];
      return [] as any;
    });
    fsMocked.statSync.mockImplementation((p: unknown) => {
      if (typeof p === 'string' && p.includes('pdf-skill')) {
        return { isDirectory: () => true } as ReturnType<typeof fsMod.statSync>;
      }
      return { isDirectory: () => false } as ReturnType<typeof fsMod.statSync>;
    });

    const cpSyncMock = vi.fn();
    (fsMocked as Record<string, unknown>).cpSync = cpSyncMock;

    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isControlGroup: true,
    };

    fakeProc = createFakeProcess();
    setImmediate(() => fakeProc.emit('close', 0));
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );

    await runContainerAgent(
      group,
      {
        prompt: 'test',
        groupFolder: group.folder,
        chatJid: 'x@g.us',
        isControlGroup: true,
      },
      () => {},
    );

    // cpSync should have been called to sync the skill
    expect(cpSyncMock).toHaveBeenCalled();
    const [src, dst] = cpSyncMock.mock.calls[0];
    expect(src).toContain('pdf-skill');
    expect(dst).toContain('pdf-skill');
  });
});

describe.skipIf(onWindows)('buildVolumeMounts — non-main group', () => {
  beforeEach(() => {
    vi.useRealTimers();
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
    fsMocked.existsSync.mockReset();
    fsMocked.existsSync.mockReturnValue(false);
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();
    fsMocked.statSync.mockReturnValue({
      isDirectory: () => false,
    } as ReturnType<typeof fsMod.statSync>);

    vi.mocked(getProjectById).mockReturnValue(undefined);
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );
  });

  it('does NOT mount project root', async () => {
    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    const mounts = await runAndCaptureMounts(group, false);

    // No /workspace/project unless projectId is set
    const projectMount = mounts.find(
      (m) => m.containerPath === '/workspace/project',
    );
    expect(projectMount).toBeUndefined();
  });

  it('mounts group folder at /workspace/group', async () => {
    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    const mounts = await runAndCaptureMounts(group, false);

    const groupMount = mounts.find(
      (m) => m.containerPath === '/workspace/group',
    );
    expect(groupMount).toBeDefined();
    expect(groupMount!.readonly).toBe(false);
  });

  it('mounts global directory read-only when it exists', async () => {
    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
    const globalDir = '/tmp/deus-test-groups/global';

    fsMocked.existsSync.mockImplementation((p: unknown) => {
      return p === globalDir;
    });

    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    const mounts = await runAndCaptureMounts(group, false);

    const globalMount = mounts.find(
      (m) => m.containerPath === '/workspace/global',
    );
    expect(globalMount).toBeDefined();
    expect(globalMount!.hostPath).toBe(globalDir);
    expect(globalMount!.readonly).toBe(true);
  });

  it('does not mount global directory when it does not exist', async () => {
    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    // existsSync returns false by default
    const mounts = await runAndCaptureMounts(group, false);

    const globalMount = mounts.find(
      (m) => m.containerPath === '/workspace/global',
    );
    expect(globalMount).toBeUndefined();
  });

  it('creates per-group IPC subdirectories', async () => {
    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);

    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    fakeProc = createFakeProcess();
    setImmediate(() => fakeProc.emit('close', 0));
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );

    await runContainerAgent(
      group,
      {
        prompt: 'test',
        groupFolder: group.folder,
        chatJid: 'x@g.us',
        isControlGroup: false,
      },
      () => {},
    );

    const mkdirCalls = fsMocked.mkdirSync.mock.calls.map(
      (c: unknown[]) => c[0] as string,
    );
    expect(mkdirCalls.some((p: string) => /[/\\]messages$/.test(p))).toBe(true);
    expect(mkdirCalls.some((p: string) => /[/\\]tasks$/.test(p))).toBe(true);
    expect(mkdirCalls.some((p: string) => /[/\\]input$/.test(p))).toBe(true);
  });

  it('mounts IPC directory at /workspace/ipc (writable)', async () => {
    const group: RegisteredGroup = {
      name: 'External',
      folder: 'ext-group',
      trigger: '@Bot',
      added_at: new Date().toISOString(),
    };

    const mounts = await runAndCaptureMounts(group, false);

    const ipcMount = mounts.find((m) => m.containerPath === '/workspace/ipc');
    expect(ipcMount).toBeDefined();
    expect(ipcMount!.readonly).toBe(false);
    expect(ipcMount!.hostPath).toContain('ext-group');
  });
});

describe.skipIf(onWindows)(
  'buildVolumeMounts — external project mounts',
  () => {
    const PROJECT_PATH = '/home/user/projects/myapp';

    beforeEach(() => {
      vi.useRealTimers();
      fakeProc = createFakeProcess();

      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
      fsMocked.existsSync.mockReset();
      fsMocked.existsSync.mockReturnValue(false);
      fsMocked.mkdirSync.mockReset();
      fsMocked.writeFileSync.mockReset();
      fsMocked.readdirSync.mockReset();
      fsMocked.readdirSync.mockReturnValue([]);
      fsMocked.statSync.mockReset();
      fsMocked.statSync.mockReturnValue({
        isDirectory: () => false,
      } as ReturnType<typeof fsMod.statSync>);
      fsMocked.realpathSync.mockReset?.();

      vi.mocked(childProcess.spawn).mockReturnValue(
        fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
      );
    });

    it('mounts real project path when realpath matches registered path', async () => {
      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);

      vi.mocked(getProjectById).mockReturnValue({
        id: 'proj-1',
        name: 'My App',
        path: PROJECT_PATH,
        type: null,
        readonly: false,
        created_at: new Date().toISOString(),
      });

      fsMocked.existsSync.mockImplementation(
        (p: unknown) => p === PROJECT_PATH,
      );
      (fsMocked as any).realpathSync = vi.fn().mockReturnValue(PROJECT_PATH);

      const group: RegisteredGroup = {
        name: 'App Group',
        folder: 'app-group',
        trigger: '@Bot',
        added_at: new Date().toISOString(),
        projectId: 'proj-1',
      };

      // Use isControlGroup: false — non-main groups don't mount process.cwd() at
      // /workspace/project, so the only /workspace/project entry comes from
      // the external project mount. This keeps the assertion unambiguous.
      const mounts = await runAndCaptureMounts(group, false);

      const projectMount = mounts.find(
        (m) => m.containerPath === '/workspace/project',
      );
      expect(projectMount).toBeDefined();
      expect(projectMount!.hostPath).toBe(PROJECT_PATH);
    });

    it('blocks mount when realpath differs from registered path (symlink swap)', async () => {
      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
      const SYMLINK_TARGET = '/etc/passwd-dir';

      vi.mocked(getProjectById).mockReturnValue({
        id: 'proj-symlink',
        name: 'Legit App',
        path: PROJECT_PATH,
        type: null,
        readonly: false,
        created_at: new Date().toISOString(),
      });

      fsMocked.existsSync.mockImplementation(
        (p: unknown) => p === PROJECT_PATH,
      );
      (fsMocked as any).realpathSync = vi.fn().mockReturnValue(SYMLINK_TARGET);

      const group: RegisteredGroup = {
        name: 'App Group',
        folder: 'app-group',
        trigger: '@Bot',
        added_at: new Date().toISOString(),
        projectId: 'proj-symlink',
      };

      // Use isControlGroup: false so we can assert no external project at /workspace/project
      const mounts = await runAndCaptureMounts(group, false);

      // Mount should be blocked — no /workspace/project with the symlink target
      const projectMount = mounts.find(
        (m) => m.containerPath === '/workspace/project',
      );
      expect(projectMount).toBeUndefined();
    });

    it('skips mount and warns when project path does not exist', async () => {
      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);

      vi.mocked(getProjectById).mockReturnValue({
        id: 'proj-missing',
        name: 'Gone App',
        path: '/gone/path',
        type: null,
        readonly: false,
        created_at: new Date().toISOString(),
      });

      // existsSync returns false for project path
      fsMocked.existsSync.mockReturnValue(false);

      const group: RegisteredGroup = {
        name: 'App Group',
        folder: 'app-group',
        trigger: '@Bot',
        added_at: new Date().toISOString(),
        projectId: 'proj-missing',
      };

      // Use isControlGroup: false to avoid the main group's process.cwd() mount
      const mounts = await runAndCaptureMounts(group, false);

      const projectMount = mounts.find(
        (m) => m.containerPath === '/workspace/project',
      );
      expect(projectMount).toBeUndefined();
    });

    it('project is readonly when project.readonly is true', async () => {
      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);

      vi.mocked(getProjectById).mockReturnValue({
        id: 'proj-ro',
        name: 'ReadOnly App',
        path: PROJECT_PATH,
        type: null,
        readonly: true,
        created_at: new Date().toISOString(),
      });

      fsMocked.existsSync.mockImplementation(
        (p: unknown) => p === PROJECT_PATH,
      );
      (fsMocked as any).realpathSync = vi.fn().mockReturnValue(PROJECT_PATH);

      const group: RegisteredGroup = {
        name: 'App Group',
        folder: 'app-group',
        trigger: '@Bot',
        added_at: new Date().toISOString(),
        projectId: 'proj-ro',
      };

      const mounts = await runAndCaptureMounts(group, true);

      const projectMount = mounts.find(
        (m) => m.containerPath === '/workspace/project',
      );
      expect(projectMount).toBeDefined();
      expect(projectMount!.readonly).toBe(true);
    });
  },
);

describe.skipIf(onWindows)(
  'buildVolumeMounts — sensitive file shadowing',
  () => {
    const PROJECT_PATH = '/home/user/projects/myapp';

    beforeEach(() => {
      vi.useRealTimers();
      fakeProc = createFakeProcess();

      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
      fsMocked.existsSync.mockReset();
      fsMocked.mkdirSync.mockReset();
      fsMocked.writeFileSync.mockReset();
      fsMocked.readdirSync.mockReset();
      fsMocked.readdirSync.mockReturnValue([]);
      fsMocked.statSync.mockReset();

      vi.mocked(childProcess.spawn).mockReturnValue(
        fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
      );

      // Default project setup for all shadowing tests
      vi.mocked(getProjectById).mockReturnValue({
        id: 'proj-shadow',
        name: 'Shadow App',
        path: PROJECT_PATH,
        type: null,
        readonly: false,
        created_at: new Date().toISOString(),
      });
      (fsMocked as any).realpathSync = vi.fn().mockReturnValue(PROJECT_PATH);
    });

    it('shadows .env file in the project with /dev/null', async () => {
      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);

      fsMocked.existsSync.mockImplementation((p: unknown) => {
        const s = p as string;
        if (s === PROJECT_PATH) return true;
        if (s === `${PROJECT_PATH}/.env`) return true;
        return false;
      });
      fsMocked.statSync.mockReturnValue({
        isDirectory: () => false,
      } as ReturnType<typeof fsMod.statSync>);

      const group: RegisteredGroup = {
        name: 'App',
        folder: 'app-group',
        trigger: '@Bot',
        added_at: new Date().toISOString(),
        projectId: 'proj-shadow',
      };

      const mounts = await runAndCaptureMounts(group, true);

      const envShadow = mounts.find(
        (m) => m.containerPath === '/workspace/project/.env',
      );
      expect(envShadow).toBeDefined();
      expect(envShadow!.hostPath).toBe('/dev/null');
      expect(envShadow!.readonly).toBe(true);
    });

    it('shadows .aws/credentials dir with empty tmpdir', async () => {
      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
      const awsDir = `${PROJECT_PATH}/credentials`;

      fsMocked.existsSync.mockImplementation((p: unknown) => {
        const s = p as string;
        if (s === PROJECT_PATH) return true;
        if (s === awsDir) return true;
        return false;
      });
      fsMocked.statSync.mockImplementation((p: unknown) => {
        if (p === awsDir)
          return { isDirectory: () => true } as ReturnType<
            typeof fsMod.statSync
          >;
        return { isDirectory: () => false } as ReturnType<
          typeof fsMod.statSync
        >;
      });

      const group: RegisteredGroup = {
        name: 'App',
        folder: 'app-group',
        trigger: '@Bot',
        added_at: new Date().toISOString(),
        projectId: 'proj-shadow',
      };

      const mounts = await runAndCaptureMounts(group, true);

      const credShadow = mounts.find(
        (m) => m.containerPath === '/workspace/project/credentials',
      );
      expect(credShadow).toBeDefined();
      expect(credShadow!.readonly).toBe(true);
      // Host path should be an empty shadow dir (not /dev/null for dirs)
      expect(credShadow!.hostPath).not.toBe('/dev/null');
      expect(credShadow!.hostPath).toContain('project-shadows');
    });

    it('creates shadow dir with mkdirSync for sensitive directories', async () => {
      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
      const secretsDir = `${PROJECT_PATH}/secrets`;

      fsMocked.existsSync.mockImplementation((p: unknown) => {
        const s = p as string;
        if (s === PROJECT_PATH) return true;
        if (s === secretsDir) return true;
        return false;
      });
      fsMocked.statSync.mockImplementation((p: unknown) => {
        if (p === secretsDir)
          return { isDirectory: () => true } as ReturnType<
            typeof fsMod.statSync
          >;
        return { isDirectory: () => false } as ReturnType<
          typeof fsMod.statSync
        >;
      });

      const group: RegisteredGroup = {
        name: 'App',
        folder: 'app-group',
        trigger: '@Bot',
        added_at: new Date().toISOString(),
        projectId: 'proj-shadow',
      };

      fakeProc = createFakeProcess();
      setImmediate(() => fakeProc.emit('close', 0));
      vi.mocked(childProcess.spawn).mockReturnValue(
        fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
      );

      await runContainerAgent(
        group,
        {
          prompt: 'test',
          groupFolder: group.folder,
          chatJid: 'x@g.us',
          isControlGroup: true,
        },
        () => {},
      );

      const mkdirCalls = fsMocked.mkdirSync.mock.calls.map(
        (c: unknown[]) => c[0] as string,
      );
      expect(
        mkdirCalls.some((p: string) => p.includes('project-shadows')),
      ).toBe(true);
    });
  },
);

describe.skipIf(onWindows)(
  'buildVolumeMounts — agent-runner source mount',
  () => {
    beforeEach(() => {
      vi.useRealTimers();
      fakeProc = createFakeProcess();

      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
      fsMocked.existsSync.mockReset();
      fsMocked.existsSync.mockReturnValue(false);
      fsMocked.mkdirSync.mockReset();
      fsMocked.writeFileSync.mockReset();
      fsMocked.readdirSync.mockReset();
      fsMocked.readdirSync.mockReturnValue([]);
      fsMocked.statSync.mockReset();
      fsMocked.statSync.mockReturnValue({
        isDirectory: () => false,
      } as ReturnType<typeof fsMod.statSync>);

      vi.mocked(getProjectById).mockReturnValue(undefined);
      vi.mocked(childProcess.spawn).mockReturnValue(
        fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
      );
    });

    it('mounts agent-runner source read-only at /app/src', async () => {
      const group: RegisteredGroup = {
        name: 'Main',
        folder: 'main-group',
        trigger: '@Deus',
        added_at: new Date().toISOString(),
      };

      const mounts = await runAndCaptureMounts(group, true);

      const appSrcMount = mounts.find((m) => m.containerPath === '/app/src');
      expect(appSrcMount).toBeDefined();
      expect(appSrcMount!.readonly).toBe(true);
      expect(appSrcMount!.hostPath).toContain('agent-runner-src');
    });

    it('copies agent-runner source when source dir exists', async () => {
      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
      const agentSrc = `${process.cwd()}/container/agent-runner/src`;

      fsMocked.existsSync.mockImplementation((p: unknown) => p === agentSrc);

      const cpSyncMock = vi.fn();
      (fsMocked as Record<string, unknown>).cpSync = cpSyncMock;

      const group: RegisteredGroup = {
        name: 'Main',
        folder: 'main-group',
        trigger: '@Deus',
        added_at: new Date().toISOString(),
      };

      fakeProc = createFakeProcess();
      setImmediate(() => fakeProc.emit('close', 0));
      vi.mocked(childProcess.spawn).mockReturnValue(
        fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
      );

      await runContainerAgent(
        group,
        {
          prompt: 'test',
          groupFolder: group.folder,
          chatJid: 'x@g.us',
          isControlGroup: true,
        },
        () => {},
      );

      expect(cpSyncMock).toHaveBeenCalledWith(
        agentSrc,
        expect.stringContaining('agent-runner-src'),
        { recursive: true },
      );
    });
  },
);

describe.skipIf(onWindows)('OAuth session-based auth', () => {
  beforeEach(() => {
    vi.useRealTimers();
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
    fsMocked.existsSync.mockReset();
    fsMocked.existsSync.mockReturnValue(false);
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();
    fsMocked.statSync.mockReturnValue({
      isDirectory: () => false,
    } as ReturnType<typeof fsMod.statSync>);

    vi.mocked(getProjectById).mockReturnValue(undefined);
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );
  });

  it('does NOT set CLAUDE_CODE_OAUTH_TOKEN env var in OAuth mode', async () => {
    const { detectAuthMode } = await import('./credential-proxy.js');
    vi.mocked(detectAuthMode).mockReturnValue('oauth');

    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isControlGroup: true,
    };

    fakeProc = createFakeProcess();
    setImmediate(() => fakeProc.emit('close', 0));
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );

    await runContainerAgent(
      group,
      {
        prompt: 'test',
        groupFolder: group.folder,
        chatJid: 'x@g.us',
        isControlGroup: true,
      },
      () => {},
    );

    const spawnMock = vi.mocked(childProcess.spawn);
    const lastCall = spawnMock.mock.calls[spawnMock.mock.calls.length - 1];
    const args = lastCall[1] as string[];

    // Should NOT contain CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY
    expect(args.join(' ')).not.toContain('CLAUDE_CODE_OAUTH_TOKEN');
    expect(args.join(' ')).not.toContain('ANTHROPIC_API_KEY');

    // Restore default
    vi.mocked(detectAuthMode).mockReturnValue('api-key');
  });
});

describe.skipIf(onWindows)('llama-cpp backend container env', () => {
  beforeEach(() => {
    vi.useRealTimers();
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
    fsMocked.existsSync.mockReset();
    fsMocked.existsSync.mockReturnValue(false);
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();
    fsMocked.statSync.mockReturnValue({
      isDirectory: () => false,
    } as ReturnType<typeof fsMod.statSync>);

    vi.mocked(getProjectById).mockReturnValue(undefined);
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );
  });

  it('injects LLAMA_CPP env vars, bypasses credential proxy, no Anthropic/OpenAI env vars', async () => {
    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isControlGroup: true,
    };

    setImmediate(() => fakeProc.emit('close', 0));

    await runContainerAgent(
      group,
      {
        prompt: 'test',
        backend: 'llama-cpp',
        groupFolder: group.folder,
        chatJid: 'x@g.us',
        isControlGroup: true,
      },
      () => {},
    );

    const spawnMock = vi.mocked(childProcess.spawn);
    const lastCall = spawnMock.mock.calls[spawnMock.mock.calls.length - 1];
    const args = lastCall[1] as string[];
    const joined = args.join(' ');

    // llama-cpp talks to host gateway directly on its own port — NOT the
    // credential proxy port. Crucial security invariant: this also asserts
    // the credential proxy is NOT in the URL (no `:3001` substring).
    expect(joined).toMatch(
      /LLAMA_CPP_BASE_URL=http:\/\/host\.docker\.internal:\d+\/v1/,
    );
    expect(joined).not.toContain(
      'LLAMA_CPP_BASE_URL=http://host.docker.internal:3001',
    );
    expect(args).toContain('LLAMA_CPP_API_KEY=placeholder');
    // No OpenAI or Anthropic env vars leak into the llama-cpp container.
    expect(joined).not.toContain('OPENAI_BASE_URL=');
    expect(joined).not.toContain('OPENAI_API_KEY=');
    expect(joined).not.toContain('ANTHROPIC_BASE_URL=');
    expect(joined).not.toContain('ANTHROPIC_API_KEY=');
  });
});

describe.skipIf(onWindows)('OpenAI backend container env', () => {
  beforeEach(() => {
    vi.useRealTimers();
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
    fsMocked.existsSync.mockReset();
    fsMocked.existsSync.mockReturnValue(false);
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();
    fsMocked.statSync.mockReturnValue({
      isDirectory: () => false,
    } as ReturnType<typeof fsMod.statSync>);

    vi.mocked(getProjectById).mockReturnValue(undefined);
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );
  });

  it('injects OpenAI proxy env vars without Anthropic env vars', async () => {
    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isControlGroup: true,
    };

    setImmediate(() => fakeProc.emit('close', 0));

    await runContainerAgent(
      group,
      {
        prompt: 'test',
        backend: 'openai',
        groupFolder: group.folder,
        chatJid: 'x@g.us',
        isControlGroup: true,
      },
      () => {},
    );

    const spawnMock = vi.mocked(childProcess.spawn);
    const lastCall = spawnMock.mock.calls[spawnMock.mock.calls.length - 1];
    const args = lastCall[1] as string[];

    expect(args).toContain(
      'OPENAI_BASE_URL=http://host.docker.internal:3001/openai',
    );
    expect(args).toContain('OPENAI_API_KEY=placeholder');
    expect(args).toContain('DEUS_OPENAI_MODEL=gpt-test-model');
    expect(args).toContain('DEUS_CONTEXT_FILE_MAX_CHARS=12345');
    expect(args.join(' ')).not.toContain('ANTHROPIC_BASE_URL=');
    expect(args.join(' ')).not.toContain('ANTHROPIC_API_KEY=');
  });
});

describe.skipIf(onWindows)('Backend parity — system-level equivalence', () => {
  let claudeArgs: string[];
  let openaiArgs: string[];
  let llamaCppArgs: string[];

  beforeAll(async () => {
    fakeProc = createFakeProcess();

    const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
    fsMocked.existsSync.mockReset();
    fsMocked.existsSync.mockReturnValue(false);
    fsMocked.mkdirSync.mockReset();
    fsMocked.writeFileSync.mockReset();
    fsMocked.readdirSync.mockReset();
    fsMocked.readdirSync.mockReturnValue([]);
    fsMocked.statSync.mockReset();
    fsMocked.statSync.mockReturnValue({
      isDirectory: () => false,
    } as ReturnType<typeof fsMod.statSync>);

    vi.mocked(getProjectById).mockReturnValue(undefined);
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );
    const { detectAuthMode } = await import('./credential-proxy.js');
    vi.mocked(detectAuthMode).mockReturnValue('api-key');

    const group: RegisteredGroup = {
      name: 'Main',
      folder: 'main-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      isControlGroup: true,
    };

    // Capture Claude backend args
    setImmediate(() => fakeProc.emit('close', 0));
    await runContainerAgent(
      group,
      {
        prompt: 'parity-test',
        groupFolder: group.folder,
        chatJid: 'x@g.us',
        isControlGroup: true,
      },
      () => {},
    );
    const spawnMock = vi.mocked(childProcess.spawn);
    claudeArgs = spawnMock.mock.calls[
      spawnMock.mock.calls.length - 1
    ][1] as string[];

    // Capture OpenAI backend args
    fakeProc = createFakeProcess();
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );
    setImmediate(() => fakeProc.emit('close', 0));
    await runContainerAgent(
      group,
      {
        prompt: 'parity-test',
        backend: 'openai',
        groupFolder: group.folder,
        chatJid: 'x@g.us',
        isControlGroup: true,
      },
      () => {},
    );
    openaiArgs = spawnMock.mock.calls[
      spawnMock.mock.calls.length - 1
    ][1] as string[];

    // Capture llama-cpp backend args (third tier: local opt-in, no proxy)
    fakeProc = createFakeProcess();
    vi.mocked(childProcess.spawn).mockReturnValue(
      fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
    );
    setImmediate(() => fakeProc.emit('close', 0));
    await runContainerAgent(
      group,
      {
        prompt: 'parity-test',
        backend: 'llama-cpp',
        groupFolder: group.folder,
        chatJid: 'x@g.us',
        isControlGroup: true,
      },
      () => {},
    );
    llamaCppArgs = spawnMock.mock.calls[
      spawnMock.mock.calls.length - 1
    ][1] as string[];
  });

  // Helper: extract env vars from Docker args (items after -e flags)
  function extractEnvVars(args: string[]): Map<string, string> {
    const envs = new Map<string, string>();
    for (let i = 0; i < args.length; i++) {
      if (args[i] === '-e' && i + 1 < args.length) {
        const [key, ...rest] = args[i + 1].split('=');
        envs.set(key, rest.join('='));
      }
    }
    return envs;
  }

  // Helper: extract volume mounts from Docker args
  function extractMounts(args: string[]): string[] {
    const mounts: string[] = [];
    for (let i = 0; i < args.length; i++) {
      if (args[i] === '-v' && i + 1 < args.length) {
        // Normalize host path prefix to make comparison stable
        mounts.push(args[i + 1].replace(/.*:/, 'HOST:'));
      }
      if (args[i] === '--mount' && i + 1 < args.length) {
        mounts.push(args[i + 1].replace(/source=[^,]*/g, 'source=HOST'));
      }
    }
    return mounts;
  }

  it('uses the same container image', () => {
    const claudeImage = claudeArgs[claudeArgs.length - 1];
    const openaiImage = openaiArgs[openaiArgs.length - 1];
    expect(claudeImage).toBe(openaiImage);
    expect(claudeImage).toBe('deus-agent:latest');
  });

  it('uses the same volume mount paths', () => {
    const claudeMounts = extractMounts(claudeArgs);
    const openaiMounts = extractMounts(openaiArgs);
    expect(claudeMounts).toEqual(openaiMounts);
  });

  it('shares all non-auth env vars', () => {
    const claudeEnv = extractEnvVars(claudeArgs);
    const openaiEnv = extractEnvVars(openaiArgs);

    const authKeys = new Set([
      'ANTHROPIC_BASE_URL',
      'ANTHROPIC_API_KEY',
      'CLAUDE_CODE_OAUTH_TOKEN',
      'OPENAI_BASE_URL',
      'OPENAI_API_KEY',
      'DEUS_OPENAI_MODEL',
    ]);

    // Per-dispatch, non-deterministic env (value is `${group}-${Date.now()}`).
    // Shared identically across backends by construction, but its VALUE differs
    // between two separate dispatches, so exclude it from the value-parity check
    // (LIA-154 DEUS_INTERACTION_ID).
    const perDispatchKeys = new Set(['DEUS_INTERACTION_ID']);
    const excluded = (k: string) => authKeys.has(k) || perDispatchKeys.has(k);

    // All non-auth, non-per-dispatch env vars must be identical
    const claudeNonAuth = new Map([...claudeEnv].filter(([k]) => !excluded(k)));
    const openaiNonAuth = new Map([...openaiEnv].filter(([k]) => !excluded(k)));
    expect(claudeNonAuth).toEqual(openaiNonAuth);
  });

  it('only differs in auth-related env vars', () => {
    const claudeEnv = extractEnvVars(claudeArgs);
    const openaiEnv = extractEnvVars(openaiArgs);

    // Claude path has Anthropic auth
    expect(claudeEnv.has('ANTHROPIC_BASE_URL')).toBe(true);
    expect(claudeEnv.has('ANTHROPIC_API_KEY')).toBe(true);
    expect(claudeEnv.has('OPENAI_API_KEY')).toBe(false);

    // OpenAI path has OpenAI auth
    expect(openaiEnv.has('OPENAI_BASE_URL')).toBe(true);
    expect(openaiEnv.has('OPENAI_API_KEY')).toBe(true);
    expect(openaiEnv.has('ANTHROPIC_API_KEY')).toBe(false);
  });

  it('all backends use placeholder credentials (never real keys)', () => {
    const claudeEnv = extractEnvVars(claudeArgs);
    const openaiEnv = extractEnvVars(openaiArgs);
    const llamaCppEnv = extractEnvVars(llamaCppArgs);

    expect(claudeEnv.get('ANTHROPIC_API_KEY')).toBe('placeholder');
    expect(openaiEnv.get('OPENAI_API_KEY')).toBe('placeholder');
    expect(llamaCppEnv.get('LLAMA_CPP_API_KEY')).toBe('placeholder');
  });

  // Remote-backend tier: Claude and OpenAI MUST route through the credential
  // proxy (the proxy injects real secrets that never live in the container).
  it('remote backends route through credential proxy', () => {
    const claudeEnv = extractEnvVars(claudeArgs);
    const openaiEnv = extractEnvVars(openaiArgs);

    expect(claudeEnv.get('ANTHROPIC_BASE_URL')).toContain(':3001');
    expect(openaiEnv.get('OPENAI_BASE_URL')).toContain(':3001');
  });

  // Local-backend tier: llama-cpp MUST NOT route through the credential
  // proxy. It talks to the host gateway directly because llama-server has
  // no auth — the proxy is irrelevant. Asserting the absence here closes
  // the loop with the remote-backend invariant above.
  it('local backend (llama-cpp) bypasses credential proxy', () => {
    const llamaCppEnv = extractEnvVars(llamaCppArgs);

    const baseUrl = llamaCppEnv.get('LLAMA_CPP_BASE_URL') ?? '';
    expect(baseUrl).not.toContain(':3001');
    expect(baseUrl).toMatch(/host\.docker\.internal:\d+\/v1/);
  });

  it('all backends receive DEUS_PROXY_TOKEN for tool proxy authentication', () => {
    const claudeEnv = extractEnvVars(claudeArgs);
    const openaiEnv = extractEnvVars(openaiArgs);
    const llamaCppEnv = extractEnvVars(llamaCppArgs);

    expect(claudeEnv.get('DEUS_PROXY_TOKEN')).toBe(TEST_PROXY_TOKEN);
    expect(openaiEnv.get('DEUS_PROXY_TOKEN')).toBe(TEST_PROXY_TOKEN);
    expect(llamaCppEnv.get('DEUS_PROXY_TOKEN')).toBe(TEST_PROXY_TOKEN);
  });
});

describe.skipIf(onWindows)(
  'optimized-prompt injection (LIA-131 Phase 2)',
  () => {
    beforeEach(() => {
      vi.useRealTimers();
      const fsMocked = vi.mocked((fsMod as any).default as typeof fsMod);
      fsMocked.existsSync.mockReset();
      fsMocked.existsSync.mockReturnValue(false);
      fsMocked.mkdirSync.mockReset();
      fsMocked.writeFileSync.mockReset();
      fsMocked.readdirSync.mockReset();
      fsMocked.readdirSync.mockReturnValue([]);
      fsMocked.statSync.mockReset();
      fsMocked.statSync.mockReturnValue({
        isDirectory: () => false,
      } as ReturnType<typeof fsMod.statSync>);
      vi.mocked(getProjectById).mockReturnValue(undefined);
      vi.mocked(getReflections).mockResolvedValue({
        block: '',
        reflectionIds: [],
      });
      vi.mocked(getActivePrompt).mockResolvedValue({ block: '' });
    });

    /** Run the agent and return the JSON payload written to the container stdin. */
    async function runAndCaptureInput(): Promise<{ prompt: string }> {
      const group: RegisteredGroup = {
        name: 'Main',
        folder: 'main-group',
        trigger: '@Deus',
        added_at: new Date().toISOString(),
      };
      fakeProc = createFakeProcess();
      let written = '';
      fakeProc.stdin.on('data', (chunk: Buffer) => {
        written += chunk.toString();
      });
      setImmediate(() => fakeProc.emit('close', 0));
      vi.mocked(childProcess.spawn).mockReturnValue(
        fakeProc as unknown as ReturnType<typeof childProcess.spawn>,
      );
      await runContainerAgent(
        group,
        {
          prompt: 'ORIGINAL_PROMPT',
          groupFolder: group.folder,
          chatJid: 'x@g.us',
          isControlGroup: false,
        },
        () => {},
      );
      return JSON.parse(written);
    }

    it('does not alter the prompt when no optimized block is returned', async () => {
      const input = await runAndCaptureInput();
      expect(input.prompt).toBe('ORIGINAL_PROMPT');
      expect(vi.mocked(getActivePrompt)).toHaveBeenCalledWith('qa');
    });

    it('prepends the optimized block, composing WITH the reflections block', async () => {
      vi.mocked(getReflections).mockResolvedValue({
        block: 'REFLECTIONS_BLOCK',
        reflectionIds: ['r1'],
      });
      vi.mocked(getActivePrompt).mockResolvedValue({
        block: 'OPTIMIZED_BLOCK',
        artifactId: 'art-1',
        baselineScore: 0.7,
        optimizedScore: 0.88,
        sampleCount: 42,
      });
      const input = await runAndCaptureInput();
      // All three present — optimized composes WITH, does not replace, reflections.
      expect(input.prompt).toContain('OPTIMIZED_BLOCK');
      expect(input.prompt).toContain('REFLECTIONS_BLOCK');
      expect(input.prompt).toContain('ORIGINAL_PROMPT');
      // Order: optimized, then reflections, then the original prompt.
      expect(input.prompt.indexOf('OPTIMIZED_BLOCK')).toBeLessThan(
        input.prompt.indexOf('REFLECTIONS_BLOCK'),
      );
      expect(input.prompt.indexOf('REFLECTIONS_BLOCK')).toBeLessThan(
        input.prompt.indexOf('ORIGINAL_PROMPT'),
      );
    });
  },
);

// ---------------------------------------------------------------------------
// readToolCalls — LIA-154 per-interaction structured tool-call read-back
// (fs is mocked in this file; drive readFileSync to exercise parse/skip)
// ---------------------------------------------------------------------------
describe('readToolCalls (LIA-154)', () => {
  const fsMocked = vi.mocked(
    (fsMod as unknown as { default: typeof fsMod }).default,
  );
  const logsDir = '/group/logs';

  afterEach(() => {
    fsMocked.readFileSync.mockReset();
    fsMocked.readFileSync.mockReturnValue('');
  });

  it('reads the per-interaction file path (logsDir/tool-calls/<safeId>.jsonl)', () => {
    fsMocked.readFileSync.mockClear();
    fsMocked.readFileSync.mockReturnValue('');
    readToolCalls(logsDir, 'grp/main-123');
    const calledPath = String(fsMocked.readFileSync.mock.calls[0][0]);
    // path separators in the id are sanitized so the filename is one segment
    expect(calledPath).toContain('tool-calls');
    expect(calledPath).toContain('grp_main-123.jsonl');
  });

  it('returns [] when the file does not exist', () => {
    fsMocked.readFileSync.mockImplementation(() => {
      const e = new Error('ENOENT') as NodeJS.ErrnoException;
      e.code = 'ENOENT';
      throw e;
    });
    expect(readToolCalls(logsDir, 'g-1')).toEqual([]);
  });

  it('returns every record in the interaction file (no cross-interaction filter needed)', () => {
    fsMocked.readFileSync.mockReturnValue(
      [
        JSON.stringify({ name: 'Read', file_path: '/a.ts', is_error: false }),
        JSON.stringify({
          name: 'Bash',
          command: 'git status',
          is_error: false,
        }),
      ].join('\n'),
    );
    expect(readToolCalls(logsDir, 'g-1')).toEqual([
      { name: 'Read', file_path: '/a.ts', is_error: false },
      { name: 'Bash', command: 'git status', is_error: false },
    ]);
  });

  it('skips malformed/torn lines but keeps valid ones', () => {
    fsMocked.readFileSync.mockReturnValue(
      [
        JSON.stringify({ name: 'Read', file_path: '/a.ts' }),
        '{"name":"Bash","command":"git', // torn line
        '',
        JSON.stringify({ name: 'Edit', file_path: '/b.ts' }),
      ].join('\n'),
    );
    expect(readToolCalls(logsDir, 'g-1')).toEqual([
      { name: 'Read', file_path: '/a.ts' },
      { name: 'Edit', file_path: '/b.ts' },
    ]);
  });
});

// ---------------------------------------------------------------------------
// readAvailableTools — LIA-154 per-interaction offered-tool manifest read-back
// ---------------------------------------------------------------------------
describe('readAvailableTools (LIA-154)', () => {
  const fsMocked = vi.mocked(
    (fsMod as unknown as { default: typeof fsMod }).default,
  );
  const logsDir = '/group/logs';

  afterEach(() => {
    fsMocked.readFileSync.mockReset();
    fsMocked.readFileSync.mockReturnValue('');
  });

  it('reads the per-interaction file (logsDir/available-tools/<safeId>.json)', () => {
    fsMocked.readFileSync.mockClear();
    fsMocked.readFileSync.mockReturnValue('[]');
    readAvailableTools(logsDir, 'grp/main-123');
    const calledPath = String(fsMocked.readFileSync.mock.calls[0][0]);
    expect(calledPath).toContain('available-tools');
    // path separators in the id are sanitized so the filename is one segment
    expect(calledPath).toContain('grp_main-123.json');
  });

  it('returns the parsed string array', () => {
    fsMocked.readFileSync.mockReturnValue(
      JSON.stringify(['Bash', 'Read', 'mcp__deus__*']),
    );
    expect(readAvailableTools(logsDir, 'g-1')).toEqual([
      'Bash',
      'Read',
      'mcp__deus__*',
    ]);
  });

  it('returns [] when the file does not exist', () => {
    fsMocked.readFileSync.mockImplementation(() => {
      const e = new Error('ENOENT') as NodeJS.ErrnoException;
      e.code = 'ENOENT';
      throw e;
    });
    expect(readAvailableTools(logsDir, 'g-1')).toEqual([]);
  });

  it('returns [] on malformed JSON', () => {
    fsMocked.readFileSync.mockReturnValue('not json [');
    expect(readAvailableTools(logsDir, 'g-1')).toEqual([]);
  });

  it('filters out non-string entries (defensive)', () => {
    fsMocked.readFileSync.mockReturnValue(
      JSON.stringify(['Bash', 42, null, 'Read']),
    );
    expect(readAvailableTools(logsDir, 'g-1')).toEqual(['Bash', 'Read']);
  });

  it('returns [] when the JSON is not an array', () => {
    fsMocked.readFileSync.mockReturnValue(JSON.stringify({ tools: ['Bash'] }));
    expect(readAvailableTools(logsDir, 'g-1')).toEqual([]);
  });
});
