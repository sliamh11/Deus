/**
 * Blocking PreToolUse observer — the first observer that can DENY a tool call
 * via `HookDispatchService`. Returning `{ decision: 'block', reason }` is honored
 * by both consult consumers (`dispatchPreToolUseGate` for the openai/llama-cpp
 * loops, `createPreToolUseHook` for the Claude SDK), so one observer gates every
 * backend. Deny rule is minimal + conservative (fail-open): block a Bash
 * recursive-force `rm` whose absolute target escapes `/workspace`; anything not
 * positively classified as out-of-`/workspace` is allowed, malformed payload → {}.
 */

import type { ObserverCallback } from './hook-dispatch-service.js';

/**
 * True when `command` is a recursive-force `rm` with at least one ABSOLUTE
 * target outside `/workspace`. Conservative by construction — unparseable or
 * relative targets degrade to `false` (allow), never to a false block.
 */
export function isRecursiveForceRmOutsideWorkspace(command: string): boolean {
  // Whitespace tokenization is deliberately simple: we do not parse quotes,
  // pipes, or subshells. Those degrade to "allow", never to a false block.
  const tokens = command.split(/\s+/).filter(Boolean);
  // First bare `rm` (or an absolute-path `rm`, e.g. /bin/rm). A stray `rm` token
  // inside e.g. an `echo` over-matches, but that only ever over-blocks a benign
  // command — acceptable for a default-off defense-in-depth guard.
  const rmIdx = tokens.findIndex((t) => t === 'rm' || t.endsWith('/rm'));
  if (rmIdx === -1) return false;

  let recursive = false;
  let force = false;
  const targets: string[] = [];

  for (const arg of tokens.slice(rmIdx + 1)) {
    if (arg === '--recursive') recursive = true;
    else if (arg === '--force') force = true;
    else if (/^-[a-zA-Z]+$/.test(arg)) {
      // Short flag cluster, e.g. -rf, -fr, -Rf, -r, -f.
      if (/[rR]/.test(arg)) recursive = true;
      if (/f/.test(arg)) force = true;
    } else if (!arg.startsWith('-')) {
      targets.push(arg);
    }
  }

  if (!recursive || !force) return false;

  // Block only ABSOLUTE targets that escape /workspace. Relative paths are
  // treated as in-workspace (the container cwd) and allowed — conservative.
  return targets.some((t) => t.startsWith('/') && !isInsideWorkspace(t));
}

function isInsideWorkspace(absPath: string): boolean {
  return absPath === '/workspace' || absPath.startsWith('/workspace/');
}

/**
 * Factory for the blocking PreToolUse observer. Matches the
 * `ObserverCallback = (event, payload) => Promise<Record<string, unknown>>`
 * contract from `hook-dispatch-service.ts`. Reads `payload.tool_name` and
 * `payload.tool_input.command` — the exact shape posted by both consult paths.
 */
export function createBlockingPreToolUseObserver(): ObserverCallback {
  return async (
    _event: string,
    payload: unknown,
  ): Promise<Record<string, unknown>> => {
    if (!payload || typeof payload !== 'object') return {};
    const p = payload as Record<string, unknown>;
    if (p.tool_name !== 'Bash') return {};

    const toolInput =
      p.tool_input && typeof p.tool_input === 'object'
        ? (p.tool_input as Record<string, unknown>)
        : {};
    const command =
      typeof toolInput.command === 'string' ? toolInput.command : '';

    if (isRecursiveForceRmOutsideWorkspace(command)) {
      return {
        decision: 'block',
        reason:
          'Blocked by PreToolUse gate: recursive-force `rm` outside /workspace is not permitted.',
      };
    }

    return {};
  };
}
