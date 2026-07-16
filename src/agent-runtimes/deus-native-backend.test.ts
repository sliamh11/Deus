import { execFile } from 'node:child_process';

import { describe, it, expect, vi, beforeEach } from 'vitest';

import type { ContainerRuntimeDeps } from './container-backend.js';
import type { RuntimeEvent } from './types.js';
import {
  RuntimeActivityBroadcaster,
  withRuntimeActivityBroadcast,
  type RuntimeActivityEnvelope,
} from './activity-broadcaster.js';

// Hoisted mocks — no live network call, no live model call anywhere in this
// file, matching every other spike/adapter test's hermeticity convention.
// checkpointerStub: a minimal fake BaseCheckpointSaver surface (getTuple is
// the only method runTurn itself calls; the rest of the saver is only ever
// exercised through the REAL LangGraph engine, which this file mocks away
// via createAgentMock). A single hoisted instance so the createAgent-wiring
// assertion below can prove the EXACT object was passed through.
const {
  createAgentMock,
  invokeMock,
  checkpointerStub,
  buildMiddlewareStackSpy,
  buildNestedDispatchToolMock,
} = vi.hoisted(() => ({
  createAgentMock: vi.fn(),
  invokeMock: vi.fn(),
  checkpointerStub: { getTuple: async () => undefined },
  // D1 (LIA-415): a pass-through SPY (implementation assigned in the
  // module mock below — always the REAL buildMiddlewareStack), so tests
  // can inspect the deps runTurn supplies (memoryRequest presence/shape)
  // without changing any behavior. Use mockClear() only, never
  // mockReset() — reset would drop the real implementation.
  buildMiddlewareStackSpy: vi.fn(),
  // C1 (LIA-409): a stub (never delegates to the real nested-dispatch
  // machinery — no live dispatcher needed for any test in this file) that
  // records the CreateNestedDispatcherDeps runTurn supplies, so a test can
  // call `buildChildMiddleware()` directly and inspect the REAL wardens
  // middleware it returns, proving `wardenCwd` reaches nested-dispatch
  // children too.
  buildNestedDispatchToolMock: vi.fn((_deps?: unknown) => ({
    name: 'dispatch_nested_agent',
  })),
}));

vi.mock('langchain', async (importOriginal) => {
  const actual = await importOriginal<typeof import('langchain')>();
  return {
    // Keep the real module (middleware-stack.ts needs the real
    // createMiddleware — pure construction, no network) and stub only the
    // agent factory, which is what would otherwise hit the model.
    ...actual,
    createAgent: createAgentMock,
  };
});

class FakeChatAnthropic {
  config: unknown;
  constructor(config: unknown) {
    this.config = config;
  }
}
vi.mock('@langchain/anthropic', () => ({
  // Constructor-only stub — never makes a network call, just records the
  // config it was built with so a test can assert on it if needed.
  ChatAnthropic: FakeChatAnthropic,
}));

class FakeAnthropicClient {
  config: unknown;
  constructor(config: unknown) {
    this.config = config;
  }
}
vi.mock('@anthropic-ai/sdk', () => ({
  default: FakeAnthropicClient,
}));

const detectAuthModeMock = vi.fn(() => 'api-key' as 'api-key' | 'oauth');
vi.mock('../credential-proxy.js', () => ({
  detectAuthMode: () => detectAuthModeMock(),
}));

const getOrCreateGroupTokenMock = vi.fn(
  (_folder?: string) => 'fake-proxy-token',
);
vi.mock('../group-tokens.js', () => ({
  getOrCreateGroupToken: (folder?: string) => getOrCreateGroupTokenMock(folder),
}));

// buildSafeTools is exercised directly by the oracle test — here it's
// enough to know runTurn calls it and passes the result to createAgent.
vi.mock('./tool-broker-langchain-adapter.js', () => ({
  buildSafeTools: vi.fn(() => []),
}));

// B4 (LIA-404): runTurn now unconditionally calls getCheckpointer() (both
// for the pre-invoke getTuple read and the createAgent wiring) — without
// this stub, EVERY runTurn call in this file would touch (or throw on) real
// filesystem state, since SqliteSaver.fromConnString opens its file eagerly
// and store/ doesn't exist on a clean checkout. A simple stub is sufficient
// here (getTuple -> undefined = "always a new session", today's behavior);
// the dedicated integration test file is where REAL checkpointer behavior
// is exercised.
vi.mock('./checkpointer.js', () => ({
  getCheckpointer: () => checkpointerStub,
}));

// D1 (LIA-415): keep the real module (real createMiddleware-built layers,
// real config parsing) and swap ONLY buildMiddlewareStack for a delegating
// spy — production behavior is unchanged, but the deps runTurn passes
// (memoryRequest for control-group turns) become inspectable.
vi.mock('./middleware-stack.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./middleware-stack.js')>();
  buildMiddlewareStackSpy.mockImplementation(actual.buildMiddlewareStack);
  return { ...actual, buildMiddlewareStack: buildMiddlewareStackSpy };
});

// C1 (LIA-409): the real `wardens` middleware built inside `runTurn` resolves
// its repo root via a REAL `git rev-parse --git-common-dir` call (left
// unmocked here — this worktree genuinely is a linked worktree, so the real
// call is fast, deterministic, and exercises the actual production
// behavior). Only `execFile` (the Python gate subprocess) is replaced with a
// controllable fake — no test in this file exercises a real `apply_patch`/
// commit-shaped-`Bash` wardens trigger except the dedicated test below,
// which sets its own implementation.
vi.mock('node:child_process', async () => {
  const actual =
    await vi.importActual<typeof import('node:child_process')>(
      'node:child_process',
    );
  return {
    ...actual,
    execFile: vi.fn(),
  };
});

// C1 (LIA-409): stub out the real nested-dispatch tool builder (see the
// hoisted `buildNestedDispatchToolMock` above) — no test in this file needs
// a live dispatcher, only the `CreateNestedDispatcherDeps` runTurn supplies
// it with.
vi.mock('./nested-dispatch-tool.js', () => ({
  buildNestedDispatchTool: buildNestedDispatchToolMock,
}));

const { createDeusNativeRuntime } = await import('./deus-native-backend.js');

const stubDeps: ContainerRuntimeDeps = {
  resolveGroup: () => undefined,
  assistantName: 'Deus',
  registerProcess: () => {},
};

describe('DeusNativeBackend', () => {
  const backend = createDeusNativeRuntime(stubDeps);

  beforeEach(() => {
    createAgentMock.mockReset();
    invokeMock.mockReset();
    detectAuthModeMock.mockReturnValue('api-key');
    // .mockReset() (not just .mockReturnValue()) so both call history AND any
    // .mockImplementation() override from a prior test (e.g. the token-mint-
    // failure test below) don't leak into the next test's assertions -- a
    // gap this file had until a new .not.toHaveBeenCalled() assertion
    // surfaced it.
    getOrCreateGroupTokenMock.mockReset();
    getOrCreateGroupTokenMock.mockReturnValue('fake-proxy-token');
  });

  it('returns correct name', () => {
    expect(backend.name()).toBe('deus-native');
  });

  it('returns correct capabilities', () => {
    const caps = backend.capabilities();
    // Security boundary — see docs/decisions/deus-v2-langchain-runtime.md.
    expect(caps.shell).toBe(false);
    expect(caps.filesystem).toBe(false);
    expect(caps.web).toBe(true);
    expect(caps.multimodal).toBe(false);
    expect(caps.handoffs).toBe(false);
    // B4 (LIA-404): checkpointer-backed sessions landed — a stored
    // session_id is a real, resumable thread identifier.
    expect(caps.persistent_sessions).toBe(true);
    expect(caps.tool_streaming).toBe(false);
  });

  it('startOrResume returns default session ref', async () => {
    const ref = await backend.startOrResume({
      prompt: 'test',
      groupFolder: 'test-folder',
      chatJid: 'test@g.us',
      isControlGroup: false,
    });

    expect(ref.backend).toBe('deus-native');
    expect(ref.session_id).toBe('');
  });

  it('close resolves without error', async () => {
    await expect(
      backend.close({ backend: 'deus-native', session_id: 'deus-native-abc' }),
    ).resolves.toBeUndefined();
  });

  it('runTurn returns error when the proxy token mint fails (analogous to "group not found")', async () => {
    getOrCreateGroupTokenMock.mockImplementation(() => {
      throw new Error(
        'getOrCreateGroupToken: folder is a publicIngress folder',
      );
    });

    const events: RuntimeEvent[] = [];
    const result = await backend.runTurn(
      {
        prompt: 'test',
        groupFolder: 'nonexistent',
        chatJid: 'test@g.us',
        isControlGroup: false,
      },
      { backend: 'deus-native', session_id: '' },
      (event) => {
        events.push(event);
      },
    );

    expect(result.status).toBe('error');
    expect(result.result).toBeNull();
    expect(result.error).toContain('publicIngress');
    // Never throws out of runTurn — matches ContainerRuntime.runTurn's
    // never-throw contract. No terminal success events were emitted.
    expect(events.some((e) => e.type === 'turn_complete')).toBe(false);
    expect(createAgentMock).not.toHaveBeenCalled();
  });

  it('runTurn refuses a publicIngress group before minting a token or building a model (fail-closed guard, added after code-review)', async () => {
    // Mirrors buildContainerArgs's own fail-closed check (container-runner.ts)
    // for the container path -- deus-native never calls that function, so
    // this is its own independent enforcement of the same rule: a
    // publicIngress (webhook-originated) group must never reach a non-Claude
    // backend's unscoped tool surface.
    const publicIngressDeps: ContainerRuntimeDeps = {
      resolveGroup: () => ({
        name: 'webhook group',
        folder: 'webhook-folder',
        trigger: 'auto',
        added_at: new Date().toISOString(),
        containerConfig: { publicIngress: true },
      }),
      assistantName: 'Deus',
      registerProcess: () => {},
    };
    const publicIngressBackend = createDeusNativeRuntime(publicIngressDeps);

    const result = await publicIngressBackend.runTurn(
      {
        prompt: 'test',
        groupFolder: 'webhook-folder',
        chatJid: 'test@g.us',
        isControlGroup: false,
      },
      { backend: 'deus-native', session_id: '' },
      () => {},
    );

    expect(result.status).toBe('error');
    expect(result.result).toBeNull();
    expect(result.error).toContain('publicIngress');
    expect(result.error).toContain('deus-native');
    // The guard must fire BEFORE any token mint or model construction --
    // never a "fail after doing the unsafe thing" check.
    expect(getOrCreateGroupTokenMock).not.toHaveBeenCalled();
    expect(createAgentMock).not.toHaveBeenCalled();
  });

  it('runTurn returns error when detectAuthMode / createAgent throws', async () => {
    createAgentMock.mockImplementation(() => {
      throw new Error('simulated createAgent failure');
    });

    const result = await backend.runTurn(
      {
        prompt: 'test',
        groupFolder: 'test-folder',
        chatJid: 'test@g.us',
        isControlGroup: false,
      },
      { backend: 'deus-native', session_id: '' },
      () => {},
    );

    expect(result.status).toBe('error');
    expect(result.result).toBeNull();
    expect(result.error).toContain('simulated createAgent failure');
  });

  it('runTurn maps a scripted createAgent response into the RunResult contract and emits the expected RuntimeEvent sequence', async () => {
    createAgentMock.mockReturnValue({ invoke: invokeMock });
    invokeMock.mockResolvedValue({
      messages: [
        { content: 'search the web for X' },
        {
          content: '',
          tool_calls: [{ name: 'web_search', args: { query: 'X' } }],
        },
        { content: 'Here is what I found about X.' },
      ],
    });

    const events: RuntimeEvent[] = [];
    const result = await backend.runTurn(
      {
        prompt: 'find X',
        groupFolder: 'test-folder',
        chatJid: 'test@g.us',
        isControlGroup: false,
      },
      { backend: 'deus-native', session_id: '' },
      (event) => {
        events.push(event);
      },
    );

    // Contract shape (RunResult).
    expect(result.status).toBe('success');
    expect(result.result).toBe('Here is what I found about X.');

    // Event sequence: the intermediate tool_call, then output_text, then
    // turn_complete — matching RuntimeEvent's union.
    const toolCallEvents = events.filter((e) => e.type === 'tool_call');
    expect(toolCallEvents).toHaveLength(1);
    expect(toolCallEvents[0]).toMatchObject({
      type: 'tool_call',
      name: 'web_search',
      arguments: { query: 'X' },
    });

    const outputTextEvents = events.filter((e) => e.type === 'output_text');
    expect(outputTextEvents).toHaveLength(1);
    expect(outputTextEvents[0]).toMatchObject({
      type: 'output_text',
      text: 'Here is what I found about X.',
    });

    expect(events.some((e) => e.type === 'turn_complete')).toBe(true);

    // Bare prompt only — no CLAUDE.md/AGENTS.md/persona context, per the
    // plan's explicit non-goal. B4 (LIA-404): invoke gains a second
    // argument — the checkpointer thread_id, which must be the SAME id the
    // RunResult.sessionRef carries (the session row references exactly the
    // checkpointer state this turn wrote).
    expect(invokeMock).toHaveBeenCalledWith(
      {
        messages: [{ role: 'user', content: 'find X' }],
      },
      { configurable: { thread_id: result.sessionRef?.session_id } },
    );

    // OAuth-vs-API-key branching happened via detectAuthMode, and a
    // per-group token was minted.
    expect(getOrCreateGroupTokenMock).toHaveBeenCalledWith('test-folder');

    // B2 (LIA-402): runTurn wires the middleware stack into createAgent in
    // canonical order — all four layers enabled by default (AC1 at the
    // runtime level; composition semantics are proven in
    // middleware-stack.test.ts's AC4 ordering test). B3 (LIA-403) appends
    // its prompt-lifecycle middleware LAST — appended, never prepended, so
    // B2's AC1-locked order is untouched.
    const createAgentArgs = createAgentMock.mock.calls[0]?.[0] as {
      middleware?: Array<{ name: string }>;
      checkpointer?: unknown;
    };
    expect(createAgentArgs.middleware?.map((m) => m.name)).toEqual([
      'permissions',
      'wardens',
      'memory',
      'telemetry',
      'prompt-lifecycle',
    ]);

    // B4 (LIA-404): the checkpointer is genuinely wired into createAgent —
    // identity check against the stub instance (toBe), not just "some field
    // is present", so a silently-dropped checkpointer can't false-negative
    // this ticket's core AC.
    expect(createAgentArgs.checkpointer).toBe(checkpointerStub);
  });

  it('branches to OAuth-mode client construction when detectAuthMode() returns "oauth"', async () => {
    detectAuthModeMock.mockReturnValue('oauth');
    createAgentMock.mockReturnValue({ invoke: invokeMock });
    invokeMock.mockResolvedValue({
      messages: [{ content: 'ok' }],
    });

    const result = await backend.runTurn(
      {
        prompt: 'hello',
        groupFolder: 'test-folder',
        chatJid: 'test@g.us',
        isControlGroup: false,
      },
      { backend: 'deus-native', session_id: '' },
      () => {},
    );

    expect(result.status).toBe('success');
    expect(detectAuthModeMock).toHaveBeenCalled();
  });
});

// ── B7 (LIA-407): permission-profile wiring at the PRODUCTION call site ───
//
// These tests prove the runContext.backendConfig.permissionProfile seam:
// omitted => the 'default' allow-all profile (today's behavior, unchanged);
// 'read-only' => the fail-closed read-only policy; an invalid value fails
// visibly BEFORE createAgent. createAgent is mocked (as everywhere in this
// file) but buildMiddlewareStack/createMiddleware are REAL, so the
// middleware objects captured off createAgentMock are the genuine
// permissions layer — invoking their wrapToolCall hook directly proves
// which policy production actually wired, not just which string was passed.

import { ToolMessage } from '@langchain/core/messages';

/** Minimal ToolCallRequest stand-in for direct wrapToolCall invocation
 *  (same shape as middleware-stack.test.ts's helper — the hook only reads
 *  toolCall.{name,id} and passes the request through). */
function makeToolCallRequest(name: string, args: Record<string, unknown>) {
  return {
    toolCall: { name, args, id: 'call_wiring_1', type: 'tool_call' as const },
    tool: undefined,
    state: {},
    runtime: {},
  };
}

async function invokeWrapToolCall(
  middleware: { wrapToolCall?: unknown },
  request: unknown,
  handler: (req: unknown) => ToolMessage,
): Promise<unknown> {
  const hook = middleware.wrapToolCall as (
    request: unknown,
    handler: unknown,
  ) => Promise<unknown> | unknown;
  return await hook(request, handler);
}

describe('DeusNativeBackend — permission profile wiring (B7/LIA-407)', () => {
  const backend = createDeusNativeRuntime(stubDeps);

  beforeEach(() => {
    createAgentMock.mockReset();
    invokeMock.mockReset();
    detectAuthModeMock.mockReturnValue('api-key');
    getOrCreateGroupTokenMock.mockReset();
    getOrCreateGroupTokenMock.mockReturnValue('fake-proxy-token');
  });

  async function runTurnWith(backendConfig?: Record<string, unknown>) {
    createAgentMock.mockReturnValue({ invoke: invokeMock });
    invokeMock.mockResolvedValue({ messages: [{ content: 'ok' }] });
    return backend.runTurn(
      {
        prompt: 'test',
        groupFolder: 'test-folder',
        chatJid: 'test@g.us',
        isControlGroup: false,
        ...(backendConfig !== undefined ? { backendConfig } : {}),
      },
      { backend: 'deus-native', session_id: '' },
      () => {},
    );
  }

  function capturedPermissionsMiddleware() {
    const args = createAgentMock.mock.calls[0]?.[0] as {
      middleware?: Array<{ name: string; wrapToolCall?: unknown }>;
    };
    const permissions = args.middleware?.[0];
    expect(permissions?.name).toBe('permissions');
    return permissions as { name: string; wrapToolCall?: unknown };
  }

  it("omitted backendConfig wires the default profile: a mutation-named call is still allowed (today's behavior, unchanged)", async () => {
    const result = await runTurnWith(undefined);
    expect(result.status).toBe('success');

    const permissions = capturedPermissionsMiddleware();
    const handler = vi.fn(
      () => new ToolMessage({ content: 'ran', tool_call_id: 'call_wiring_1' }),
    );
    await invokeWrapToolCall(
      permissions,
      makeToolCallRequest('write_file', { path: '/x', content: 'y' }),
      handler,
    );
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('backendConfig.permissionProfile "read-only" wires the read-only policy: mutation denied, read allowed — with canonical order retained', async () => {
    const result = await runTurnWith({ permissionProfile: 'read-only' });
    expect(result.status).toBe('success');

    // Canonical order is untouched by the profile selection (B2's AC1 +
    // B3's appended prompt-lifecycle).
    const createAgentArgs = createAgentMock.mock.calls[0]?.[0] as {
      middleware?: Array<{ name: string }>;
    };
    expect(createAgentArgs.middleware?.map((m) => m.name)).toEqual([
      'permissions',
      'wardens',
      'memory',
      'telemetry',
      'prompt-lifecycle',
    ]);

    const permissions = capturedPermissionsMiddleware();

    // A mutation tool is DENIED by the wired policy: handler untouched,
    // synthetic error ToolMessage naming the profile.
    const deniedHandler = vi.fn(
      () => new ToolMessage({ content: 'ran', tool_call_id: 'call_wiring_1' }),
    );
    const denial = (await invokeWrapToolCall(
      permissions,
      makeToolCallRequest('write_file', { path: '/x', content: 'y' }),
      deniedHandler,
    )) as ToolMessage;
    expect(deniedHandler).not.toHaveBeenCalled();
    expect(denial).toBeInstanceOf(ToolMessage);
    expect(denial.status).toBe('error');
    const content =
      typeof denial.content === 'string'
        ? denial.content
        : JSON.stringify(denial.content);
    expect(content).toContain('permission_denied');
    expect(content).toContain('read-only');

    // A read tool still flows through — today's web_search/web_fetch calls
    // remain allowed under the read-only profile (plan AC3).
    const allowedHandler = vi.fn(
      () => new ToolMessage({ content: 'ok', tool_call_id: 'call_wiring_1' }),
    );
    await invokeWrapToolCall(
      permissions,
      makeToolCallRequest('web_search', { query: 'x' }),
      allowedHandler,
    );
    expect(allowedHandler).toHaveBeenCalledTimes(1);
  });

  it('an unrecognized profile name fails visibly BEFORE createAgent (status error, never a silent fallback to default)', async () => {
    const result = await runTurnWith({ permissionProfile: 'totally-invalid' });
    expect(result.status).toBe('error');
    expect(result.result).toBeNull();
    expect(result.error).toContain(
      'unknown permission profile "totally-invalid"',
    );
    expect(createAgentMock).not.toHaveBeenCalled();
  });

  it('a non-string permissionProfile value fails visibly BEFORE createAgent', async () => {
    const result = await runTurnWith({ permissionProfile: 42 });
    expect(result.status).toBe('error');
    expect(result.error).toContain(
      'backendConfig.permissionProfile must be a string',
    );
    expect(createAgentMock).not.toHaveBeenCalled();
  });
});

// ── D1 (LIA-415): memory retrieval wiring at the PRODUCTION call site ─────
//
// buildMiddlewareStack is a delegating spy over the REAL implementation
// (see the module mock at the top of this file), so these tests inspect the
// genuine deps runTurn supplies: a control-group turn must carry the
// submitted prompt plus the backend-scoped session id into the memory
// layer, and a non-control-group turn must omit memoryRequest entirely
// (personal-vault isolation — the placeholder seam's documented leak
// concern). No live model, Python, vault, or Ollama call happens anywhere
// here: createAgent is mocked, so no beforeModel hook (and therefore no
// real subprocess adapter) ever fires.

import type { BuildMiddlewareStackDeps } from './middleware-stack.js';

describe('DeusNativeBackend — memory retrieval wiring (D1/LIA-415)', () => {
  const backend = createDeusNativeRuntime(stubDeps);

  beforeEach(() => {
    createAgentMock.mockReset();
    invokeMock.mockReset();
    detectAuthModeMock.mockReturnValue('api-key');
    getOrCreateGroupTokenMock.mockReset();
    getOrCreateGroupTokenMock.mockReturnValue('fake-proxy-token');
    // mockClear, NOT mockReset — the spy must keep delegating to the real
    // buildMiddlewareStack.
    buildMiddlewareStackSpy.mockClear();
  });

  async function runTurnAs(isControlGroup: boolean) {
    createAgentMock.mockReturnValue({ invoke: invokeMock });
    invokeMock.mockResolvedValue({ messages: [{ content: 'ok' }] });
    return backend.runTurn(
      {
        prompt: 'a generic fixture prompt about testing',
        groupFolder: 'test-folder',
        chatJid: 'test@g.us',
        isControlGroup,
      },
      { backend: 'deus-native', session_id: '' },
      () => {},
    );
  }

  function suppliedDeps(): BuildMiddlewareStackDeps {
    expect(buildMiddlewareStackSpy).toHaveBeenCalledTimes(1);
    // Guard before the cast: a future arg-order change to
    // buildMiddlewareStack must fail loudly here, not produce undefined
    // deps and vacuously-passing assertions downstream.
    const deps = buildMiddlewareStackSpy.mock.calls[0]?.[1];
    expect(deps).toBeDefined();
    return deps as BuildMiddlewareStackDeps;
  }

  it('a control-group turn supplies memoryRequest with the submitted prompt and the backend-scoped session id', async () => {
    const result = await runTurnAs(true);
    expect(result.status).toBe('success');

    const deps = suppliedDeps();
    expect(deps.memoryRequest).toEqual({
      prompt: 'a generic fixture prompt about testing',
      // The SAME id the RunResult.sessionRef carries — the checkpointer
      // thread_id computed at the top of the turn, so the hook's
      // session-concept/dedup state keys to the real session.
      sessionId: result.sessionRef?.session_id,
    });
    // No test-only adapter override leaks into production wiring.
    expect(deps.memoryRetrievalAdapter).toBeUndefined();

    // Middleware ordering is untouched by the new dep (B2's locked order +
    // B3's appended prompt-lifecycle).
    const createAgentArgs = createAgentMock.mock.calls[0]?.[0] as {
      middleware?: Array<{ name: string }>;
    };
    expect(createAgentArgs.middleware?.map((m) => m.name)).toEqual([
      'permissions',
      'wardens',
      'memory',
      'telemetry',
      'prompt-lifecycle',
    ]);
  });

  it('a non-control-group turn omits memoryRequest entirely (personal-vault isolation, AAG-014)', async () => {
    const result = await runTurnAs(false);
    expect(result.status).toBe('success');

    const deps = suppliedDeps();
    expect(deps.memoryRequest).toBeUndefined();
    expect('memoryRequest' in deps).toBe(false);

    // Ordering unchanged here too.
    const createAgentArgs = createAgentMock.mock.calls[0]?.[0] as {
      middleware?: Array<{ name: string }>;
    };
    expect(createAgentArgs.middleware?.map((m) => m.name)).toEqual([
      'permissions',
      'wardens',
      'memory',
      'telemetry',
      'prompt-lifecycle',
    ]);
  });
});

// ── Activity broadcaster decorator integration (LIA-432/G5) ────────────────
//
// The decorator itself (event tee order, synthetic-error rules, downstream
// sink transparency) is unit-tested against a FAKE runtime in
// activity-broadcaster.test.ts. This block locks the one piece that requires
// the REAL DeusNativeRuntime: its catch-path returns `RunResult.status ===
// 'error'` WITHOUT ever calling `eventSink` with a `type: 'error'` event (see
// the "runTurn returns error when the proxy token mint fails" case above) —
// proving `withRuntimeActivityBroadcast` synthesizes exactly one terminal
// broadcaster-only error for that real shape, without altering the original
// downstream sink or RunResult.
describe('DeusNativeBackend — activity broadcaster decorator integration (LIA-432/G5)', () => {
  beforeEach(() => {
    createAgentMock.mockReset();
    invokeMock.mockReset();
    detectAuthModeMock.mockReturnValue('api-key');
    getOrCreateGroupTokenMock.mockReset();
    getOrCreateGroupTokenMock.mockReturnValue('fake-proxy-token');
  });

  it('publishes one terminal broadcaster-only error envelope for a returned error with no emitted error event, without changing the downstream sink or RunResult', async () => {
    getOrCreateGroupTokenMock.mockImplementation(() => {
      throw new Error(
        'getOrCreateGroupToken: folder is a publicIngress folder',
      );
    });

    const backend = createDeusNativeRuntime(stubDeps);
    const broadcaster = new RuntimeActivityBroadcaster();
    const received: RuntimeActivityEnvelope[] = [];
    broadcaster.subscribe((e) => received.push(e));
    const decorated = withRuntimeActivityBroadcast(backend, broadcaster);

    const events: RuntimeEvent[] = [];
    const result = await decorated.runTurn(
      {
        prompt: 'test',
        groupFolder: 'nonexistent',
        chatJid: 'test@g.us',
        isControlGroup: false,
      },
      { backend: 'deus-native', session_id: '' },
      (event) => {
        events.push(event);
      },
    );

    // RunResult is unchanged from the undecorated behavior asserted above.
    expect(result.status).toBe('error');
    expect(result.result).toBeNull();
    expect(result.error).toContain('publicIngress');
    // DeusNativeRuntime's catch path never calls eventSink — the original
    // downstream sink still sees zero events, decorator or not.
    expect(events).toHaveLength(0);
    expect(createAgentMock).not.toHaveBeenCalled();

    // The broadcaster received exactly one synthetic terminal error, never
    // injected into the caller's own sink (the empty `events` array above
    // already proves the latter).
    expect(received).toHaveLength(1);
    expect(received[0].type).toBe('error');
    expect(received[0].payload).toMatchObject({
      error: expect.stringContaining('publicIngress'),
    });
    expect(received[0].source).toEqual({
      backend: 'deus-native',
      groupFolder: 'nonexistent',
      chatJid: 'test@g.us',
    });
  });
});

// ── C1 (LIA-409): wardens layer — real repo-root/cwd wiring at the
// PRODUCTION call site ───────────────────────────────────────────────────
//
// buildWardensMiddleware itself is unit-tested in middleware-stack.test.ts;
// this proves runTurn threads the RIGHT cwd into it — `worktreePath` ahead
// of `cwd` — the same worktreePath-wins seam the permissions-profile block
// above proves for `backendConfig.permissionProfile`.

describe('DeusNativeBackend — wardens cwd wiring (C1/LIA-409)', () => {
  const backend = createDeusNativeRuntime(stubDeps);
  const execFileMock = vi.mocked(execFile);

  beforeEach(() => {
    createAgentMock.mockReset();
    invokeMock.mockReset();
    detectAuthModeMock.mockReturnValue('api-key');
    getOrCreateGroupTokenMock.mockReset();
    getOrCreateGroupTokenMock.mockReturnValue('fake-proxy-token');
    execFileMock.mockReset();
  });

  function capturedWardensMiddleware() {
    const args = createAgentMock.mock.calls[0]?.[0] as {
      middleware?: Array<{ name: string; wrapToolCall?: unknown }>;
    };
    const wardens = args.middleware?.[1];
    expect(wardens?.name).toBe('wardens');
    return wardens as { name: string; wrapToolCall?: unknown };
  }

  it('threads the absolute worktreePath ahead of cwd into wardens stdin events', async () => {
    let capturedStdin = '';
    execFileMock.mockImplementation(((
      _cmd: string,
      _args: readonly string[],
      _options: unknown,
      callback: (err: null, stdout: string) => void,
    ) => {
      const child = {
        stdin: {
          on: () => child.stdin,
          end: (data: string) => {
            capturedStdin = data;
            queueMicrotask(() => callback(null, ''));
          },
        },
      };
      return child as unknown as ReturnType<typeof execFile>;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    }) as any);

    createAgentMock.mockReturnValue({ invoke: invokeMock });
    invokeMock.mockResolvedValue({ messages: [{ content: 'ok' }] });

    // worktreePath is THIS worktree's real root (a genuine git repo, so the
    // production repo-root resolution succeeds for real); cwd is a
    // deliberately different, never-consulted value proving worktreePath
    // wins outright.
    const worktreePath = process.cwd();
    const result = await backend.runTurn(
      {
        prompt: 'test',
        groupFolder: 'test-folder',
        chatJid: 'test@g.us',
        isControlGroup: false,
        cwd: '/definitely/not/the/worktree/path',
        worktreePath,
      },
      { backend: 'deus-native', session_id: '' },
      () => {},
    );

    expect(result.status).toBe('success');

    const wardens = capturedWardensMiddleware();
    const handler = vi.fn(
      () => new ToolMessage({ content: 'ok', tool_call_id: 'call_wiring_1' }),
    );
    await invokeWrapToolCall(
      wardens,
      makeToolCallRequest('apply_patch', { patch: 'x' }),
      handler,
    );

    expect(handler).toHaveBeenCalledTimes(1);
    const event = JSON.parse(capturedStdin) as { cwd: string };
    expect(event.cwd).toBe(worktreePath);
    expect(event.cwd).not.toBe('/definitely/not/the/worktree/path');
  });

  it('threads the SAME resolved wardenCwd into a nested-dispatch child middleware stack (B8/LIA-408 integration)', async () => {
    let capturedStdin = '';
    execFileMock.mockImplementation(((
      _cmd: string,
      _args: readonly string[],
      _options: unknown,
      callback: (err: null, stdout: string) => void,
    ) => {
      const child = {
        stdin: {
          on: () => child.stdin,
          end: (data: string) => {
            capturedStdin = data;
            queueMicrotask(() => callback(null, ''));
          },
        },
      };
      return child as unknown as ReturnType<typeof execFile>;
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    }) as any);

    createAgentMock.mockReturnValue({ invoke: invokeMock });
    invokeMock.mockResolvedValue({ messages: [{ content: 'ok' }] });
    buildNestedDispatchToolMock.mockClear();

    const worktreePath = process.cwd();
    const result = await backend.runTurn(
      {
        prompt: 'test',
        groupFolder: 'test-folder',
        chatJid: 'test@g.us',
        isControlGroup: false,
        cwd: '/definitely/not/the/worktree/path',
        worktreePath,
      },
      { backend: 'deus-native', session_id: '' },
      () => {},
    );
    expect(result.status).toBe('success');

    // The real `CreateNestedDispatcherDeps` runTurn supplies to
    // buildNestedDispatchTool (stubbed at the module boundary — see the
    // hoisted mock — so no live dispatcher is needed here).
    expect(buildNestedDispatchToolMock).toHaveBeenCalledTimes(1);
    const deps = buildNestedDispatchToolMock.mock.calls[0]?.[0] as {
      buildChildMiddleware?: () => Array<{
        name: string;
        wrapToolCall?: unknown;
      }>;
    };
    expect(deps.buildChildMiddleware).toBeDefined();

    // Calling the REAL factory produces a REAL wardens middleware — same
    // pattern as capturedWardensMiddleware() above, just for the child
    // stack instead of the parent's.
    const childMiddleware = deps.buildChildMiddleware!();
    const childWardens = childMiddleware[1];
    expect(childWardens?.name).toBe('wardens');

    const handler = vi.fn(
      () => new ToolMessage({ content: 'ok', tool_call_id: 'call_wiring_1' }),
    );
    await invokeWrapToolCall(
      childWardens,
      makeToolCallRequest('apply_patch', { patch: 'x' }),
      handler,
    );

    expect(handler).toHaveBeenCalledTimes(1);
    const event = JSON.parse(capturedStdin) as { cwd: string };
    expect(event.cwd).toBe(worktreePath);
    expect(event.cwd).not.toBe('/definitely/not/the/worktree/path');
  });
});
