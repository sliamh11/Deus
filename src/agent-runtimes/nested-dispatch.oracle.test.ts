/**
 * Oracle tests for LIA-408/B8 — Deus-owned nested subagent dispatch.
 *
 * @oracle Independently authored from the LIA-408 task brief and acceptance
 * criteria, BLIND to the implementation. At authoring time there is no
 * nested-dispatch production module; deus-native-backend.ts still lists
 * nested subagent dispatch as a B8 non-goal. Must not be weakened by the
 * implementer; strengthen instead of loosening if a real contract gap is
 * found during implementation.
 *
 * SPEC (observable intent from the task brief):
 *   - A parent agent can dispatch a nested createAgent instance.
 *   - Every dispatch explicitly declares an output contract.
 *   - Output that violates that contract is rejected or returned as a
 *     visible contract failure; it must never masquerade as success.
 *   - A subagent selects its model independently of the parent and of other
 *     subagents.
 *   - Every dispatch outcome carries traceable agent-id and model metadata.
 *
 * MINIMAL TEST SEAM:
 *   - `createNestedDispatcher({ resolveModel })` returns an object with an
 *     async `dispatch(request)` method.
 *   - A request contains `agentId`, `model`, `prompt`, and `outputContract`.
 *     `outputContract` is a Zod-compatible schema whose successful parse is
 *     returned as `output`.
 *   - A successful result has `status: 'success'`, the validated `output`,
 *     and `metadata: { agentId, model }`.
 *   - A contract violation may either reject or return a non-success result.
 *     In both forms it identifies the failure as contract-related and keeps
 *     the same trace metadata.
 *
 * The seam deliberately does NOT prescribe checkpoint sharing, middleware
 * inheritance, parallelism, tool exposure, or a concrete failure class: the
 * available spec makes none of those decisions.
 *
 * RED PROOF: `nested-dispatch.ts` does not exist in the pre-B8 tree, so this
 * file currently fails at import resolution. Once that module exists, the
 * behavioral assertions below remain discriminating and hermetic: both the
 * parent and child use LangChain's FakeToolCallingModel and perform no live
 * model or network calls.
 */

// This module is intentionally absent before LIA-408. Import-resolution
// failure is the initial red state; the tests then lock the behavior.
import { createNestedDispatcher } from './nested-dispatch.js';

import { createAgent, FakeToolCallingModel, tool } from 'langchain';
import { describe, expect, it, vi } from 'vitest';
import { z } from 'zod';

const { createAgentConfigs } = vi.hoisted(() => ({
  createAgentConfigs: [] as unknown[],
}));

// AC1 names nested createAgent instances as part of the required primitive,
// so wrap the real factory rather than mocking the graph. Both parent and
// child agents remain genuine LangChain agents; this records how many were
// constructed and with which model.
vi.mock('langchain', async (importOriginal) => {
  const actual = await importOriginal<typeof import('langchain')>();
  return {
    ...actual,
    createAgent: (config: Parameters<typeof actual.createAgent>[0]) => {
      createAgentConfigs.push(config);
      return actual.createAgent(config);
    },
  };
});

const RESEARCH_MODEL = 'sub-model-research-oracle';
const REVIEW_MODEL = 'sub-model-review-oracle';

const EXPECTED_RESEARCH_OUTPUT = {
  summary: 'nested agent completed the research',
  confidence: 0.92,
};

const RESEARCH_OUTPUT_CONTRACT = z.preprocess(
  (raw) => {
    if (typeof raw !== 'string') return raw;
    try {
      return JSON.parse(raw) as unknown;
    } catch {
      return raw;
    }
  },
  z.object({
    summary: z.string().min(1),
    confidence: z.number().min(0).max(1),
  }),
);

/**
 * FakeToolCallingModel normally emits a bare final AIMessage. Stamp a known
 * final content value on it, including every model returned by bindTools —
 * createAgent always rebinds models, and FakeToolCallingModel constructs a
 * fresh instance when that happens.
 */
function modelReturning(content: string): FakeToolCallingModel {
  const decorate = (model: FakeToolCallingModel): FakeToolCallingModel => {
    const originalGenerate = model._generate.bind(model);
    model._generate = async (
      ...args: Parameters<FakeToolCallingModel['_generate']>
    ) => {
      const result = await originalGenerate(...args);
      const generation = result.generations[0];
      if (generation) {
        generation.text = content;
        generation.message.content = content;
      }
      return result;
    };

    const originalBindTools = model.bindTools.bind(model);
    model.bindTools = (
      ...args: Parameters<FakeToolCallingModel['bindTools']>
    ) => {
      const bound = originalBindTools(...args);
      return bound instanceof FakeToolCallingModel ? decorate(bound) : bound;
    };
    return model;
  };

  return decorate(new FakeToolCallingModel({ toolCalls: [[]] }));
}

function parentCalling(toolName: string): FakeToolCallingModel {
  return new FakeToolCallingModel({
    toolCalls: [
      [
        {
          name: toolName,
          args: { task: 'research the requested topic' },
          id: 'parent_call_1',
        },
      ],
      [],
    ],
  });
}

function expectTraceMetadata(
  carrier: unknown,
  expected: { agentId: string; model: string },
): void {
  const record = carrier as {
    agentId?: unknown;
    model?: unknown;
    metadata?: { agentId?: unknown; model?: unknown };
  };
  expect(record.metadata ?? record).toMatchObject(expected);
}

async function expectContractFailure(
  operation: Promise<unknown>,
  expectedMetadata: { agentId: string; model: string },
): Promise<void> {
  const settled = await operation.then(
    (value) => ({ kind: 'returned' as const, value }),
    (reason: unknown) => ({ kind: 'rejected' as const, reason }),
  );

  if (settled.kind === 'returned') {
    const result = settled.value as {
      status?: unknown;
      error?: unknown;
    };
    expect(result.status).not.toBe('success');
    expect(JSON.stringify(result)).toMatch(/contract/i);
    expectTraceMetadata(result, expectedMetadata);
  } else {
    expect(String(settled.reason)).toMatch(/contract/i);
    expectTraceMetadata(settled.reason, expectedMetadata);
  }
}

describe('@oracle nested dispatch — parent-to-child contract (LIA-408 AC1, AC2, AC4, AC5)', () => {
  it('@oracle a real parent agent tool call returns validated child output with independent model metadata', async () => {
    // @oracle: AC1 + AC2 + AC4 + AC5 — the parent dispatches a nested agent;
    // the dispatch declares a contract and returns validated output together
    // with the selected child agent/model identity.
    const childModel = modelReturning(JSON.stringify(EXPECTED_RESEARCH_OUTPUT));
    const resolveModel = vi.fn((model: string) => {
      if (model !== RESEARCH_MODEL) {
        throw new Error(`unexpected oracle model request: ${model}`);
      }
      return childModel;
    });
    const dispatcher = createNestedDispatcher({ resolveModel });
    let observedDispatchResult: unknown;
    createAgentConfigs.length = 0;

    const dispatchTool = tool(
      async (args: { task: string }) => {
        observedDispatchResult = await dispatcher.dispatch({
          agentId: 'researcher',
          model: RESEARCH_MODEL,
          prompt: args.task,
          outputContract: RESEARCH_OUTPUT_CONTRACT,
        });
        return JSON.stringify(observedDispatchResult);
      },
      {
        name: 'dispatch_nested_agent',
        description: 'Dispatches the oracle research subagent.',
        schema: {
          type: 'object',
          properties: { task: { type: 'string' } },
          required: ['task'],
          additionalProperties: false,
        },
      },
    );

    const parentModel = parentCalling(dispatchTool.name);
    const parent = createAgent({
      model: parentModel,
      tools: [dispatchTool],
    });
    await parent.invoke({
      messages: [{ role: 'user', content: 'delegate this research task' }],
    });

    expect(resolveModel).toHaveBeenCalledOnce();
    expect(resolveModel).toHaveBeenCalledWith(RESEARCH_MODEL);
    expect(createAgentConfigs).toHaveLength(2);
    expect((createAgentConfigs[0] as { model: unknown }).model).toBe(
      parentModel,
    );
    expect((createAgentConfigs[1] as { model: unknown }).model).toBe(
      childModel,
    );
    expect(childModel).not.toBe(parentModel);
    expect(observedDispatchResult).toMatchObject({
      status: 'success',
      output: EXPECTED_RESEARCH_OUTPUT,
      metadata: {
        agentId: 'researcher',
        model: RESEARCH_MODEL,
      },
    });
  });

  it('@oracle resolves the model separately for every subagent dispatch', async () => {
    // @oracle: AC4 — model choice is per dispatch, not inherited from the
    // parent or cached globally from the first child.
    const resolveModel = vi.fn((model: string) => {
      if (model === RESEARCH_MODEL) {
        return modelReturning(
          JSON.stringify({ summary: 'research result', confidence: 0.8 }),
        );
      }
      if (model === REVIEW_MODEL) {
        return modelReturning(
          JSON.stringify({ summary: 'review result', confidence: 0.7 }),
        );
      }
      throw new Error(`unexpected oracle model request: ${model}`);
    });
    const dispatcher = createNestedDispatcher({ resolveModel });

    const research = await dispatcher.dispatch({
      agentId: 'researcher',
      model: RESEARCH_MODEL,
      prompt: 'research',
      outputContract: RESEARCH_OUTPUT_CONTRACT,
    });
    const review = await dispatcher.dispatch({
      agentId: 'reviewer',
      model: REVIEW_MODEL,
      prompt: 'review',
      outputContract: RESEARCH_OUTPUT_CONTRACT,
    });

    expect(resolveModel.mock.calls.map(([model]) => model)).toEqual([
      RESEARCH_MODEL,
      REVIEW_MODEL,
    ]);
    expect(research).toMatchObject({
      status: 'success',
      output: { summary: 'research result', confidence: 0.8 },
      metadata: { agentId: 'researcher', model: RESEARCH_MODEL },
    });
    expect(review).toMatchObject({
      status: 'success',
      output: { summary: 'review result', confidence: 0.7 },
      metadata: { agentId: 'reviewer', model: REVIEW_MODEL },
    });
  });
});

describe('@oracle nested dispatch — output-contract enforcement (LIA-408 AC2, AC3, AC5)', () => {
  it('@oracle invalid child output is never reported as success and retains trace metadata', async () => {
    // @oracle: AC3 + AC5 — schema-invalid output is rejected or surfaced as
    // a contract failure, with the responsible child and model identifiable.
    const resolveModel = vi.fn(() =>
      modelReturning(JSON.stringify({ summary: '', confidence: 'definitely' })),
    );
    const dispatcher = createNestedDispatcher({ resolveModel });

    await expectContractFailure(
      dispatcher.dispatch({
        agentId: 'researcher',
        model: RESEARCH_MODEL,
        prompt: 'return malformed output',
        outputContract: RESEARCH_OUTPUT_CONTRACT,
      }),
      { agentId: 'researcher', model: RESEARCH_MODEL },
    );
  });

  it('@oracle omitting the output contract fails before any child model is selected', async () => {
    // @oracle: AC2 — an output contract is mandatory for EACH dispatch;
    // dynamically malformed callers fail closed before child execution.
    const resolveModel = vi.fn(() =>
      modelReturning(JSON.stringify(EXPECTED_RESEARCH_OUTPUT)),
    );
    const dispatcher = createNestedDispatcher({ resolveModel });

    await expectContractFailure(
      dispatcher.dispatch({
        agentId: 'researcher',
        model: RESEARCH_MODEL,
        prompt: 'this request deliberately has no contract',
      } as never),
      { agentId: 'researcher', model: RESEARCH_MODEL },
    );
    expect(resolveModel).not.toHaveBeenCalled();
  });
});
