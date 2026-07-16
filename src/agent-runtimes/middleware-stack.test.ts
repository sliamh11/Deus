import { dirname, join } from 'node:path';

import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import {
  createAgent,
  createMiddleware,
  tool,
  FakeToolCallingModel,
  type AgentMiddleware,
} from 'langchain';
import { ToolMessage } from '@langchain/core/messages';

import {
  CANONICAL_MIDDLEWARE_ORDER,
  buildMemoryMiddleware,
  buildMiddlewareStack,
  buildPermissionsMiddleware,
  buildWardensMiddleware,
  parseMiddlewareStackConfig,
  resolveMiddlewareStackConfig,
  type MiddlewareLayerName,
  type OrderMarker,
} from './middleware-stack.js';
import type { MemoryRetrievalRequest } from './memory-retrieval.js';
import type {
  MemoryReembedAdapter,
  MemoryReembedRequest,
} from './memory-reembed.js';

// ── C1 (LIA-409): hermetic child-process seam for the wardens supplementary
// suite below. Only `execFile` (the Python gate invocation) is replaced with
// a controllable fake; `execFileSync` (the Git common-dir repo-root query)
// stays wrapped around the REAL implementation via `vi.fn(actual...)` so:
// - every test ABOVE this point (ordering/toggle/AC4/permissions) keeps
//   resolving a real repo root via a real `git rev-parse` call, exactly the
//   behavior already proven green before this suite existed;
// - the git-fallback supplementary test below can override it for exactly
//   one call via `mockImplementationOnce`, then it reverts to the real
//   implementation for every subsequent call, in this file and beyond.
vi.mock('node:child_process', async () => {
  const actual =
    await vi.importActual<typeof import('node:child_process')>(
      'node:child_process',
    );
  return {
    ...actual,
    execFileSync: vi.fn(actual.execFileSync),
    execFile: vi.fn(),
  };
});

/** Middleware instance names equal their layer names, so ordering
 *  assertions read directly against CANONICAL_MIDDLEWARE_ORDER. */
function layerNames(config = {}): string[] {
  return buildMiddlewareStack(config).middleware.map((m) => m.name);
}

describe('buildMiddlewareStack — ordering (AC1) and disablement (AC3)', () => {
  it('all layers enabled by default ({} config) in exactly the canonical order', () => {
    expect(layerNames({})).toEqual([
      'permissions',
      'wardens',
      'memory',
      'telemetry',
    ]);
    // The canonical array is the single source of truth being asserted.
    expect(layerNames({})).toEqual([...CANONICAL_MIDDLEWARE_ORDER]);
  });

  it.each([
    ['permissions', ['wardens', 'memory', 'telemetry']],
    ['wardens', ['permissions', 'memory', 'telemetry']],
    ['memory', ['permissions', 'wardens', 'telemetry']],
    ['telemetry', ['permissions', 'wardens', 'memory']],
  ] as const)(
    'disabling %s preserves the relative order of the remaining layers',
    (disabled, expected) => {
      expect(layerNames({ [disabled]: false })).toEqual(expected);
    },
  );

  it('disabling multiple layers preserves the relative order of the rest', () => {
    expect(layerNames({ permissions: false, memory: false })).toEqual([
      'wardens',
      'telemetry',
    ]);
  });

  it('disabling all layers returns an empty middleware array', () => {
    expect(
      layerNames({
        permissions: false,
        wardens: false,
        memory: false,
        telemetry: false,
      }),
    ).toEqual([]);
  });

  it('a disabled layer contributes no log; enabled layers keep theirs', () => {
    const { logs } = buildMiddlewareStack({ wardens: false });
    expect(logs.wardens).toBeUndefined();
    expect(logs.permissions).toEqual([]);
    expect(logs.memory).toEqual([]);
    expect(logs.telemetry).toEqual([]);
  });
});

describe('buildMiddlewareStack — independent per-layer toggles (AC2)', () => {
  it.each(CANONICAL_MIDDLEWARE_ORDER.map((l) => [l] as const))(
    'the %s toggle controls only its own layer',
    (layer: MiddlewareLayerName) => {
      const names = layerNames({ [layer]: false });
      // The toggled layer is gone...
      expect(names).not.toContain(layer);
      // ...and EVERY other layer is untouched, in canonical order.
      expect(names).toEqual(
        CANONICAL_MIDDLEWARE_ORDER.filter((l) => l !== layer),
      );
    },
  );

  it('an explicit true is the same as an absent key (enabled)', () => {
    expect(
      layerNames({
        permissions: true,
        wardens: true,
        memory: true,
        telemetry: true,
      }),
    ).toEqual([...CANONICAL_MIDDLEWARE_ORDER]);
  });
});

// ── AC4: entry/exit ordering proof over a REAL createAgent turn ──────────
//
// beforeAgent/afterAgent is the one hook type that wraps the entire turn
// exactly once per layer (ReactAgent.js chains beforeAgent nodes in forward
// array order and afterAgent nodes in reverse), so the shared orderMarkers
// array records the genuine cross-layer onion. The scripted model emits a
// real tool call on its first cycle so every layer's SUBSTANTIVE hook
// (wrapToolCall / beforeModel+afterModel / wrapModelCall) also fires at
// least once — confirming the markers and the substantive logs are
// consistent with each other. FakeToolCallingModel is langchain's own
// createAgent test model (real BaseChatModel with working bindTools) —
// hermetic, no network (same precedent as lia399's test).

const ECHO_TOOL = tool(
  async (args: { value: string }) => `echo:${args.value}`,
  {
    name: 'echo_tool',
    description: 'Echoes the provided value back.',
    schema: {
      type: 'object',
      properties: { value: { type: 'string' } },
      required: ['value'],
      additionalProperties: false,
    },
  },
);

function scriptedModel(): FakeToolCallingModel {
  // First model cycle: request one echo_tool call. Second cycle: plain
  // final answer (empty toolCalls entry), ending the ReAct loop.
  return new FakeToolCallingModel({
    toolCalls: [
      [{ name: 'echo_tool', args: { value: 'ping' }, id: 'call_1' }],
      [],
    ],
  });
}

describe('middleware entry/exit order across a real agent turn (AC4)', () => {
  it('records the onion order: enter 0->3, exit 3->0, with every substantive hook also firing', async () => {
    const orderMarkers: OrderMarker[] = [];
    const { middleware, logs } = buildMiddlewareStack({}, { orderMarkers });

    const agent = createAgent({
      model: scriptedModel(),
      tools: [ECHO_TOOL],
      middleware,
    });
    await agent.invoke({
      messages: [{ role: 'user', content: 'call the echo tool once' }],
    });

    // The empirical proof of the array-order-to-wrap-order mapping:
    // index 0 (permissions) enters FIRST and exits LAST.
    expect(orderMarkers).toEqual([
      { layer: 'permissions', phase: 'enter' },
      { layer: 'wardens', phase: 'enter' },
      { layer: 'memory', phase: 'enter' },
      { layer: 'telemetry', phase: 'enter' },
      { layer: 'telemetry', phase: 'exit' },
      { layer: 'memory', phase: 'exit' },
      { layer: 'wardens', phase: 'exit' },
      { layer: 'permissions', phase: 'exit' },
    ]);

    // Substantive hooks fired consistently with the scripted turn:
    // one tool call seen by BOTH wrapToolCall layers...
    expect(logs.permissions?.map((r) => r.toolName)).toEqual(['echo_tool']);
    expect(logs.wardens?.map((r) => r.toolName)).toEqual(['echo_tool']);
    // ...two model cycles (tool-call turn + final turn) seen by memory's
    // beforeModel/afterModel and telemetry's wrapModelCall.
    expect(logs.memory?.filter((r) => r.hook === 'beforeModel')).toHaveLength(
      2,
    );
    expect(logs.memory?.filter((r) => r.hook === 'afterModel')).toHaveLength(2);
    expect(logs.telemetry?.map((r) => r.requestIndex)).toEqual([0, 1]);
    expect(logs.telemetry?.[0]?.providerClass).toBe('FakeToolCallingModel');
  });

  it('a disabled layer drops out of the recorded order without disturbing the rest (AC3, live)', async () => {
    const orderMarkers: OrderMarker[] = [];
    const { middleware, logs } = buildMiddlewareStack(
      { wardens: false },
      { orderMarkers },
    );

    const agent = createAgent({
      model: scriptedModel(),
      tools: [ECHO_TOOL],
      middleware,
    });
    await agent.invoke({
      messages: [{ role: 'user', content: 'call the echo tool once' }],
    });

    expect(orderMarkers).toEqual([
      { layer: 'permissions', phase: 'enter' },
      { layer: 'memory', phase: 'enter' },
      { layer: 'telemetry', phase: 'enter' },
      { layer: 'telemetry', phase: 'exit' },
      { layer: 'memory', phase: 'exit' },
      { layer: 'permissions', phase: 'exit' },
    ]);
    expect(logs.wardens).toBeUndefined();
    // The remaining wrapToolCall layer still saw the tool call.
    expect(logs.permissions?.map((r) => r.toolName)).toEqual(['echo_tool']);
  });
});

describe('parseMiddlewareStackConfig — invalid config rejected or normalized deterministically (AC5)', () => {
  it('drops unknown keys and keeps known ones (normalized, not rejected)', () => {
    expect(
      parseMiddlewareStackConfig({ wardens: false, futureLayer: true }),
    ).toEqual({ wardens: false });
  });

  it('returns {} (all enabled) for an empty object', () => {
    expect(parseMiddlewareStackConfig({})).toEqual({});
  });

  it('throws a descriptive error on a non-boolean value for a known key', () => {
    expect(() => parseMiddlewareStackConfig({ wardens: 'false' })).toThrow(
      'layer "wardens" must be a boolean, got string',
    );
  });

  it.each([
    ['null', null],
    ['array', []],
    ['string', 'wardens'],
    ['number', 42],
  ] as const)('throws on a non-object config (%s)', (_label, raw) => {
    expect(() => parseMiddlewareStackConfig(raw)).toThrow(
      'expected a plain object',
    );
  });
});

describe('resolveMiddlewareStackConfig — DEUS_NATIVE_MIDDLEWARE_CONFIG env var', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('defaults to {} (all enabled) when unset', () => {
    vi.stubEnv('DEUS_NATIVE_MIDDLEWARE_CONFIG', '');
    expect(resolveMiddlewareStackConfig()).toEqual({});
  });

  it('defaults to {} on a malformed JSON string (env typo must not crash the runtime)', () => {
    vi.stubEnv('DEUS_NATIVE_MIDDLEWARE_CONFIG', '{wardens: false'); // not JSON
    expect(resolveMiddlewareStackConfig()).toEqual({});
  });

  it('parses a valid JSON object through the strict validator', () => {
    vi.stubEnv(
      'DEUS_NATIVE_MIDDLEWARE_CONFIG',
      '{"wardens":false,"unknown":true}',
    );
    expect(resolveMiddlewareStackConfig()).toEqual({ wardens: false });
  });

  it('stays strict on valid JSON that is a malformed config OBJECT', () => {
    // Deliberate boundary (see resolveMiddlewareStackConfig's doc comment):
    // lenient only for the JSON-STRING failure mode, not for a genuinely
    // malformed parsed object.
    vi.stubEnv('DEUS_NATIVE_MIDDLEWARE_CONFIG', '{"wardens":"nope"}');
    expect(() => resolveMiddlewareStackConfig()).toThrow(
      'layer "wardens" must be a boolean',
    );
  });
});

// ── B7 (LIA-407): real permissions layer — SUPPLEMENTARY coverage ─────────
//
// The independent oracle (middleware-stack.oracle.test.ts, authored blind to
// this implementation) pins the agent-level DENY contract: non-delegation,
// the model-visible denial ToolMessage, argument non-exposure, and the deny
// decision log. This block complements it — it does NOT re-assert those —
// with what the oracle deliberately left implementation-authored (plan AC2/
// AC5): the direct-invocation ALLOW path's request-identity guarantee, the
// direct-level deny shape, default-profile behavior, invalid-profile
// fail-visibly semantics, and a real-createAgent proof that compliant
// (allowed) flows are not impeded.

/** Minimal ToolCallRequest stand-in for DIRECT wrapToolCall invocation —
 *  the hook only reads request.toolCall.{name,id} and passes the request
 *  through, so state/runtime/tool can be inert stubs. */
function makeToolCallRequest(
  name: string,
  args: Record<string, unknown>,
  id = 'call_direct_1',
) {
  return {
    toolCall: { name, args, id, type: 'tool_call' as const },
    tool: undefined,
    state: {},
    runtime: {},
  };
}

/** Casts through the middleware's generic hook signature so tests can call
 *  wrapToolCall directly with the minimal request above. */
async function invokeWrapToolCall(
  middleware: { wrapToolCall?: unknown },
  request: unknown,
  handler: (req: unknown) => Promise<ToolMessage> | ToolMessage,
): Promise<unknown> {
  const hook = middleware.wrapToolCall as (
    request: unknown,
    handler: unknown,
  ) => Promise<unknown> | unknown;
  return await hook(request, handler);
}

describe('buildPermissionsMiddleware — allow path delegates the ORIGINAL request (AC2, AC5)', () => {
  it('read-only profile + an allowed read tool: handler called exactly once with the SAME request and args references (frozen/nested)', async () => {
    const { middleware, log } = buildPermissionsMiddleware('read-only');

    // Deep-frozen, nested args: any reconstruction (handler({...request}) or
    // rebuilt toolCall) would break the toBe identity assertions below.
    const args = Object.freeze({
      query: Object.freeze({ text: 'deus', lang: 'en' }),
    }) as unknown as Record<string, unknown>;
    const request = makeToolCallRequest('web_search', args, 'call_allow_1');

    const handlerResult = new ToolMessage({
      content: 'searched ok',
      tool_call_id: 'call_allow_1',
    });
    let received: unknown;
    const handler = vi.fn((req: unknown) => {
      received = req;
      return handlerResult;
    });

    const result = await invokeWrapToolCall(middleware, request, handler);

    expect(handler).toHaveBeenCalledTimes(1);
    // Reference identity — the AC5 no-rewrite guarantee.
    expect(received).toBe(request);
    expect((received as { toolCall: { args: unknown } }).toolCall.args).toBe(
      args,
    );
    // The handler's own result flows back unmodified.
    expect(result).toBe(handlerResult);

    expect(log).toEqual([
      {
        toolName: 'web_search',
        decision: 'allow',
        source: 'rule',
        reason: expect.stringContaining('web_search'),
      },
    ]);
  });

  it('default profile (omitted argument): an UNMATCHED tool is allowed via the policy default', async () => {
    const { middleware, log } = buildPermissionsMiddleware();
    const request = makeToolCallRequest('echo_tool', { value: 'ping' });
    const handler = vi.fn(
      () => new ToolMessage({ content: 'ok', tool_call_id: 'call_direct_1' }),
    );

    await invokeWrapToolCall(middleware, request, handler);

    expect(handler).toHaveBeenCalledTimes(1);
    expect(log).toEqual([
      {
        toolName: 'echo_tool',
        decision: 'allow',
        source: 'default',
        reason: expect.stringContaining('default is allow'),
      },
    ]);
  });
});

describe('buildPermissionsMiddleware — deny path at the direct hook level (AC2)', () => {
  it('read-only profile + a mutation tool: handler never invoked; synthetic error ToolMessage carries name/id/profile/reason but NOT the arguments', async () => {
    const SENTINEL = 'DIRECT_SENTINEL_should_not_leak';
    const { middleware, log } = buildPermissionsMiddleware('read-only');
    const request = makeToolCallRequest(
      'bash_exec',
      { command: SENTINEL },
      'call_deny_1',
    );
    const handler = vi.fn(
      () => new ToolMessage({ content: 'ran', tool_call_id: 'call_deny_1' }),
    );

    const result = await invokeWrapToolCall(middleware, request, handler);

    expect(handler).not.toHaveBeenCalled();
    expect(result).toBeInstanceOf(ToolMessage);
    const denial = result as ToolMessage;
    expect(denial.status).toBe('error');
    expect(denial.tool_call_id).toBe('call_deny_1');
    expect(denial.name).toBe('bash_exec');
    const content =
      typeof denial.content === 'string'
        ? denial.content
        : JSON.stringify(denial.content);
    expect(content).toContain('permission_denied');
    expect(content).toContain('read-only');
    expect(content).toContain('bash_exec');
    expect(content).not.toContain(SENTINEL);

    expect(log).toEqual([
      {
        toolName: 'bash_exec',
        decision: 'deny',
        source: 'rule',
        reason: expect.stringContaining('bash_exec'),
      },
    ]);
  });
});

describe('permission profile selection — invalid names fail visibly (plan Scope)', () => {
  it('buildPermissionsMiddleware throws on an unknown profile name', () => {
    expect(() => buildPermissionsMiddleware('no-such-profile')).toThrow(
      'unknown permission profile "no-such-profile"',
    );
  });

  it('buildMiddlewareStack throws on an unknown profile name BEFORE returning any middleware', () => {
    expect(() =>
      buildMiddlewareStack({}, { permissionProfile: 'no-such-profile' }),
    ).toThrow('unknown permission profile "no-such-profile"');
  });

  it('the up-front validation fires even when the permissions layer is toggled OFF (never silently ignore a requested restriction)', () => {
    expect(() =>
      buildMiddlewareStack(
        { permissions: false },
        { permissionProfile: 'no-such-profile' },
      ),
    ).toThrow('unknown permission profile "no-such-profile"');
  });
});

describe('permissions middleware — compliant (allowed) flows are not impeded through a real agent (AC2/AC3 allow side)', () => {
  it('read-only profile + a scripted call to an allowed read tool: the tool executes and its real result reaches the graph', async () => {
    const readSpy = vi.fn(async (args: { query: string }) => {
      return `results-for:${args.query}`;
    });
    const readTool = tool(readSpy, {
      name: 'web_search',
      description: 'test double for an allowed read tool',
      schema: {
        type: 'object',
        properties: { query: { type: 'string' } },
        required: ['query'],
        additionalProperties: false,
      },
    });
    const model = new FakeToolCallingModel({
      toolCalls: [
        [{ name: 'web_search', args: { query: 'deus' }, id: 'call_ro_1' }],
        [],
      ],
    });
    const { middleware, logs } = buildMiddlewareStack(
      { wardens: false, memory: false, telemetry: false },
      { permissionProfile: 'read-only' },
    );

    const agent = createAgent({ model, tools: [readTool], middleware });
    const result = await agent.invoke({
      messages: [{ role: 'user', content: 'search for deus' }],
    });

    // The allowed tool genuinely ran...
    expect(readSpy).toHaveBeenCalledTimes(1);
    // ...its REAL output (not a denial) is the tool message in the graph...
    const toolMessage = (result as { messages: unknown[] }).messages.find(
      (m): m is ToolMessage =>
        ToolMessage.isInstance(m as never) &&
        (m as ToolMessage).tool_call_id === 'call_ro_1',
    );
    expect(toolMessage).toBeDefined();
    expect(toolMessage?.status).not.toBe('error');
    expect(String(toolMessage?.content)).toContain('results-for:deus');
    // ...and the decision log recorded a rule-sourced allow.
    expect(logs.permissions).toEqual([
      {
        toolName: 'web_search',
        decision: 'allow',
        source: 'rule',
        reason: expect.stringContaining('web_search'),
      },
    ]);
  });
});

// ── D3 (LIA-417): dormant post-success memory re-embedding mechanism ────
//
// These tests deliberately invoke the memory middleware's wrapToolCall hook
// directly. No supported edit tool is currently supplied to the production
// deus-native agent; deus-native-tool-scope.oracle.test.ts remains the
// separate standing proof of that live reachability boundary.

function successfulToolMessage(id = 'call_edit_1'): ToolMessage {
  return new ToolMessage({ content: 'edited', tool_call_id: id });
}

describe('memory middleware — post-success edit re-embedding (D3/LIA-417)', () => {
  it('delegates a recognized edit once with the original request, then re-embeds with the canonical name and exact path', async () => {
    const adapterCalls: MemoryReembedRequest[] = [];
    const reembedAdapter: MemoryReembedAdapter = async (request) => {
      adapterCalls.push(request);
    };
    const { middleware: stack } = buildMiddlewareStack(
      {},
      { memoryReembedAdapter: reembedAdapter },
    );
    const middleware = stack.find((layer) => layer.name === 'memory');
    if (middleware === undefined) throw new Error('memory layer not built');
    const args = Object.freeze({ path: '/workspace/project/example.ts' });
    const request = makeToolCallRequest('edit_file', args, 'call_edit_1');
    const handlerResult = successfulToolMessage();
    const handler = vi.fn(() => handlerResult);

    const result = await invokeWrapToolCall(middleware, request, handler);

    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith(request);
    expect(adapterCalls).toEqual([
      { toolName: 'Edit', filePath: '/workspace/project/example.ts' },
    ]);
    expect(result).toBe(handlerResult);
  });

  it('does not re-embed when a recognized edit returns an error-status ToolMessage', async () => {
    const reembedAdapter = vi.fn<MemoryReembedAdapter>();
    const { middleware } = buildMemoryMiddleware(
      undefined,
      undefined,
      reembedAdapter,
    );
    const request = makeToolCallRequest('write_file', {
      path: '/workspace/project/example.ts',
    });
    const handlerResult = new ToolMessage({
      content: 'write failed',
      tool_call_id: 'call_direct_1',
      status: 'error',
    });

    const result = await invokeWrapToolCall(
      middleware,
      request,
      () => handlerResult,
    );

    expect(reembedAdapter).not.toHaveBeenCalled();
    expect(result).toBe(handlerResult);
  });

  it('preserves a rejected handler failure and does not invoke the adapter', async () => {
    const reembedAdapter = vi.fn<MemoryReembedAdapter>();
    const { middleware } = buildMemoryMiddleware(
      undefined,
      undefined,
      reembedAdapter,
    );
    const request = makeToolCallRequest('edit_file', {
      path: '/workspace/project/example.ts',
    });
    const handlerFailure = new Error('edit failed');

    await expect(
      invokeWrapToolCall(middleware, request, () =>
        Promise.reject(handlerFailure),
      ),
    ).rejects.toBe(handlerFailure);
    expect(reembedAdapter).not.toHaveBeenCalled();
  });

  it.each([
    'read_file',
    'grep_files',
    'glob_files',
    'bash_exec',
    'web_search',
    'web_fetch',
    'unknown_tool',
  ])(
    'delegates successful non-edit tool %s without re-embedding',
    async (name) => {
      const reembedAdapter = vi.fn<MemoryReembedAdapter>();
      const { middleware } = buildMemoryMiddleware(
        undefined,
        undefined,
        reembedAdapter,
      );
      const request = makeToolCallRequest(name, {
        path: '/workspace/project/example.ts',
        file_path: '/workspace/project/example.ts',
      });
      const handlerResult = successfulToolMessage();
      const handler = vi.fn(() => handlerResult);

      const result = await invokeWrapToolCall(middleware, request, handler);

      expect(handler).toHaveBeenCalledTimes(1);
      expect(handler).toHaveBeenCalledWith(request);
      expect(reembedAdapter).not.toHaveBeenCalled();
      expect(result).toBe(handlerResult);
    },
  );

  it.each([
    ['missing', {}],
    ['non-string', { path: 42 }],
    ['empty', { path: '' }],
    ['whitespace-only', { path: '   \t' }],
  ])('ignores a supported broker edit with a %s path', async (_label, args) => {
    const reembedAdapter = vi.fn<MemoryReembedAdapter>();
    const { middleware } = buildMemoryMiddleware(
      undefined,
      undefined,
      reembedAdapter,
    );

    await invokeWrapToolCall(
      middleware,
      makeToolCallRequest('edit_file', args),
      () => successfulToolMessage(),
    );

    expect(reembedAdapter).not.toHaveBeenCalled();
  });

  it.each([
    ['Write', 'Write'],
    ['Edit', 'Edit'],
    ['MultiEdit', 'MultiEdit'],
  ] as const)(
    'reads file_path from hook-shaped %s and preserves its canonical name',
    async (toolName, expectedToolName) => {
      const reembedAdapter = vi.fn<MemoryReembedAdapter>();
      const { middleware } = buildMemoryMiddleware(
        undefined,
        undefined,
        reembedAdapter,
      );

      await invokeWrapToolCall(
        middleware,
        makeToolCallRequest(toolName, {
          file_path: ' /workspace/vault/exact path.md ',
          path: '/wrong/path',
        }),
        () => successfulToolMessage(),
      );

      expect(reembedAdapter).toHaveBeenCalledWith({
        toolName: expectedToolName,
        filePath: ' /workspace/vault/exact path.md ',
      });
    },
  );

  it.each([
    ['write_file', 'Write'],
    ['edit_file', 'Edit'],
  ] as const)(
    'reads path from broker-shaped %s and maps it to %s',
    async (toolName, expectedToolName) => {
      const reembedAdapter = vi.fn<MemoryReembedAdapter>();
      const { middleware } = buildMemoryMiddleware(
        undefined,
        undefined,
        reembedAdapter,
      );

      await invokeWrapToolCall(
        middleware,
        makeToolCallRequest(toolName, {
          path: '/workspace/project/exact.ts',
          file_path: '/wrong/path',
        }),
        () => successfulToolMessage(),
      );

      expect(reembedAdapter).toHaveBeenCalledWith({
        toolName: expectedToolName,
        filePath: '/workspace/project/exact.ts',
      });
    },
  );

  it('contains an injected adapter rejection and returns the successful result unchanged', async () => {
    const reembedAdapter = vi.fn<MemoryReembedAdapter>(async () => {
      throw new Error('injected re-embed failure');
    });
    const { middleware } = buildMemoryMiddleware(
      undefined,
      undefined,
      reembedAdapter,
    );
    const handlerResult = successfulToolMessage();

    const result = await invokeWrapToolCall(
      middleware,
      makeToolCallRequest('edit_file', {
        path: '/workspace/project/example.ts',
      }),
      () => handlerResult,
    );

    expect(reembedAdapter).toHaveBeenCalledTimes(1);
    expect(result).toBe(handlerResult);
  });
});

// ── D1 (LIA-415): memory layer retrieval injection over a REAL createAgent
// turn ────────────────────────────────────────────────────────────────────
//
// The adapter is an injected hermetic double (no Python, vault, or Ollama);
// what these tests prove is the MIDDLEWARE contract: one retrieval per turn
// for the submitted prompt, non-empty context visible in the model input
// (AC3) after the original prompt, and the empty-result path leaving the
// model input unchanged without failing the turn (AC4). The subprocess
// boundary itself is pinned down in memory-retrieval.test.ts.

const MEMORY_FIXTURE_REQUEST: MemoryRetrievalRequest = {
  prompt: 'call the echo tool once',
  sessionId: 'session-fixture-d1',
};

const RECALLED_CONTEXT =
  '=== UNTRUSTED RECALLED MEMORY (fixture) ===\nrecalled fixture context';

/** Innermost capture middleware: records each model request's message
 *  contents exactly as the (fake) model is about to receive them — the
 *  "visible in the model input" oracle. Appended AFTER the stack, mirroring
 *  how runTurn appends its prompt-lifecycle middleware. */
function buildModelInputCapture(captured: string[][]): AgentMiddleware {
  return createMiddleware({
    name: 'model-input-capture',
    wrapModelCall: (request, handler) => {
      captured.push(
        request.messages.map((m) =>
          typeof m.content === 'string' ? m.content : JSON.stringify(m.content),
        ),
      );
      return handler(request);
    },
  });
}

describe('memory middleware — beforeModel retrieval injection (D1/LIA-415)', () => {
  it('injects non-empty retrieved context into the model input after the prompt, retrieving exactly once across a two-cycle turn (AC1/AC3/AC5)', async () => {
    const adapterCalls: MemoryRetrievalRequest[] = [];
    const { middleware } = buildMiddlewareStack(
      {},
      {
        memoryRequest: MEMORY_FIXTURE_REQUEST,
        memoryRetrievalAdapter: async (request) => {
          adapterCalls.push(request);
          return RECALLED_CONTEXT;
        },
      },
    );

    const captured: string[][] = [];
    // Explicit AgentMiddleware[] annotation — same reason as runTurn's own
    // allMiddleware: the mixed array literal otherwise infers a shape that
    // misses createAgent's overloads.
    const allMiddleware: AgentMiddleware[] = [
      ...middleware,
      buildModelInputCapture(captured),
    ];
    const agent = createAgent({
      model: scriptedModel(),
      tools: [ECHO_TOOL],
      middleware: allMiddleware,
    });
    const result = await agent.invoke({
      messages: [{ role: 'user', content: MEMORY_FIXTURE_REQUEST.prompt }],
    });

    // AC1: retrieval ran BEFORE the first model request — the injected
    // context is already in that request's input. AC3: it appears after the
    // original prompt (appended via the messages reducer, never position 0).
    expect(captured[0]).toEqual([
      MEMORY_FIXTURE_REQUEST.prompt,
      RECALLED_CONTEXT,
    ]);

    // One retrieval for the submitted prompt, despite TWO model cycles
    // (tool-call cycle + final cycle) — the closure gate held.
    expect(captured).toHaveLength(2);
    expect(adapterCalls).toEqual([MEMORY_FIXTURE_REQUEST]);

    // The injected message is retained state: the second cycle still sees it.
    expect(captured[1]).toContain(RECALLED_CONTEXT);

    // The turn completed normally with a final model answer.
    expect((result as { messages: unknown[] }).messages.length).toBeGreaterThan(
      0,
    );
  });

  it('an empty retrieval result leaves the model input unchanged and does not fail the turn (AC4/AC5)', async () => {
    const adapter = vi.fn(async () => '');
    const { middleware } = buildMiddlewareStack(
      {},
      {
        memoryRequest: MEMORY_FIXTURE_REQUEST,
        memoryRetrievalAdapter: adapter,
      },
    );

    const captured: string[][] = [];
    // Explicit AgentMiddleware[] annotation — same reason as runTurn's own
    // allMiddleware: the mixed array literal otherwise infers a shape that
    // misses createAgent's overloads.
    const allMiddleware: AgentMiddleware[] = [
      ...middleware,
      buildModelInputCapture(captured),
    ];
    const agent = createAgent({
      model: scriptedModel(),
      tools: [ECHO_TOOL],
      middleware: allMiddleware,
    });
    const result = await agent.invoke({
      messages: [{ role: 'user', content: MEMORY_FIXTURE_REQUEST.prompt }],
    });

    // Retrieval WAS attempted (once)...
    expect(adapter).toHaveBeenCalledTimes(1);
    // ...but the model input is byte-identical to a no-injection turn.
    expect(captured[0]).toEqual([MEMORY_FIXTURE_REQUEST.prompt]);
    // The turn still ran to completion — two cycles, final answer present.
    expect(captured).toHaveLength(2);
    expect((result as { messages: unknown[] }).messages.length).toBeGreaterThan(
      0,
    );
  });

  it('a rejecting adapter is contained: model input unchanged, turn completes (AC4 hardening)', async () => {
    const adapter = vi.fn(async () => {
      throw new Error('injected adapter failure');
    });
    const { middleware } = buildMiddlewareStack(
      {},
      {
        memoryRequest: MEMORY_FIXTURE_REQUEST,
        memoryRetrievalAdapter: adapter,
      },
    );

    const captured: string[][] = [];
    // Explicit AgentMiddleware[] annotation — same reason as runTurn's own
    // allMiddleware: the mixed array literal otherwise infers a shape that
    // misses createAgent's overloads.
    const allMiddleware: AgentMiddleware[] = [
      ...middleware,
      buildModelInputCapture(captured),
    ];
    const agent = createAgent({
      model: scriptedModel(),
      tools: [ECHO_TOOL],
      middleware: allMiddleware,
    });
    await agent.invoke({
      messages: [{ role: 'user', content: MEMORY_FIXTURE_REQUEST.prompt }],
    });

    expect(adapter).toHaveBeenCalledTimes(1);
    expect(captured[0]).toEqual([MEMORY_FIXTURE_REQUEST.prompt]);
    expect(captured).toHaveLength(2);
  });

  it('without memoryRequest the layer stays a pass-through observer — the adapter is never invoked', async () => {
    const adapter = vi.fn(async () => RECALLED_CONTEXT);
    const { middleware, logs } = buildMiddlewareStack(
      {},
      { memoryRetrievalAdapter: adapter },
    );

    const captured: string[][] = [];
    // Explicit AgentMiddleware[] annotation — same reason as runTurn's own
    // allMiddleware: the mixed array literal otherwise infers a shape that
    // misses createAgent's overloads.
    const allMiddleware: AgentMiddleware[] = [
      ...middleware,
      buildModelInputCapture(captured),
    ];
    const agent = createAgent({
      model: scriptedModel(),
      tools: [ECHO_TOOL],
      middleware: allMiddleware,
    });
    await agent.invoke({
      messages: [{ role: 'user', content: MEMORY_FIXTURE_REQUEST.prompt }],
    });

    expect(adapter).not.toHaveBeenCalled();
    expect(captured[0]).toEqual([MEMORY_FIXTURE_REQUEST.prompt]);
    // The observational log still records every firing — record contract
    // unchanged from the placeholder era.
    expect(logs.memory?.filter((r) => r.hook === 'beforeModel')).toHaveLength(
      2,
    );
  });
});

// ── C1 (LIA-409): real wardens layer — SUPPLEMENTARY coverage ─────────────
//
// The frozen independent oracle
// (middleware-stack.warden-gates.oracle.test.ts, authored blind to this
// implementation, over a REAL temporary python3 on PATH) pins the
// agent-level contract: apply_patch/commit-Bash trigger mapping, exact
// unchanged stdin, the model-visible deny ToolMessage, non-delegation on
// block, and Claude's hook path staying additive. This block complements it
// with what the oracle deliberately leaves to implementation-authored
// coverage: non-matching-tool passthrough, per-behavior sequencing/
// short-circuit, the exit-code-is-not-a-signal contract, every fail-closed
// failure mode (subprocess error, timeout, stdin/EPIPE, malformed JSON,
// protocol drift), and the Git-common-dir repo-root resolution (including
// its non-git fallback) — via a hermetic `execFile` fake plus the REAL `git`
// binary for repo-root resolution (see the `node:child_process` mock above).

import { execFile, execFileSync } from 'node:child_process';

const execFileMock = vi.mocked(execFile);
const execFileSyncMock = vi.mocked(execFileSync);

// Computed via the REAL (unmocked) execFileSync, exactly like the frozen
// oracle's own top-of-file setup — this worktree genuinely is a linked
// worktree, so WORKTREE_ROOT and REPO_ROOT are genuinely different absolute
// paths, no mocking required to exercise that distinction (case 18 below).
const WORKTREE_ROOT = process.cwd();
const COMMON_GIT_DIR = execFileSync(
  'git',
  ['rev-parse', '--path-format=absolute', '--git-common-dir'],
  { cwd: WORKTREE_ROOT, encoding: 'utf8' },
).trim();
const REPO_ROOT = dirname(COMMON_GIT_DIR);
const WARDEN_SCRIPT = join(REPO_ROOT, 'scripts', 'codex_warden_hooks.py');

type FakeBehaviorResponse =
  | { kind: 'stdout'; stdout: string }
  | { kind: 'error'; error: NodeJS.ErrnoException }
  | { kind: 'stdin-error'; error: Error };

interface CapturedWardenCall {
  argv: string[];
  stdin: string;
}

let behaviorResponses: Map<string, FakeBehaviorResponse>;
let capturedWardenCalls: CapturedWardenCall[];

/** Default: an unconfigured behavior allows (empty stdout) — most tests only
 *  care about ONE behavior's response and want every other selected
 *  behavior in a commit sequence to pass through as a plain allow. */
function setWardenResponse(behavior: string, response: FakeBehaviorResponse) {
  behaviorResponses.set(behavior, response);
}

beforeEach(() => {
  behaviorResponses = new Map();
  capturedWardenCalls = [];
  execFileMock.mockReset();
  execFileMock.mockImplementation(((
    cmd: string,
    args: readonly string[],
    _options: unknown,
    callback: (err: NodeJS.ErrnoException | null, stdout: string) => void,
  ) => {
    const argv = [cmd, ...args];
    const behavior = args[2] as string;
    const response = behaviorResponses.get(behavior) ?? {
      kind: 'stdout',
      stdout: '',
    };
    let errorHandler: ((err: Error) => void) | undefined;
    const child = {
      stdin: {
        on: (event: string, handler: (err: Error) => void) => {
          if (event === 'error') errorHandler = handler;
          return child.stdin;
        },
        end: (data: string) => {
          capturedWardenCalls.push({ argv, stdin: data });
          queueMicrotask(() => {
            if (response.kind === 'stdin-error') {
              errorHandler?.(response.error);
              return;
            }
            if (response.kind === 'error') {
              callback(response.error, '');
              return;
            }
            callback(null, response.stdout);
          });
        },
      },
    };
    return child as unknown as ReturnType<typeof execFile>;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  }) as any);
});

afterEach(() => {
  execFileMock.mockReset();
  execFileSyncMock.mockClear();
});

/** Builds a real wardens middleware pinned at this worktree's cwd (so the
 *  REAL git-derived REPO_ROOT/WARDEN_SCRIPT above are the ones exercised)
 *  and drives its wrapToolCall hook directly, matching this file's existing
 *  direct-invocation helpers for the permissions layer. */
function buildWardens() {
  return buildWardensMiddleware(WORKTREE_ROOT);
}

async function callWardens(
  toolName: string,
  args: Record<string, unknown>,
  id = 'call_warden_1',
) {
  const { middleware, log } = buildWardens();
  const handler = vi.fn(
    async () => new ToolMessage({ content: 'executed', tool_call_id: id }),
  );
  const request = makeToolCallRequest(toolName, args, id);
  const result = await invokeWrapToolCall(middleware, request, handler);
  return { handler, log, result };
}

function expectArgvContract(argv: string[], behavior: string): void {
  expect(argv.slice(1, 4)).toEqual([WARDEN_SCRIPT, 'run', behavior]);
  const flagIndex = argv.indexOf('--repo-root');
  expect(flagIndex).toBeGreaterThanOrEqual(0);
  expect(argv[flagIndex + 1]).toBe(REPO_ROOT);
}

const PLAN_REVIEW_BEHAVIOR_NAME = 'plan-review-gate';

describe('wardens middleware — non-matching tools delegate exactly once, no Python invocation', () => {
  it('an unmatched tool (web_search) delegates the original request exactly once and does not spawn Python', async () => {
    const { handler, log, result } = await callWardens('web_search', {
      query: 'x',
    });
    expect(execFileMock).not.toHaveBeenCalled();
    expect(handler).toHaveBeenCalledTimes(1);
    expect(result).toBeInstanceOf(ToolMessage);
    expect(log).toEqual([{ toolName: 'web_search', decision: 'allow' }]);
  });

  it('a non-commit Bash call delegates exactly once and does not spawn Python', async () => {
    const { handler, log } = await callWardens('Bash', {
      command: 'npm test',
    });
    expect(execFileMock).not.toHaveBeenCalled();
    expect(handler).toHaveBeenCalledTimes(1);
    expect(log).toEqual([{ toolName: 'Bash', decision: 'allow' }]);
  });

  it.each(['write_file', 'edit_file', 'bash_exec'])(
    '%s remains outside the exact-name trigger contract and delegates without a gate invocation',
    async (toolName) => {
      const { handler } = await callWardens(toolName, {
        path: '/x',
        command: 'git commit -m x',
      });
      expect(execFileMock).not.toHaveBeenCalled();
      expect(handler).toHaveBeenCalledTimes(1);
    },
  );
});

describe('wardens middleware — identity trigger mapping (no translation)', () => {
  it('apply_patch identity-maps to plan-review-gate with unchanged tool_name/tool_input', async () => {
    const patchArgs = { patch: '*** Begin Patch\n*** End Patch' };
    const { handler, log } = await callWardens('apply_patch', patchArgs);

    expect(handler).toHaveBeenCalledTimes(1);
    expect(capturedWardenCalls).toHaveLength(1);
    const call = capturedWardenCalls[0]!;
    expectArgvContract(call.argv, 'plan-review-gate');
    expect(JSON.parse(call.stdin)).toEqual({
      cwd: WORKTREE_ROOT,
      tool_name: 'apply_patch',
      tool_input: patchArgs,
    });
    expect(log).toEqual([{ toolName: 'apply_patch', decision: 'allow' }]);
  });

  it('commit-shaped Bash identity-maps every commit gate to unchanged tool_name/tool_input', async () => {
    const commitArgs = { command: 'git commit -m "identity check"' };
    await callWardens('Bash', commitArgs);

    expect(capturedWardenCalls).toHaveLength(3);
    for (const call of capturedWardenCalls) {
      expect(JSON.parse(call.stdin)).toEqual({
        cwd: WORKTREE_ROOT,
        tool_name: 'Bash',
        tool_input: commitArgs,
      });
    }
  });

  it('commit-shaped Bash invokes code-review-gate, ai-eng-gate, and verification-gate sequentially in tuple order', async () => {
    const { handler } = await callWardens('Bash', {
      command: 'git commit -m order',
    });

    expect(handler).toHaveBeenCalledTimes(1);
    const behaviorsInvoked = capturedWardenCalls.map((c) => c.argv[3]);
    expect(behaviorsInvoked).toEqual([
      'code-review-gate',
      'ai-eng-gate',
      'verification-gate',
    ]);
  });
});

describe('wardens middleware — commit gates can each independently deny and short-circuit', () => {
  it('code-review-gate can deny a git commit: no later behavior runs, handler never called', async () => {
    setWardenResponse('code-review-gate', {
      kind: 'stdout',
      stdout: JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'deny',
          permissionDecisionReason: '[code-review-gate] BLOCKED: sentinel',
        },
      }),
    });
    const { handler, log, result } = await callWardens('Bash', {
      command: 'git commit -m x',
    });

    expect(handler).not.toHaveBeenCalled();
    expect(capturedWardenCalls.map((c) => c.argv[3])).toEqual([
      'code-review-gate',
    ]);
    expect(result).toBeInstanceOf(ToolMessage);
    expect((result as ToolMessage).status).toBe('error');
    expect((result as ToolMessage).content).toBe(
      '[code-review-gate] BLOCKED: sentinel',
    );
    expect(log).toEqual([
      {
        toolName: 'Bash',
        decision: 'deny',
        reason: '[code-review-gate] BLOCKED: sentinel',
      },
    ]);
  });

  it('ai-eng-gate can deny after code-review allows: verification never invoked, handler never called', async () => {
    setWardenResponse('ai-eng-gate', {
      kind: 'stdout',
      stdout: JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'deny',
          permissionDecisionReason: '[ai-eng-gate] BLOCKED: sentinel',
        },
      }),
    });
    const { handler } = await callWardens('Bash', {
      command: 'git commit -m x',
    });

    expect(handler).not.toHaveBeenCalled();
    expect(capturedWardenCalls.map((c) => c.argv[3])).toEqual([
      'code-review-gate',
      'ai-eng-gate',
    ]);
  });

  it('verification-gate can deny after code-review and ai-eng allow: handler never called', async () => {
    setWardenResponse('verification-gate', {
      kind: 'stdout',
      stdout: JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'deny',
          permissionDecisionReason: '[verification-gate] BLOCKED: sentinel',
        },
      }),
    });
    const { handler } = await callWardens('Bash', {
      command: 'git commit -m x',
    });

    expect(handler).not.toHaveBeenCalled();
    expect(capturedWardenCalls.map((c) => c.argv[3])).toEqual([
      'code-review-gate',
      'ai-eng-gate',
      'verification-gate',
    ]);
  });
});

describe('wardens middleware — the stdout JSON is the only deny signal, never the exit code', () => {
  it('empty stdout allows even though the underlying gate process "exits zero" (no exit-code inspection at all)', async () => {
    // The fake never models a distinct nonzero-exit-but-empty-stdout case
    // because codex_warden_hooks.py's own contract makes that combination
    // impossible for these four runners (scripts/codex_warden_hooks.py:
    // 3795-3810) — every callback in this suite either supplies an explicit
    // execFile ERROR (case below) or a clean stdout string; there is no
    // third "exit code" input anywhere in the production code path being
    // exercised, which is itself the proof that exit code is never
    // consulted.
    const { handler } = await callWardens('apply_patch', { patch: 'x' });
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('a deny JSON blocks regardless of process exit status', async () => {
    setWardenResponse(PLAN_REVIEW_BEHAVIOR_NAME, {
      kind: 'stdout',
      stdout: JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'deny',
          permissionDecisionReason: '[plan-review-gate] BLOCKED: exit-zero',
        },
      }),
    });
    const { handler, result } = await callWardens('apply_patch', {
      patch: 'x',
    });
    expect(handler).not.toHaveBeenCalled();
    expect((result as ToolMessage).content).toBe(
      '[plan-review-gate] BLOCKED: exit-zero',
    );
  });
});

describe('wardens middleware — fail-closed on every infrastructure/protocol failure', () => {
  it('a spawn/non-zero subprocess failure fails closed with sanitized REVISE feedback', async () => {
    const err = new Error('spawn python3 ENOENT') as NodeJS.ErrnoException;
    err.code = 'ENOENT';
    setWardenResponse(PLAN_REVIEW_BEHAVIOR_NAME, { kind: 'error', error: err });

    const { handler, result, log } = await callWardens('apply_patch', {
      patch: 'x',
    });

    expect(handler).not.toHaveBeenCalled();
    const message = result as ToolMessage;
    expect(message.status).toBe('error');
    const content = String(message.content);
    expect(content).toContain('[plan-review-gate] REVISE');
    expect(content).toContain('could not be evaluated');
    expect(content).not.toContain('ENOENT');
    expect(content).not.toContain('spawn python3');
    expect(log[0]?.decision).toBe('deny');
  });

  it('a subprocess timeout fails closed with sanitized REVISE feedback', async () => {
    const err = new Error('command timed out') as NodeJS.ErrnoException & {
      killed?: boolean;
      signal?: string;
    };
    err.killed = true;
    err.signal = 'SIGTERM';
    setWardenResponse(PLAN_REVIEW_BEHAVIOR_NAME, { kind: 'error', error: err });

    const { handler, result } = await callWardens('apply_patch', {
      patch: 'x',
    });

    expect(handler).not.toHaveBeenCalled();
    const content = String((result as ToolMessage).content);
    expect(content).toContain('[plan-review-gate] REVISE');
    expect(content).not.toContain('SIGTERM');
  });

  it('a stdin/EPIPE failure fails closed and settles exactly once (no unhandled rejection, no double resolution)', async () => {
    setWardenResponse(PLAN_REVIEW_BEHAVIOR_NAME, {
      kind: 'stdin-error',
      error: new Error('EPIPE'),
    });

    const { handler, result } = await callWardens('apply_patch', {
      patch: 'x',
    });

    expect(handler).not.toHaveBeenCalled();
    const content = String((result as ToolMessage).content);
    expect(content).toContain('[plan-review-gate] REVISE');
    expect(content).not.toContain('EPIPE');
  });

  it('malformed (non-JSON) stdout fails closed with sanitized REVISE feedback', async () => {
    setWardenResponse(PLAN_REVIEW_BEHAVIOR_NAME, {
      kind: 'stdout',
      stdout: 'not-json-at-all{{{',
    });

    const { handler, result } = await callWardens('apply_patch', {
      patch: 'x',
    });

    expect(handler).not.toHaveBeenCalled();
    expect(String((result as ToolMessage).content)).toContain(
      '[plan-review-gate] REVISE',
    );
  });

  it('unexpected non-empty JSON (not the deny contract) fails closed as protocol drift', async () => {
    setWardenResponse(PLAN_REVIEW_BEHAVIOR_NAME, {
      kind: 'stdout',
      stdout: JSON.stringify({ unexpected: 'shape' }),
    });

    const { handler, result } = await callWardens('apply_patch', {
      patch: 'x',
    });

    expect(handler).not.toHaveBeenCalled();
    expect(String((result as ToolMessage).content)).toContain(
      '[plan-review-gate] REVISE',
    );
  });

  it('a deny object with an empty-string reason is treated as protocol drift, not a usable deny', async () => {
    setWardenResponse(PLAN_REVIEW_BEHAVIOR_NAME, {
      kind: 'stdout',
      stdout: JSON.stringify({
        hookSpecificOutput: {
          hookEventName: 'PreToolUse',
          permissionDecision: 'deny',
          permissionDecisionReason: '',
        },
      }),
    });

    const { handler, result } = await callWardens('apply_patch', {
      patch: 'x',
    });

    expect(handler).not.toHaveBeenCalled();
    expect(String((result as ToolMessage).content)).toContain(
      '[plan-review-gate] REVISE',
    );
  });
});

describe('wardens middleware — Git-common-dir repo-root resolution', () => {
  it('derives repo_root from the git-common-dir parent, not the worktree/module path (this IS a linked worktree)', () => {
    // No mocking needed: this test file already runs inside a linked
    // worktree, so WORKTREE_ROOT (this file's process.cwd()) and REPO_ROOT
    // (this suite's own real git-common-dir computation above) are
    // genuinely different absolute paths — the same distinction
    // `.claude/hooks/warden-shim.sh` documents and the frozen oracle
    // hardcodes.
    expect(REPO_ROOT).not.toBe(WORKTREE_ROOT);
  });

  it('normalizes a relative wardenCwd to an absolute event.cwd (path.resolve is applied inside the factory itself, not only by callers)', async () => {
    // buildWardensMiddleware/buildMiddlewareStack are public entry points —
    // a relative wardenCwd must not leak a relative `cwd` into the
    // serialized PreToolUse event even though today's one production caller
    // (deus-native-backend.ts) already resolves it first.
    const { middleware } = buildWardensMiddleware('.');
    const handler = vi.fn(
      () => new ToolMessage({ content: 'ok', tool_call_id: 'call_direct_1' }),
    );
    await invokeWrapToolCall(
      middleware,
      makeToolCallRequest('apply_patch', { patch: 'x' }),
      handler,
    );

    expect(capturedWardenCalls).toHaveLength(1);
    const event = JSON.parse(capturedWardenCalls[0]!.stdin) as { cwd: string };
    expect(event.cwd).toBe(WORKTREE_ROOT);
    expect(event.cwd).not.toBe('.');
  });

  it('a matched call sends --repo-root and the script path from the git-common-dir parent, never the worktree path', async () => {
    await callWardens('apply_patch', { patch: 'x' });
    const call = capturedWardenCalls[0]!;
    expect(call.argv[1]).toBe(WARDEN_SCRIPT);
    expect(call.argv[1]).not.toContain(WORKTREE_ROOT);
    const flagIndex = call.argv.indexOf('--repo-root');
    expect(call.argv[flagIndex + 1]).toBe(REPO_ROOT);
  });

  it('falls back to the module-relative path when the Git query fails (non-git cwd / git unavailable)', async () => {
    execFileSyncMock.mockImplementationOnce(() => {
      throw new Error('fatal: not a git repository');
    });

    // Constructed fresh so the failing execFileSync call above is the ONE
    // this construction consumes.
    const { middleware } = buildWardensMiddleware('/tmp/not-a-git-repo');
    const handler = vi.fn(
      async () =>
        new ToolMessage({ content: 'ok', tool_call_id: 'call_fallback_1' }),
    );
    await invokeWrapToolCall(
      middleware,
      makeToolCallRequest('apply_patch', { patch: 'x' }, 'call_fallback_1'),
      handler,
    );

    expect(capturedWardenCalls).toHaveLength(1);
    const call = capturedWardenCalls[0]!;
    // The fallback script path is two directories above middleware-stack.ts
    // (src/agent-runtimes/../.. = the checkout root) — NOT the git-derived
    // REPO_ROOT above and NOT the bogus wardenCwd.
    expect(call.argv[1]).not.toBe(WARDEN_SCRIPT);
    expect(call.argv[1]).not.toContain('/tmp/not-a-git-repo');
    expect(call.argv[1].endsWith('scripts/codex_warden_hooks.py')).toBe(true);
  });
});

describe('wardens middleware — Python commit prefilter matches the exact GIT_COMMIT_RE contract', () => {
  it.each([
    ['git commit -m x', true],
    ['git -C /repo commit -m x', true],
    ['echo hi && git commit -m x', true],
    ['echo git commit', false],
    ['git status', false],
    ['git commitment -m x', false],
  ] as const)('%s -> commit-shaped: %s', async (command, isCommitShaped) => {
    await callWardens('Bash', { command });
    expect(capturedWardenCalls.length > 0).toBe(isCommitShaped);
  });
});

describe('wardens middleware — wardens:false toggle omits the layer entirely (no Git, no Python)', () => {
  it('buildMiddlewareStack with wardens:false performs no Git or Python subprocess call', () => {
    execFileSyncMock.mockClear();
    const { middleware } = buildMiddlewareStack({ wardens: false });
    expect(middleware.map((m) => m.name)).not.toContain('wardens');
    expect(execFileSyncMock).not.toHaveBeenCalled();
    expect(execFileMock).not.toHaveBeenCalled();
  });
});
