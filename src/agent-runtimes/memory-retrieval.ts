/**
 * Subprocess adapter for the personal-vault memory retrieval hook
 * (LIA-415 / D1).
 *
 * A narrow Adapter over the UNCHANGED `scripts/memory_retrieval_hook.py`
 * protocol: `{prompt, session_id}` JSON on stdin, and (optionally)
 * `{hookSpecificOutput: {additionalContext}}` JSON on stdout. Every
 * retrieval decision — minimum prompt length, thresholds, scoring,
 * embedding, fallback, abstain, truncation, procedure-kind opt-in,
 * session-concept expansion, and injection dedup — stays in Python; this
 * module only launches the process and extracts the one verified output
 * field.
 *
 * Fail-open by contract: empty output, malformed output, a non-zero exit,
 * a timeout kill, or a spawn failure all resolve to `''` (never reject) —
 * a no-result retrieval must never fail the model turn (AC4). This mirrors
 * the hook's own executable guard, which catches exceptions and always
 * exits zero.
 *
 * Cross-platform per docs/decisions/platform-abstraction-layer.md: the
 * interpreter comes from `PYTHON_BIN`, the script path from `PROJECT_ROOT`,
 * and no OS detection happens here. The pre-existing retrieval library's
 * Linux/macOS-only limitation is unchanged — on an unsupported host the
 * subprocess failure resolves to no context and the turn proceeds.
 */

import { spawn } from 'child_process';
import path from 'path';

import { PROJECT_ROOT } from '../config.js';
import { PYTHON_BIN } from '../platform.js';

/** Absolute path of the unchanged Python retrieval hook. */
export const MEMORY_RETRIEVAL_HOOK_PATH = path.join(
  PROJECT_ROOT,
  'scripts',
  'memory_retrieval_hook.py',
);

/**
 * Hard execution bound, matching the hook's existing Claude Code
 * registration (`.claude/settings.json` UserPromptSubmit `timeout: 5`
 * seconds) — the same budget the script is already tuned to live within.
 */
export const MEMORY_RETRIEVAL_TIMEOUT_MS = 5_000;

/** The hook's stdin contract: the submitted prompt plus the (possibly
 *  empty) backend-scoped session id driving session-concept expansion and
 *  injection dedup inside the script. */
export interface MemoryRetrievalRequest {
  prompt: string;
  sessionId: string;
}

/**
 * The middleware-facing retrieval function shape. `retrieveMemoryContext`
 * is the production implementation; tests inject hermetic doubles through
 * `BuildMiddlewareStackDeps.memoryRetrievalAdapter`.
 */
export type MemoryRetrievalAdapter = (
  request: MemoryRetrievalRequest,
) => Promise<string>;

/**
 * Extracts `hookSpecificOutput.additionalContext` from the hook's stdout,
 * normalizing every no-context shape (blank output — the hook prints
 * NOTHING on a short prompt or an abstained recall — malformed JSON, or a
 * missing/non-string field) to `''`.
 */
function extractAdditionalContext(stdout: string): string {
  if (stdout.trim() === '') return '';
  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    return '';
  }
  const context = (
    parsed as { hookSpecificOutput?: { additionalContext?: unknown } }
  )?.hookSpecificOutput?.additionalContext;
  return typeof context === 'string' ? context : '';
}

/**
 * Runs one retrieval attempt through the Python hook. Resolves the
 * recalled (already untrusted-framed) context string, or `''` on any
 * no-result or failure path. Never rejects.
 */
export const retrieveMemoryContext: MemoryRetrievalAdapter = (request) =>
  new Promise((resolve) => {
    // The child can emit 'error' AND 'close', and stdin can emit EPIPE on
    // top of either — resolve exactly once, first signal wins.
    let settled = false;
    const settle = (value: string) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };

    let child: ReturnType<typeof spawn>;
    try {
      // Direct interpreter launch — no shell — with the built-in spawn
      // timeout as the five-second execution bound (a timed-out child is
      // killed and closes with a non-zero/null code, i.e. the fail-open
      // path below).
      child = spawn(PYTHON_BIN, [MEMORY_RETRIEVAL_HOOK_PATH], {
        cwd: PROJECT_ROOT,
        stdio: ['pipe', 'pipe', 'ignore'],
        timeout: MEMORY_RETRIEVAL_TIMEOUT_MS,
      });
    } catch {
      settle('');
      return;
    }

    let stdout = '';
    child.stdout?.on('data', (chunk: Buffer | string) => {
      stdout += String(chunk);
    });

    // Spawn failure (e.g. interpreter not found) — fail open.
    child.on('error', () => settle(''));
    child.on('close', (code) => {
      // The hook's own guard always exits zero; any other code (including
      // null from a timeout kill) is a process failure — fail open.
      if (code !== 0) {
        settle('');
        return;
      }
      settle(extractAdditionalContext(stdout));
    });

    // EPIPE guard: if the child dies before consuming stdin, the write
    // below surfaces here instead of crashing the process.
    child.stdin?.on('error', () => settle(''));
    child.stdin?.end(
      JSON.stringify({
        prompt: request.prompt,
        session_id: request.sessionId,
      }),
    );
  });
