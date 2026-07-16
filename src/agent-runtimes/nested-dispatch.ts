/**
 * Nested subagent dispatch primitive for the `deus-native` runtime
 * (LIA-408 / B8).
 *
 * `createNestedDispatcher()` is the Deus-owned, in-process replacement for
 * Claude Code's Agent tool inside a single `deus-native` `runTurn()`. It
 * constructs one fresh child `createAgent` per dispatch, invokes it with an
 * explicit, isolated prompt, and independently validates the terminal child
 * output against a caller-supplied Zod-compatible output contract before it
 * ever reaches the parent. This module is deliberately NOT
 * `src/multi-agent/orchestrator.ts`: that module topologically schedules
 * `SubagentTask[]` across independent container/runtime sessions and parses
 * `[STATUS:...]` text markers; this module creates one child LangChain agent
 * inside the current in-process turn and validates its output against a
 * schema. Both primitives coexist — see
 * docs/decisions/deus-v2-subagent-dispatch.md.
 *
 * Test seam: `createNestedDispatcher({ resolveModel })` is the ONLY required
 * dependency — this is the independently authored oracle's minimal
 * construction (`nested-dispatch.oracle.test.ts`, `ORACLE_BRIEF.md`). Every
 * other dependency below is optional and defaults to the real production
 * behavior, so production code never has to pass anything beyond
 * `resolveModel` plus (in practice) the production-only extras it actually
 * needs.
 */

import { createAgent, type AgentMiddleware } from 'langchain';
import type { BaseChatModel } from '@langchain/core/language_models/chat_models';
import type { StructuredTool } from '@langchain/core/tools';
import type { BaseMessage } from '@langchain/core/messages';

/**
 * A Zod-compatible output contract. Only the one operation the dispatcher
 * actually needs is required — `safeParseAsync` — so a real Zod schema (from
 * `z.object(...)` or the compiled `z.fromJSONSchema(...)` result the
 * production tool adapter builds) satisfies this directly, and a minimal
 * hand-rolled test double can too, without importing Zod's own types here.
 */
export interface OutputContract<T> {
  safeParseAsync(
    value: unknown,
  ): Promise<
    | { success: true; data: T }
    | { success: false; error: { message?: string; issues?: unknown } }
  >;
}

/** One nested-dispatch request. `outputContract` is mandatory — AC2. */
export interface NestedDispatchRequest<T> {
  /** Identifies the dispatched subagent for tracing (AC5). Not a schema
   *  name — purely a caller-chosen label surfaced back in `metadata`. */
  agentId: string;
  /** The child's model identifier, resolved independently of the parent's
   *  model via `resolveModel` (AC4). */
  model: string;
  /** The explicit, isolated task prompt sent to the child. The child never
   *  sees the parent's transcript. */
  prompt: string;
  /** Mandatory Zod-compatible contract every dispatch must declare (AC2). */
  outputContract: OutputContract<T>;
}

/** Trace metadata carried on every result path (AC5) — always present, even
 *  on failure, so a failed dispatch remains identifiable. */
export interface NestedDispatchMetadata {
  agentId: string;
  /** The requested model id, echoed back even when child construction
   *  fails before a real client's `.model` field could be read. */
  model: string;
  /** Present only when a production dependency supplies it (e.g. the
   *  parent's session id for trace context). Never invented here. */
  parentSessionId?: string;
  /** Present only when a production dependency supplies real provider
   *  attribution. Never invented here. */
  provider?: string;
  /** Present only when the child's terminal AIMessage carried
   *  `usage_metadata`. Never a fabricated zero. */
  usage?: {
    inputTokens: number;
    outputTokens: number;
    totalTokens: number;
  };
}

export type NestedDispatchResult<T> =
  | {
      status: 'success';
      output: T;
      metadata: NestedDispatchMetadata;
    }
  | {
      status: 'contract_failure';
      error: {
        code: 'subagent_output_contract_failed';
        message: string;
        issues?: unknown;
      };
      metadata: NestedDispatchMetadata;
    }
  | {
      status: 'error';
      error: { code: 'subagent_execution_failed'; message: string };
      metadata: NestedDispatchMetadata;
    };

/** One completed child AI message, reported to the optional usage observer
 *  below. Mirrors the shape `deus-native-usage.ts`'s collector consumes. */
export interface NestedDispatchUsageObservation {
  agentId: string;
  model: string;
  message: BaseMessage;
}

export interface CreateNestedDispatcherDeps {
  /**
   * Resolves a requested model id to a real chat-model client. The ONLY
   * required dependency — the oracle-defined seam. Called once per
   * `dispatch()` call, never cached across dispatches (AC4: independent
   * per-subagent model selection, not inherited from the parent or from an
   * earlier sibling dispatch).
   */
  resolveModel: (model: string) => BaseChatModel;
  /**
   * Builds the child's tool list. Defaults to no tools (`[]`) — a caller
   * that wants children to have tools (production: a fresh, isolated
   * `buildSafeTools()` result per dispatch) supplies this explicitly. The
   * child NEVER automatically receives the parent's tool list, and never
   * receives `dispatch_nested_agent` itself (no recursive nesting from this
   * primitive alone).
   */
  buildChildTools?: () => Promise<StructuredTool[]> | StructuredTool[];
  /**
   * Builds the child's middleware stack. Defaults to none (`[]`) — a fresh
   * array/instances per call is the caller's responsibility (production:
   * `buildMiddlewareStack()` called again per dispatch, never a shared
   * reference).
   */
  buildChildMiddleware?: () => AgentMiddleware[];
  /** Trace context echoed into `metadata.parentSessionId` when supplied.
   *  Never a real session/thread id minted here — the child gets no
   *  checkpointer and no session of its own. */
  parentSessionId?: string;
  /** Trace context echoed into `metadata.provider` when supplied. */
  provider?: string;
  /** Optional per-child-message usage observer, called once per completed
   *  child AIMessage (mirrors the parent turn's own per-AIMessage usage
   *  loop in `deus-native-backend.ts`). Never called for a non-AI message.
   *  Awaited before `dispatch()` resolves, so a production observer that
   *  itself awaits an async `eventSink` (the shared `TurnUsageCollector`)
   *  is guaranteed to have recorded the event before the caller proceeds. */
  onUsage?: (
    observation: NestedDispatchUsageObservation,
  ) => void | Promise<void>;
}

export interface NestedDispatcher {
  dispatch<T>(
    request: NestedDispatchRequest<T>,
  ): Promise<NestedDispatchResult<T>>;
}

/** Narrow, defensive shape-check for a Zod-compatible contract — the
 *  dispatcher only ever calls `safeParseAsync`, so that is the entire
 *  runtime contract it enforces on dynamically malformed direct callers
 *  (the oracle's "omitting the output contract" case, which arrives via
 *  `as never` and is not caught by TypeScript at the call site). */
function isOutputContract(value: unknown): value is OutputContract<unknown> {
  return (
    typeof value === 'object' &&
    value !== null &&
    typeof (value as { safeParseAsync?: unknown }).safeParseAsync === 'function'
  );
}

function baseMetadata(
  request: { agentId?: unknown; model?: unknown },
  deps: CreateNestedDispatcherDeps,
): NestedDispatchMetadata {
  return {
    agentId: typeof request.agentId === 'string' ? request.agentId : '',
    model: typeof request.model === 'string' ? request.model : '',
    ...(deps.parentSessionId !== undefined
      ? { parentSessionId: deps.parentSessionId }
      : {}),
    ...(deps.provider !== undefined ? { provider: deps.provider } : {}),
  };
}

/** Extracts the terminal child AIMessage's content as a JS value, parsing
 *  JSON text when the content is a JSON string. Non-JSON string content is
 *  returned as-is — the contract's own `safeParseAsync` is the single
 *  source of truth for whether that shape is valid, matching the oracle's
 *  `z.preprocess` JSON-text convention (RESEARCH_OUTPUT_CONTRACT). */
function extractCandidateOutput(content: unknown): unknown {
  if (typeof content !== 'string') return content;
  try {
    return JSON.parse(content) as unknown;
  } catch {
    return content;
  }
}

/**
 * Reads usage_metadata off a message the same defensive way
 * `deus-native-backend.ts`'s own per-AIMessage loop does — via the `type`
 * discriminator, never `instanceof`, never duck-typed on usage presence
 * alone.
 */
function readUsage(
  message: BaseMessage,
): NestedDispatchMetadata['usage'] | undefined {
  if ((message as { type?: string }).type !== 'ai') return undefined;
  const usage = (
    message as {
      usage_metadata?: {
        input_tokens: number;
        output_tokens: number;
        total_tokens: number;
      };
    }
  ).usage_metadata;
  if (!usage) return undefined;
  return {
    inputTokens: usage.input_tokens,
    outputTokens: usage.output_tokens,
    totalTokens: usage.total_tokens,
  };
}

/**
 * Builds the Deus-owned nested dispatch primitive (AC1). Every `dispatch()`
 * call:
 * 1. Validates the request shape (`agentId`, `model`, `prompt`,
 *    `outputContract`) BEFORE calling `resolveModel` — a dynamically
 *    malformed direct caller (missing contract) fails closed before any
 *    model resolution or child construction (matches the oracle's
 *    "omitting the output contract fails before any child model is
 *    selected" case).
 * 2. Resolves the model fresh, every call — never cached (AC4).
 * 3. Builds fresh child tools/middleware (defaulting to none) and
 *    constructs exactly one child via `createAgent({ model, tools,
 *    middleware })`, with NO checkpointer — children are one-shot and
 *    create no persistent session state.
 * 4. Invokes the child with ONLY the explicit `prompt` — never the parent's
 *    transcript.
 * 5. Extracts the terminal AI message, parses JSON text when present, and
 *    independently validates it via the contract's `safeParseAsync` (AC2,
 *    AC3). Only the parsed value is ever returned as `output` — invalid raw
 *    output is never echoed back.
 * 6. Reports every completed AIMessage to `onUsage` when supplied, and
 *    attaches the child's own usage (if reported) to `metadata.usage`.
 *
 * Every result path — success, contract failure, execution failure —
 * carries `metadata.agentId`/`metadata.model`, so a failed dispatch remains
 * traceable (AC5).
 */
export function createNestedDispatcher(
  deps: CreateNestedDispatcherDeps,
): NestedDispatcher {
  return {
    async dispatch<T>(
      request: NestedDispatchRequest<T>,
    ): Promise<NestedDispatchResult<T>> {
      const metadata = baseMetadata(
        request as { agentId?: unknown; model?: unknown },
        deps,
      );

      // Step 1: validate the request shape BEFORE resolveModel. Real
      // TypeScript callers cannot construct a request missing
      // outputContract, but the oracle's own "omitting the output
      // contract" case calls dispatch() with an `as never` cast to exercise
      // exactly this dynamic-caller failure path.
      if (
        typeof request?.agentId !== 'string' ||
        request.agentId.length === 0
      ) {
        return {
          status: 'contract_failure',
          error: {
            code: 'subagent_output_contract_failed',
            message: 'nested dispatch request is missing a non-empty "agentId"',
          },
          metadata,
        };
      }
      if (typeof request.model !== 'string' || request.model.length === 0) {
        return {
          status: 'contract_failure',
          error: {
            code: 'subagent_output_contract_failed',
            message: 'nested dispatch request is missing a non-empty "model"',
          },
          metadata,
        };
      }
      if (typeof request.prompt !== 'string' || request.prompt.length === 0) {
        return {
          status: 'contract_failure',
          error: {
            code: 'subagent_output_contract_failed',
            message: 'nested dispatch request is missing a non-empty "prompt"',
          },
          metadata,
        };
      }
      if (!isOutputContract(request.outputContract)) {
        return {
          status: 'contract_failure',
          error: {
            code: 'subagent_output_contract_failed',
            message:
              'nested dispatch request is missing a Zod-compatible ' +
              '"outputContract" (expected a safeParseAsync method)',
          },
          metadata,
        };
      }

      let model: BaseChatModel;
      let result: Awaited<ReturnType<ReturnType<typeof createAgent>['invoke']>>;
      try {
        // Step 2: fresh model resolution, every call.
        model = deps.resolveModel(request.model);

        // Step 3: fresh child dependencies + one nested createAgent.
        const tools = (await deps.buildChildTools?.()) ?? [];
        const middleware = deps.buildChildMiddleware?.() ?? [];
        const child = createAgent({ model, tools, middleware });

        // Step 4: only the explicit child prompt — never the parent's
        // transcript, never a shared checkpointer/session.
        result = await child.invoke({
          messages: [{ role: 'user', content: request.prompt }],
        });
      } catch (err) {
        const message = err instanceof Error ? err.message : String(err);
        return {
          status: 'error',
          error: { code: 'subagent_execution_failed', message },
          metadata,
        };
      }

      // Reflect the actually-constructed client's model id when readable,
      // otherwise retain the requested id (already in `metadata.model`).
      const resolvedModelId = (model as unknown as { model?: string }).model;
      const metadataWithResolvedModel: NestedDispatchMetadata = {
        ...metadata,
        ...(typeof resolvedModelId === 'string' && resolvedModelId.length > 0
          ? { model: resolvedModelId }
          : {}),
      };

      const messages = (result?.messages ?? []) as BaseMessage[];
      for (const message of messages) {
        if ((message as { type?: string }).type !== 'ai') continue;
        await deps.onUsage?.({
          agentId: request.agentId,
          model: metadataWithResolvedModel.model,
          message,
        });
      }

      const last = messages[messages.length - 1] as
        { content?: unknown } | undefined;
      const usage = last ? readUsage(last as BaseMessage) : undefined;
      const metadataWithUsage: NestedDispatchMetadata = {
        ...metadataWithResolvedModel,
        ...(usage !== undefined ? { usage } : {}),
      };

      if (last === undefined || last.content === undefined) {
        return {
          status: 'contract_failure',
          error: {
            code: 'subagent_output_contract_failed',
            message:
              'nested dispatch child produced no terminal output message',
          },
          metadata: metadataWithUsage,
        };
      }

      // Step 5: parse JSON text (when present) and validate independently.
      const candidate = extractCandidateOutput(last.content);
      const parsed = await request.outputContract.safeParseAsync(candidate);
      if (!parsed.success) {
        return {
          status: 'contract_failure',
          error: {
            code: 'subagent_output_contract_failed',
            message:
              parsed.error.message ??
              'nested dispatch child output failed contract validation',
            ...(parsed.error.issues !== undefined
              ? { issues: parsed.error.issues }
              : {}),
          },
          metadata: metadataWithUsage,
        };
      }

      return {
        status: 'success',
        output: parsed.data as T,
        metadata: metadataWithUsage,
      };
    },
  };
}
