/**
 * PreToolUse gate — consulted before each tool call executes.
 *
 * `dispatchPreToolUseGate` is the backend-neutral core: it POSTs the tool call
 * to the HookDispatchService and honors a `{ decision: 'block' }` response. It
 * is shared by the Claude SDK adapter (`createPreToolUseHook`) AND the
 * handwritten OpenAI / llama-cpp tool loops, so all three backends enforce with
 * one identical posture.
 *
 * Enable gate: returns `{ block: false }` with NO fetch unless
 * `HOOK_DISPATCH_ENABLED === 'true'` — mirroring how the Claude SDK only wires
 * the PreToolUse hook when enabled (index.ts). Default off ⇒ zero behavior
 * change.
 *
 * Failure mode: unreachable / non-OK / timeout → `{ block: false }` (fail-open,
 * silent degradation — same pattern as memory-retrieval-hook.ts). The gate is
 * defense-in-depth for an already-authenticated turn, not the sole control; an
 * unreachable gate must not brick the turn.
 * Timeout: 4000 ms.
 */

import type {
  HookCallback,
  PreToolUseHookInput,
} from '@anthropic-ai/claude-agent-sdk';

const DISPATCH_TIMEOUT_MS = 4000;

/**
 * Host for the container-local HookDispatchService (:3002) consult: localhost,
 * NOT DEUS_PROXY_HOST (which addresses host-side services). Override: HOOK_DISPATCH_HOST.
 */
export function dispatchHost(): string {
  return process.env.HOOK_DISPATCH_HOST ?? '127.0.0.1';
}

export interface PreToolUseGateArgs {
  toolName: string;
  toolInput: unknown;
  toolUseId?: string;
  sessionId?: string;
  /** Defaults to `dispatchHost()` (HOOK_DISPATCH_HOST ?? '127.0.0.1'). */
  host?: string;
  /** Defaults to HOOK_DISPATCH_PORT ?? 3002. */
  port?: number;
  /** Defaults to DEUS_PROXY_TOKEN. */
  token?: string;
}

export interface PreToolUseGateResult {
  block: boolean;
  reason?: string;
  /**
   * The raw dispatch response on a non-block success (so the Claude SDK adapter
   * can forward `additionalContext` and any other SDK-relevant fields, exactly
   * as the pre-refactor hook did via `return data`). Undefined when disabled,
   * non-OK, or on error.
   */
  response?: Record<string, unknown>;
}

/**
 * Backend-neutral PreToolUse gate. See file header for the enable/failure
 * contract. Returns `{ block: true, reason }` to refuse the tool call.
 */
export async function dispatchPreToolUseGate(
  args: PreToolUseGateArgs,
): Promise<PreToolUseGateResult> {
  if (process.env.HOOK_DISPATCH_ENABLED !== 'true') return { block: false };

  const host = args.host ?? dispatchHost();
  const port =
    args.port ?? parseInt(process.env.HOOK_DISPATCH_PORT ?? '3002', 10);
  const token = args.token ?? process.env.DEUS_PROXY_TOKEN;
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
        // Hardcoded — this gate only ever serves PreToolUse. Keeps the body
        // byte-identical to the Claude SDK hook's so an observer reading
        // payload.hook_event_name sees the same value on every backend.
        hook_event_name: 'PreToolUse',
        tool_name: args.toolName,
        tool_input: args.toolInput,
        tool_use_id: args.toolUseId,
        session_id: args.sessionId,
      }),
      signal: controller.signal,
    });

    if (!res.ok) return { block: false };

    const data = (await res.json()) as Record<string, unknown>;

    if (data.decision === 'block') {
      return {
        block: true,
        reason:
          typeof data.reason === 'string'
            ? data.reason
            : 'Blocked by PreToolUse observer',
      };
    }

    return { block: false, response: data };
  } catch {
    console.warn(
      '[pre-tool-use-hook] HookDispatchService unreachable, skipping',
    );
    return { block: false };
  } finally {
    clearTimeout(timer);
  }
}

/**
 * Claude SDK adapter over `dispatchPreToolUseGate`. Behavior is identical to the
 * pre-refactor hook: `{ decision: 'block', reason }` on a block, the raw
 * dispatch response forwarded on a non-block success, `{}` otherwise.
 */
export function createPreToolUseHook(
  host: string = dispatchHost(),
  port: number = parseInt(process.env.HOOK_DISPATCH_PORT ?? '3002', 10),
  token: string | undefined = process.env.DEUS_PROXY_TOKEN,
): HookCallback {
  return async (input): Promise<Record<string, unknown>> => {
    const hookInput = input as PreToolUseHookInput;
    const gate = await dispatchPreToolUseGate({
      toolName: hookInput.tool_name,
      toolInput: hookInput.tool_input,
      toolUseId: hookInput.tool_use_id,
      sessionId: hookInput.session_id,
      host,
      port,
      token,
    });

    if (gate.block) {
      return {
        decision: 'block' as const,
        reason: gate.reason ?? 'Blocked by PreToolUse observer',
      };
    }

    return gate.response ?? {};
  };
}
