import { describe, it, expect, afterEach, vi } from 'vitest';
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
  buildMiddlewareStack,
  buildPermissionsMiddleware,
  parseMiddlewareStackConfig,
  resolveMiddlewareStackConfig,
  type MiddlewareLayerName,
  type OrderMarker,
} from './middleware-stack.js';
import type { MemoryRetrievalRequest } from './memory-retrieval.js';

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
