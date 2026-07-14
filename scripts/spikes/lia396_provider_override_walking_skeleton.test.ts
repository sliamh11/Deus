import { ChatAnthropic } from '@langchain/anthropic';
import { AIMessage } from '@langchain/core/messages';
import { ChatOllama } from '@langchain/ollama';
import { createAgent, type ModelRequest } from 'langchain';
import { describe, expect, it, vi } from 'vitest';

import {
  CLAUDE_MODEL_ID,
  OLLAMA_BASE_URL,
  OLLAMA_SUB_MODEL_ID,
  createProviderRoutingMiddleware,
  defaultMainSubResolver,
  makeDelegateToSubagentTool,
  makeScriptedMainStub,
} from './lia396_provider_override_walking_skeleton.js';

/**
 * The integration test below requires a real, reachable local Ollama server
 * with OLLAMA_SUB_MODEL_ID actually pulled — infrastructure this repo's
 * default test suite cannot assume (CI, other contributors' machines, or
 * even a machine with Ollama running but a different model set). Self-gates
 * via a live probe (daemon reachable AND the specific model present in
 * /api/tags, short timeout) rather than a manual opt-in env var, mirroring
 * the ANTHROPIC_API_KEY-presence gate used for the Claude smoke test below
 * — the suite adapts to what's actually available instead of failing with a
 * missing-model error on a machine that has Ollama but not this model.
 */
async function isOllamaSubModelAvailable(): Promise<boolean> {
  try {
    const response = await fetch(`${OLLAMA_BASE_URL}/api/tags`, {
      signal: AbortSignal.timeout(1000),
    });
    if (!response.ok) return false;
    const body = (await response.json()) as {
      models?: Array<{ name?: string; model?: string }>;
    };
    return (body.models ?? []).some(
      (m) => m.name === OLLAMA_SUB_MODEL_ID || m.model === OLLAMA_SUB_MODEL_ID,
    );
  } catch {
    return false;
  }
}

const OLLAMA_AVAILABLE = await isOllamaSubModelAvailable();

/**
 * Minimal fake ModelRequest for direct wrapModelCall invocation — only the
 * fields the middleware actually reads (request.model, request.runtime
 * .context.role) need to be present, mirroring A2's own precedent
 * (lia395_warden_veto_walking_skeleton.test.ts:251-255) of casting a
 * minimal object literal rather than fully satisfying ModelRequest's shape.
 */
function fakeRequest(
  role: string | undefined,
  model: ModelRequest['model'],
): ModelRequest {
  return {
    model,
    messages: [],
    runtime: { context: role === undefined ? undefined : { role } },
  } as unknown as ModelRequest;
}

describe('createProviderRoutingMiddleware — selection', () => {
  it("selects a real ChatAnthropic instance for role 'main'", async () => {
    const { middleware } = createProviderRoutingMiddleware({
      resolveModel: defaultMainSubResolver(),
    });
    const canned = new AIMessage({ content: 'ok' });
    const handler = vi.fn().mockResolvedValue(canned);

    await middleware.wrapModelCall!(
      fakeRequest('main', new ChatOllama({ model: OLLAMA_SUB_MODEL_ID })),
      handler,
    );

    expect(handler).toHaveBeenCalledTimes(1);
    const passedRequest = handler.mock.calls[0]?.[0] as ModelRequest;
    expect(passedRequest.model).toBeInstanceOf(ChatAnthropic);
    expect((passedRequest.model as ChatAnthropic).model).toBe(CLAUDE_MODEL_ID);
  });

  it("selects a real ChatOllama instance for role 'sub'", async () => {
    const { middleware } = createProviderRoutingMiddleware({
      resolveModel: defaultMainSubResolver(),
    });
    const canned = new AIMessage({ content: 'ok' });
    const handler = vi.fn().mockResolvedValue(canned);

    await middleware.wrapModelCall!(
      fakeRequest(
        'sub',
        new ChatAnthropic({ model: CLAUDE_MODEL_ID, apiKey: 'placeholder' }),
      ),
      handler,
    );

    expect(handler).toHaveBeenCalledTimes(1);
    const passedRequest = handler.mock.calls[0]?.[0] as ModelRequest;
    expect(passedRequest.model).toBeInstanceOf(ChatOllama);
    expect((passedRequest.model as ChatOllama).model).toBe(OLLAMA_SUB_MODEL_ID);
  });

  it('passes the original request.model through for an unrecognized role', async () => {
    const { middleware } = createProviderRoutingMiddleware({
      resolveModel: defaultMainSubResolver(),
    });
    const original = new ChatOllama({ model: OLLAMA_SUB_MODEL_ID });
    const handler = vi.fn().mockResolvedValue(new AIMessage({ content: 'ok' }));

    await middleware.wrapModelCall!(
      fakeRequest('mystery-role', original),
      handler,
    );

    const passedRequest = handler.mock.calls[0]?.[0] as ModelRequest;
    expect(passedRequest.model).toBe(original);
  });
});

describe('createProviderRoutingMiddleware — routing log', () => {
  it('records requestIndex/role/providerClass/modelId/executed in order', async () => {
    const { middleware, log } = createProviderRoutingMiddleware({
      resolveModel: defaultMainSubResolver(),
    });
    const handler = vi.fn().mockResolvedValue(new AIMessage({ content: 'ok' }));

    await middleware.wrapModelCall!(
      fakeRequest('main', new ChatOllama({ model: OLLAMA_SUB_MODEL_ID })),
      handler,
    );
    await middleware.wrapModelCall!(
      fakeRequest('sub', new ChatOllama({ model: OLLAMA_SUB_MODEL_ID })),
      handler,
    );

    expect(log).toHaveLength(2);
    expect(log[0]).toMatchObject({
      requestIndex: 0,
      role: 'main',
      providerClass: 'ChatAnthropic',
      modelId: CLAUDE_MODEL_ID,
      executed: true,
    });
    expect(log[1]).toMatchObject({
      requestIndex: 1,
      role: 'sub',
      providerClass: 'ChatOllama',
      modelId: OLLAMA_SUB_MODEL_ID,
      executed: true,
    });
  });

  it('records executed:false when executeOverride short-circuits the call', async () => {
    const { middleware, log } = createProviderRoutingMiddleware({
      resolveModel: defaultMainSubResolver(),
      executeOverride: (role) =>
        role === 'main' ? new AIMessage({ content: 'stubbed' }) : undefined,
    });
    const handler = vi.fn().mockResolvedValue(new AIMessage({ content: 'ok' }));

    const result = await middleware.wrapModelCall!(
      fakeRequest('main', new ChatOllama({ model: OLLAMA_SUB_MODEL_ID })),
      handler,
    );

    expect(handler).not.toHaveBeenCalled();
    expect((result as AIMessage).content).toBe('stubbed');
    expect(log[0]).toMatchObject({ role: 'main', executed: false });
  });
});

describe('one logical session — nested delegate_to_subagent (real Ollama call)', () => {
  const maybeIt = OLLAMA_AVAILABLE ? it : it.skip;

  maybeIt(
    'routes main -> sub -> main within one agent.invoke() call, sub leg genuinely live',
    async () => {
      const { middleware: sharedMiddleware, log } =
        createProviderRoutingMiddleware({
          resolveModel: defaultMainSubResolver(),
          executeOverride: makeScriptedMainStub(),
        });
      const placeholderModel = new ChatOllama({ model: OLLAMA_SUB_MODEL_ID });
      const delegateTool = makeDelegateToSubagentTool(
        sharedMiddleware,
        placeholderModel,
      );

      const mainAgent = createAgent({
        model: placeholderModel,
        tools: [delegateTool],
        middleware: [sharedMiddleware],
      });

      const result = await mainAgent.invoke(
        {
          messages: [
            {
              role: 'user',
              content:
                'Delegate to your subagent to find out its favorite fruit, then report back.',
            },
          ],
        },
        { context: { role: 'main' } },
      );

      const subEntries = log.filter((r) => r.role === 'sub');
      const mainEntries = log.filter((r) => r.role === 'main');

      expect(subEntries).toHaveLength(1);
      expect(subEntries[0]).toMatchObject({
        providerClass: 'ChatOllama',
        executed: true,
      });

      expect(mainEntries.length).toBeGreaterThanOrEqual(2);
      expect(
        mainEntries.every(
          (r) => r.providerClass === 'ChatAnthropic' && r.executed === false,
        ),
      ).toBe(true);

      const subIndex = subEntries[0]!.requestIndex;
      expect(mainEntries.some((r) => r.requestIndex < subIndex)).toBe(true);
      expect(mainEntries.some((r) => r.requestIndex > subIndex)).toBe(true);

      const toolMessage = result.messages.find(
        (m) => (m as { name?: string }).name === 'delegate_to_subagent',
      );
      expect(toolMessage).toBeDefined();
      const payload = JSON.parse(String(toolMessage!.content)) as {
        subagent_answer: string;
      };
      // Real local Ollama response — non-empty, not the canned main-leg stub string.
      expect(payload.subagent_answer.length).toBeGreaterThan(0);
      expect(payload.subagent_answer).not.toBe(
        'Delegated to the subagent and got an answer back.',
      );
    },
    30_000,
  );
});

describe('optional live full-Claude smoke test', () => {
  const hasCredential = Boolean(process.env.ANTHROPIC_API_KEY);
  const maybeIt = hasCredential ? it : it.skip;

  maybeIt(
    'runs the same one-session flow with a real Claude call, no executeOverride',
    async () => {
      const { middleware: sharedMiddleware, log } =
        createProviderRoutingMiddleware({
          resolveModel: defaultMainSubResolver(),
        });
      const placeholderModel = new ChatOllama({ model: OLLAMA_SUB_MODEL_ID });
      const delegateTool = makeDelegateToSubagentTool(
        sharedMiddleware,
        placeholderModel,
      );

      const mainAgent = createAgent({
        model: placeholderModel,
        tools: [delegateTool],
        middleware: [sharedMiddleware],
      });

      await mainAgent.invoke(
        {
          messages: [
            {
              role: 'user',
              content:
                'Delegate to your subagent to find out its favorite fruit, then report back.',
            },
          ],
        },
        { context: { role: 'main' } },
      );

      const mainEntries = log.filter((r) => r.role === 'main');
      expect(mainEntries.some((r) => r.executed === true)).toBe(true);
    },
    60_000,
  );
});
