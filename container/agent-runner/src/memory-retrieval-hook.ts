/**
 * Memory retrieval hook for container agents.
 *
 * Fetches memory context from the host's memory bridge (POST /memory/query on
 * the credential proxy). Used by both Claude (UserPromptSubmit additionalContext)
 * and OpenAI (system prompt prepend) backends.
 *
 * Failure mode: bridge unreachable / timeout / error → returns empty object or
 * empty string. Silent degradation, never crashes the agent.
 */

const BRIDGE_TIMEOUT_MS = 4000;

interface MemoryBridgeResponse {
  context: string;
  paths: string[];
  confidence: number;
  fell_back: boolean;
}

function getBridgeUrl(): string | null {
  const proxyPort = process.env.CREDENTIAL_PROXY_PORT || '3001';
  const proxyHost = process.env.DEUS_PROXY_HOST || 'host.docker.internal';
  if (!process.env.DEUS_PROXY_TOKEN) return null;
  return `http://${proxyHost}:${proxyPort}/memory/query`;
}

export async function fetchMemoryContext(
  query: string,
  source: string = 'container',
): Promise<string> {
  const url = getBridgeUrl();
  if (!url) return '';

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), BRIDGE_TIMEOUT_MS);

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        'x-deus-proxy-token': process.env.DEUS_PROXY_TOKEN!,
        'x-deus-source': source,
      },
      body: JSON.stringify({ query, source }),
      signal: controller.signal,
    });

    if (!res.ok) return '';

    const data = (await res.json()) as MemoryBridgeResponse;
    return data.context || '';
  } catch {
    return '';
  } finally {
    clearTimeout(timer);
  }
}

export function createMemoryRetrievalHook() {
  return async (input: {
    prompt?: string;
  }): Promise<Record<string, unknown>> => {
    const prompt = input.prompt;
    if (!prompt) return {};

    const context = await fetchMemoryContext(prompt, 'container-claude');
    if (!context) return {};

    return {
      hookSpecificOutput: {
        hookEventName: 'UserPromptSubmit',
        additionalContext: context,
      },
    };
  };
}
