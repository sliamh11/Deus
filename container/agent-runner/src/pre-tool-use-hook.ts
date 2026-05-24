/**
 * PreToolUse hook — dispatches PreToolUseHookInput to HookDispatchService
 * before each tool call executes.
 *
 * Blocking: if the dispatch service responds { decision: 'block' } the SDK
 * is instructed to block the tool call.
 *
 * Failure mode: unreachable / timeout → returns {} (silent degradation,
 * same pattern as memory-retrieval-hook.ts BRIDGE_TIMEOUT_MS / line 12).
 * Timeout: 4000 ms.
 */

import type {
  HookCallback,
  PreToolUseHookInput,
} from '@anthropic-ai/claude-agent-sdk';

const DISPATCH_TIMEOUT_MS = 4000;

export function createPreToolUseHook(
  host: string = process.env.DEUS_PROXY_HOST ?? 'host.docker.internal',
  port: number = parseInt(process.env.HOOK_DISPATCH_PORT ?? '3002', 10),
  token: string | undefined = process.env.DEUS_PROXY_TOKEN,
): HookCallback {
  return async (input): Promise<Record<string, unknown>> => {
    const hookInput = input as PreToolUseHookInput;
    const url = `http://${host}:${port}/hooks/PreToolUse`;

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), DISPATCH_TIMEOUT_MS);

    try {
      const headers: Record<string, string> = {
        'content-type': 'application/json',
      };
      if (token) headers['x-deus-proxy-token'] = token;

      const res = await fetch(url, {
        method: 'POST',
        headers,
        body: JSON.stringify({
          hook_event_name: hookInput.hook_event_name,
          tool_name: hookInput.tool_name,
          tool_input: hookInput.tool_input,
          tool_use_id: hookInput.tool_use_id,
          session_id: hookInput.session_id,
        }),
        signal: controller.signal,
      });

      if (!res.ok) return {};

      const data = (await res.json()) as Record<string, unknown>;

      // Forward block decision back to the SDK
      if (data.decision === 'block') {
        return {
          decision: 'block' as const,
          reason:
            typeof data.reason === 'string'
              ? data.reason
              : 'Blocked by PreToolUse observer',
        };
      }

      return data;
    } catch {
      console.warn(
        '[pre-tool-use-hook] HookDispatchService unreachable, skipping',
      );
      return {};
    } finally {
      clearTimeout(timer);
    }
  };
}
