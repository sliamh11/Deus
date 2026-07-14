/**
 * Spike (LIA-394 / A1): does LangChain JS's `createAgent` actually drive a
 * multi-turn tool-calling loop through Deus's existing
 * `container/agent-runner/src/tool-broker.ts` tool definitions and execution
 * logic, UNMODIFIED?
 *
 * This is a host-side-only spike. It deliberately runs outside the scope
 * declared by docs/decisions/backend-neutral-agent-runtime.md (which covers
 * src/agent-runtimes/, container/agent-runner/, deus-cmd.sh/ps1, AGENTS.md,
 * AI_AGENT_GUIDELINES.md) using a personal ANTHROPIC_API_KEY rather than the
 * credential-proxy route. See the paired write-up
 * (lia394_langchain_walking_skeleton.md) for the full question/method/verdict
 * and the relationship to prior ADRs.
 *
 * Only web_search and web_fetch are exposed here — the only two tool-broker
 * cases that bypass resolveWorkspacePath entirely and don't spawn shell
 * commands. See the write-up for the full rationale.
 *
 * Cross-platform: uses only standard Node.js `URL`, LangChain's HTTP-based
 * model client, and no shell-outs or OS-specific paths.
 */

import path from 'path';
import { fileURLToPath } from 'url';

import { ChatAnthropic } from '@langchain/anthropic';
import { tool, type StructuredTool } from '@langchain/core/tools';
import { createAgent } from 'langchain';

import {
  executeBrokerTool,
  getOpenAIToolDefinitions,
  type ToolBrokerContext,
} from '../../container/agent-runner/src/tool-broker.js';

const MODEL_ID = 'claude-opus-4-8';
const ALLOWED_WEB_FETCH_HOSTS = ['npmjs.com', 'www.npmjs.com'];
const SPIKE_PROMPT =
  'Search the web for the current LangChain JS version on npm, then fetch ' +
  'the npmjs.com package page and tell me the license.';

/**
 * Adapter: maps every tool-broker.ts tool definition to a LangChain
 * StructuredTool. Pure adapter — no tool behavior is redefined, no logic is
 * duplicated. Each tool's execute function calls executeBrokerTool()
 * unchanged; the JSON-schema `parameters` from getOpenAIToolDefinitions()
 * are passed directly to tool()'s `schema` field (LangChain's tool() accepts
 * raw JSON Schema 7 directly — no Zod conversion needed).
 */
export function toolBrokerToLangChainTools(
  ctx: ToolBrokerContext,
): StructuredTool[] {
  return getOpenAIToolDefinitions().map((definition) =>
    tool(
      async (args: Record<string, unknown>) => {
        const result = await executeBrokerTool(definition.name, args, ctx);
        return JSON.stringify(result);
      },
      {
        name: definition.name,
        description: definition.description,
        // definition.parameters is typed as the broad
        // OpenAIFunctionToolDefinition['parameters'] = Record<string, unknown>
        // (tool-broker.ts doesn't narrow it further), but is always built by
        // the schema() helper into a real JSON-Schema-7 object shape. tool()'s
        // JsonSchema7Type overload needs that narrower shape at the type
        // level; the runtime value already satisfies it.
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        schema: definition.parameters as any,
      },
    ),
  );
}

/**
 * Decorator: wraps a single-URL-argument tool with a code-level host
 * allowlist enforced BEFORE the wrapped tool's real execute function runs.
 * Preserves the wrapped tool's name/description/schema unchanged; only
 * `.invoke()` is intercepted. On a disallowed or malformed URL, returns a
 * structured tool-error result to the model instead of executing the fetch
 * — never throws an unhandled exception, and never delegates to the
 * wrapped tool on the reject path (so the wrapped tool's real execute
 * function — and therefore executeBrokerTool — is provably never reached
 * for a disallowed host).
 */
export function withHostAllowlist(
  wrapped: StructuredTool,
  allowedHosts: string[],
): StructuredTool {
  return tool(
    async (args: Record<string, unknown>) => {
      const rawUrl = typeof args.url === 'string' ? args.url : '';
      let parsed: URL;
      try {
        parsed = new URL(rawUrl);
      } catch (err) {
        return JSON.stringify({
          ok: false,
          error: `host-allowlist: malformed URL "${rawUrl}": ${
            err instanceof Error ? err.message : String(err)
          }`,
        });
      }
      if (!allowedHosts.includes(parsed.hostname)) {
        return JSON.stringify({
          ok: false,
          error: `host-allowlist: hostname "${parsed.hostname}" is not in the allowed list [${allowedHosts.join(', ')}]`,
        });
      }
      const result = await wrapped.invoke(args);
      return typeof result === 'string' ? result : JSON.stringify(result);
    },
    {
      name: wrapped.name,
      description: wrapped.description,
      // wrapped.schema's static type is StructuredTool's generic SchemaT
      // (unconstrained by this function's signature), but at runtime it is
      // the same JSON-Schema-7 object every tool in this file is built from
      // — re-declaring it here (unchanged) so the Decorator's own schema
      // matches the tool it wraps exactly.
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      schema: wrapped.schema as any,
    },
  );
}

function buildToolBrokerContext(): ToolBrokerContext {
  // web_search and web_fetch never touch ctx.containerInput (confirmed by
  // reading executeBrokerTool's web_search/web_fetch case bodies), so a
  // minimal stub is sufficient for this spike's scope.
  return {
    cwd: process.cwd(),
    containerInput: { groupFolder: 'lia394-spike', chatJid: 'lia394-spike' },
  };
}

function buildSpikeTools(): StructuredTool[] {
  const ctx = buildToolBrokerContext();
  return toolBrokerToLangChainTools(ctx)
    .filter((t) => ['web_search', 'web_fetch'].includes(t.name))
    .map((t) =>
      t.name === 'web_fetch'
        ? withHostAllowlist(t, ALLOWED_WEB_FETCH_HOSTS)
        : t,
    );
}

interface TranscriptToolCall {
  name?: string;
  args?: unknown;
  id?: string;
}

function printTranscriptMessage(message: unknown, index: number): void {
  const m = message as {
    _getType?: () => string;
    getType?: () => string;
    content?: unknown;
    tool_calls?: TranscriptToolCall[];
    name?: string;
  };
  const type =
    (typeof m.getType === 'function' && m.getType()) ||
    (typeof m._getType === 'function' && m._getType()) ||
    'unknown';
  console.log(`\n--- [${index}] ${type} ---`);
  if (m.name) console.log(`name: ${m.name}`);
  if (m.content !== undefined) {
    const content =
      typeof m.content === 'string' ? m.content : JSON.stringify(m.content);
    console.log(`content: ${content}`);
  }
  if (Array.isArray(m.tool_calls) && m.tool_calls.length > 0) {
    for (const call of m.tool_calls) {
      console.log(
        `tool_call: ${call.name}(${JSON.stringify(call.args)}) [id=${call.id}]`,
      );
    }
  }
}

async function main(): Promise<void> {
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    console.log(
      'A1 spike requires ANTHROPIC_API_KEY — set it in .env or your shell ' +
        'to run the live model-call proof. Exiting without attempting a call.',
    );
    process.exit(3);
  }

  const tools = buildSpikeTools();
  console.log(
    `Built ${tools.length} tool(s): ${tools.map((t) => t.name).join(', ')}`,
  );

  const agent = createAgent({
    model: new ChatAnthropic({ apiKey, model: MODEL_ID }),
    tools,
  });

  console.log(`\nInvoking agent with prompt:\n"${SPIKE_PROMPT}"\n`);
  const result = await agent.invoke({
    messages: [{ role: 'user', content: SPIKE_PROMPT }],
  });

  console.log('\n=== Full transcript ===');
  result.messages.forEach((message, index) =>
    printTranscriptMessage(message, index),
  );

  const last = result.messages[result.messages.length - 1] as {
    content?: unknown;
  };
  console.log('\n=== Final answer ===');
  console.log(
    typeof last?.content === 'string'
      ? last.content
      : JSON.stringify(last?.content),
  );
}

// Only run when executed directly (not when imported by the unit tests).
// Compare resolved filesystem paths, not raw strings: on Windows,
// import.meta.url is a forward-slash file:// URI (file:///C:/...) while
// process.argv[1] is a backslash path (C:\...) — a raw string/URL
// comparison never matches there, silently turning main() into a no-op.
const invokedDirectly =
  process.argv[1] !== undefined &&
  fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (invokedDirectly) {
  main().catch((err) => {
    console.error('A1 spike failed:', err instanceof Error ? err.message : err);
    process.exit(1);
  });
}
