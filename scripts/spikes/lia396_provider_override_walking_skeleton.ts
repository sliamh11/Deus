import { fileURLToPath } from 'node:url';
import path from 'node:path';

import { ChatAnthropic } from '@langchain/anthropic';
import { AIMessage } from '@langchain/core/messages';
import { ChatOllama } from '@langchain/ollama';
import {
  createAgent,
  createMiddleware,
  tool,
  type AgentMiddleware,
  type ModelRequest,
} from 'langchain';

export const CLAUDE_MODEL_ID = 'claude-opus-4-8'; // same as A1's MODEL_ID
export const OLLAMA_SUB_MODEL_ID = 'gemma4:e2b';
// Reuses the repo's existing OLLAMA_URL convention (.env.example:36, used
// elsewhere for embeddings/judge) rather than introducing a new env var —
// mapped to @langchain/ollama's baseUrl constructor field internally.
export const OLLAMA_BASE_URL =
  process.env.OLLAMA_URL ?? 'http://localhost:11434';
const ANTHROPIC_PLACEHOLDER_KEY = 'sk-ant-lia396-placeholder-never-sent';

export type ProviderRole = string; // deliberately open (not 'main'|'sub'
// union) so A7 can add more roles without touching this file.

export interface ProviderRoutingRecord {
  /** Monotonic, 0-based, across this middleware instance's whole lifetime —
   *  lets a caller reconstruct request order across a nested session. */
  requestIndex: number;
  role: ProviderRole;
  /** request.model's constructor name AFTER selection, e.g. 'ChatAnthropic'. */
  providerClass: string;
  /** Best-effort model id: (model as { model?: string }).model. */
  modelId: string | undefined;
  /** true = the real LangChain handler ran (network call happened);
   *  false = executeOverride short-circuited it (e.g. no live credential). */
  executed: boolean;
}

/**
 * Resolves which model instance should handle a request for a given role.
 * Return the request's own request.model unchanged to mean "no override for
 * this role" (passthrough). Strategy-pattern seam (interchangeable per-role
 * algorithm, matching A2's `invokeGate` injectable-seam precedent) — this IS
 * the thing under test/reuse; A7 supplies its own resolver per benchmark
 * matrix cell.
 */
export type ProviderResolver = (
  role: ProviderRole,
  request: ModelRequest,
) => ModelRequest['model'];

/**
 * Optional per-role execution override (Strategy-pattern seam, same family
 * as ProviderResolver). Return undefined to mean "call the real handler"
 * (default for every role). Return an AIMessage to short-circuit the real
 * network call — used ONLY to route around a known, disclosed environmental
 * gap (e.g. missing credential), never silently.
 *
 * IMPORTANT: a caller driving a tool-calling loop from a stub MUST close
 * over call-count state and script a tool_calls-bearing AIMessage on the
 * turn that should trigger a tool, and a plain AIMessage on the turn that
 * should end the loop — same shape as FakeToolCallingModel's scripted
 * toolCalls array, just applied here (a stateless single-canned-response
 * stub would never actually invoke a tool, so a nested subagent leg would
 * never fire).
 */
export type ExecuteOverride = (
  role: ProviderRole,
  request: ModelRequest,
  model: ModelRequest['model'],
) => Promise<AIMessage | undefined> | AIMessage | undefined;

export interface CreateProviderRoutingMiddlewareOptions {
  resolveModel: ProviderResolver;
  executeOverride?: ExecuteOverride;
  /** role read when request.runtime.context is missing/has no `.role`. */
  defaultRole?: ProviderRole;
}

export interface ProviderRoutingMiddleware {
  middleware: AgentMiddleware;
  log: ProviderRoutingRecord[];
}

/**
 * Reusable primitive: a real wrapModelCall middleware that selects a model
 * per-request based on request.runtime.context.role, and records every
 * selection (and whether it actually executed) to an inspectable log.
 * A7 (LIA-400) imports this directly for its cross-provider benchmark
 * matrix — no reimplementation needed.
 */
export function createProviderRoutingMiddleware(
  options: CreateProviderRoutingMiddlewareOptions,
): ProviderRoutingMiddleware {
  const log: ProviderRoutingRecord[] = [];
  let requestIndex = 0;

  const middleware = createMiddleware({
    name: 'ProviderRouting',
    wrapModelCall: async (request, handler) => {
      const role =
        (request.runtime.context as { role?: ProviderRole } | undefined)
          ?.role ??
        options.defaultRole ??
        'unknown';
      const model = options.resolveModel(role, request);
      const index = requestIndex++;

      const override = await options.executeOverride?.(role, request, model);
      if (override) {
        log.push({
          requestIndex: index,
          role,
          providerClass: model.constructor.name,
          modelId: (model as { model?: string }).model,
          executed: false,
        });
        return override;
      }

      const result = await handler({ ...request, model });
      log.push({
        requestIndex: index,
        role,
        providerClass: model.constructor.name,
        modelId: (model as { model?: string }).model,
        executed: true,
      });
      return result;
    },
  });

  return { middleware, log };
}

/**
 * Default resolver for THIS spike's two roles — 'main' -> ChatAnthropic,
 * 'sub' -> ChatOllama. Constructs a NEW instance per call. A7 will likely
 * bring its own resolver covering more providers, so this is documented as
 * an example, not baked into createProviderRoutingMiddleware itself.
 */
export function defaultMainSubResolver(): ProviderResolver {
  return (role, request) => {
    if (role === 'main') {
      const apiKey = process.env.ANTHROPIC_API_KEY ?? ANTHROPIC_PLACEHOLDER_KEY;
      return new ChatAnthropic({ model: CLAUDE_MODEL_ID, apiKey });
    }
    if (role === 'sub') {
      return new ChatOllama({
        model: OLLAMA_SUB_MODEL_ID,
        baseUrl: OLLAMA_BASE_URL,
      });
    }
    return request.model; // passthrough for unrecognized roles
  };
}

/**
 * Builds a stateful executeOverride that short-circuits ONLY the 'main'
 * role's execution, scripting a tool_calls-bearing AIMessage on its first
 * invocation (so the ReAct loop actually invokes delegate_to_subagent) and
 * a plain final AIMessage on every subsequent invocation. The 'sub' role
 * always returns undefined (no override — real handler, real Ollama call).
 */
export function makeScriptedMainStub(): ExecuteOverride {
  let mainCallCount = 0;
  return (role) => {
    if (role !== 'main') return undefined;
    mainCallCount += 1;
    if (mainCallCount === 1) {
      return new AIMessage({
        content: '',
        tool_calls: [
          {
            name: 'delegate_to_subagent',
            args: { task: 'What is your favorite fruit? Answer in one word.' },
            id: 'call_1',
            type: 'tool_call',
          },
        ],
      });
    }
    return new AIMessage({
      content: 'Delegated to the subagent and got an answer back.',
    });
  };
}

/**
 * Builds the delegate_to_subagent tool: its execute() nests a SECOND
 * createAgent + invoke(), sharing the caller-supplied middleware instance,
 * tagged { role: 'sub' }. This nesting IS "one logical session" (call-stack
 * continuity) — see the plan's design-question-1 resolution.
 */
export function makeDelegateToSubagentTool(
  sharedMiddleware: AgentMiddleware,
  placeholderModel: ModelRequest['model'],
) {
  return tool(
    async (args: { task: string }) => {
      const subAgent = createAgent({
        model: placeholderModel, // overridden by sharedMiddleware every call
        middleware: [sharedMiddleware],
      });
      const subResult = await subAgent.invoke(
        { messages: [{ role: 'user', content: args.task }] },
        { context: { role: 'sub' } },
      );
      const last = subResult.messages.at(-1);
      return JSON.stringify({ subagent_answer: last?.content ?? '' });
    },
    {
      name: 'delegate_to_subagent',
      description: 'Delegates a sub-task to a local subagent model.',
      schema: {
        type: 'object',
        properties: { task: { type: 'string' } },
        required: ['task'],
        additionalProperties: false,
      },
    },
  );
}

// ── Direct-execution smoke demo ─────────────────────────────────────────

async function runOneSessionDemo(): Promise<void> {
  const { middleware: sharedMiddleware, log } = createProviderRoutingMiddleware(
    {
      resolveModel: defaultMainSubResolver(),
      executeOverride: makeScriptedMainStub(),
    },
  );
  const placeholderModel = new ChatOllama({
    model: OLLAMA_SUB_MODEL_ID,
    baseUrl: OLLAMA_BASE_URL,
  });
  const delegateTool = makeDelegateToSubagentTool(
    sharedMiddleware,
    placeholderModel,
  );

  const mainAgent = createAgent({
    model: placeholderModel, // overridden by sharedMiddleware every call
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

  console.log('\n── Transcript ──');
  for (const message of result.messages) {
    const name = (message as { name?: string }).name;
    console.log(
      `[${message.getType()}${name ? `:${name}` : ''}] ${String(message.content)}`,
    );
  }

  console.log('\n── Routing log ──');
  for (const record of log) {
    console.log(
      `#${record.requestIndex} role=${record.role} provider=${record.providerClass} model=${record.modelId} executed=${record.executed}`,
    );
  }
}

async function main(): Promise<void> {
  await runOneSessionDemo();
}

const invokedDirectly =
  process.argv[1] !== undefined &&
  fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (invokedDirectly) {
  main().catch((err) => {
    console.error('A3 spike failed:', err instanceof Error ? err.message : err);
    process.exit(1);
  });
}
