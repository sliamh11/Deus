/**
 * PostToolUse observer hook — fire-and-forget dispatch to HookDispatchService.
 *
 * Non-blocking: the fetch is initiated but NOT awaited, so agent turn latency
 * is not increased (target budget: < 20 ms, matching tool-sizes.jsonl baseline).
 *
 * Failure mode: any error → single console.warn, return {} immediately.
 * Mirrors the non-blocking logging pattern in src/index.ts lines 343-372.
 */

import type {
  HookCallback,
  PostToolUseHookInput,
} from '@anthropic-ai/claude-agent-sdk';

export function createPostToolUseObserverHook(
  host: string = process.env.DEUS_PROXY_HOST ?? 'host.docker.internal',
  port: number = parseInt(process.env.HOOK_DISPATCH_PORT ?? '3002', 10),
  token: string | undefined = process.env.DEUS_PROXY_TOKEN,
): HookCallback {
  return async (input): Promise<Record<string, unknown>> => {
    const hookInput = input as PostToolUseHookInput;
    const url = `http://${host}:${port}/hooks/PostToolUse`;

    const headers: Record<string, string> = {
      'content-type': 'application/json',
    };
    if (token) headers['x-deus-proxy-token'] = token;

    // Fire-and-forget: intentionally not awaited so the agent turn is not blocked.
    // The promise is assigned to void to suppress floating-promise lint warnings.
    void fetch(url, {
      method: 'POST',
      headers,
      body: JSON.stringify({
        hook_event_name: hookInput.hook_event_name,
        tool_name: hookInput.tool_name,
        tool_input: hookInput.tool_input,
        tool_response: hookInput.tool_response,
        tool_use_id: hookInput.tool_use_id,
        session_id: hookInput.session_id,
      }),
    }).catch((err: unknown) => {
      console.warn(
        '[post-tool-use-observer] dispatch failed:',
        err instanceof Error ? err.message : String(err),
      );
    });

    return {};
  };
}
