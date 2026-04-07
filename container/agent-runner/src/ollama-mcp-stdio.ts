/**
 * Ollama stdio MCP Server for Deus
 * Exposes local Ollama models as tools for the container agent.
 * Claude remains the orchestrator; Ollama handles offloaded inference.
 *
 * Environment:
 *   OLLAMA_HOST — Ollama API base URL (default: http://host.docker.internal:11434)
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { z } from 'zod';

const OLLAMA_FALLBACKS = [
  'http://host.docker.internal:11434',
  'http://localhost:11434',
];

function getOllamaHost(): string {
  return process.env.OLLAMA_HOST || OLLAMA_FALLBACKS[0];
}

function log(message: string): void {
  process.stderr.write(`[OLLAMA] ${message}\n`);
}

async function ollamaFetch(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const host = getOllamaHost();
  const url = `${host}${path}`;
  try {
    return await fetch(url, init);
  } catch {
    // Try fallback hosts if the primary fails
    for (const fallback of OLLAMA_FALLBACKS.slice(1)) {
      if (fallback === host) continue;
      try {
        return await fetch(`${fallback}${path}`, init);
      } catch {
        continue;
      }
    }
    throw new Error(
      `Failed to connect to Ollama at ${host}. Is Ollama running?`,
    );
  }
}

const server = new McpServer({
  name: 'ollama',
  version: '1.0.0',
});

server.tool(
  'ollama_list_models',
  'List all locally installed Ollama models. Call this first to know which models are available before generating.',
  {},
  async () => {
    try {
      const res = await ollamaFetch('/api/tags');
      if (!res.ok) {
        return {
          content: [
            {
              type: 'text' as const,
              text: `Ollama API error: ${res.status} ${res.statusText}`,
            },
          ],
          isError: true,
        };
      }
      const data = (await res.json()) as {
        models?: Array<{ name: string; size: number; modified_at: string }>;
      };
      const models = data.models || [];
      if (models.length === 0) {
        return {
          content: [
            {
              type: 'text' as const,
              text: 'No models installed. Pull one with: ollama pull llama3.2',
            },
          ],
        };
      }
      const lines = models.map((m) => {
        const gb = (m.size / 1e9).toFixed(1);
        return `- ${m.name} (${gb} GB)`;
      });
      return {
        content: [
          {
            type: 'text' as const,
            text: `Installed models:\n${lines.join('\n')}`,
          },
        ],
      };
    } catch (err) {
      return {
        content: [
          {
            type: 'text' as const,
            text: `Error: ${err instanceof Error ? err.message : String(err)}`,
          },
        ],
        isError: true,
      };
    }
  },
);

server.tool(
  'ollama_generate',
  `Send a prompt to a local Ollama model and return its response.
Use ollama_list_models first to see what's available.
Good uses: summarization, classification, translation, code generation, analysis tasks you want to offload from the main Claude context.
Tip: smaller models (e.g., gemma3:1b, llama3.2) are fast; larger models (gemma4:e4b, llama3.3) are higher quality.`,
  {
    model: z
      .string()
      .describe('Model name (e.g., "llama3.2", "gemma4:e4b", "qwen3.5:4b")'),
    prompt: z.string().describe('The prompt to send to the model'),
    system: z
      .string()
      .optional()
      .describe('Optional system prompt to set context or behavior'),
    temperature: z
      .number()
      .min(0)
      .max(2)
      .optional()
      .describe(
        'Sampling temperature 0–2 (default: 0.7). Lower = more deterministic.',
      ),
  },
  async (args) => {
    log(
      `>>> Generating with ${args.model} (prompt: ${args.prompt.length} chars)`,
    );
    try {
      const body: Record<string, unknown> = {
        model: args.model,
        prompt: args.prompt,
        stream: false,
        options: {} as Record<string, unknown>,
      };
      if (args.system) body.system = args.system;
      if (args.temperature !== undefined) {
        (body.options as Record<string, unknown>).temperature =
          args.temperature;
      }

      const res = await ollamaFetch('/api/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      if (!res.ok) {
        const errText = await res.text();
        return {
          content: [
            {
              type: 'text' as const,
              text: `Ollama error (${res.status}): ${errText}`,
            },
          ],
          isError: true,
        };
      }

      const data = (await res.json()) as { response?: string; error?: string };
      if (data.error) {
        return {
          content: [
            { type: 'text' as const, text: `Model error: ${data.error}` },
          ],
          isError: true,
        };
      }

      const response = data.response || '';
      log(`<<< Done (${response.length} chars)`);
      return { content: [{ type: 'text' as const, text: response }] };
    } catch (err) {
      return {
        content: [
          {
            type: 'text' as const,
            text: `Error: ${err instanceof Error ? err.message : String(err)}`,
          },
        ],
        isError: true,
      };
    }
  },
);

const transport = new StdioServerTransport();
await server.connect(transport);
