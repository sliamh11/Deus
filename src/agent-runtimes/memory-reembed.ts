/**
 * Fail-open subprocess adapter for memory re-embedding after edits
 * (LIA-417 / D3).
 *
 * This is a narrow adapter over the UNCHANGED
 * `scripts/memory_tree_hook.py` PostToolUse protocol. It only launches the
 * outer hook with one canonical edit event; vault membership, file type,
 * database availability, feature gates, worker launch, and embedding results
 * remain owned by the Python hook.
 */

import { spawn } from 'child_process';
import path from 'path';

import { PROJECT_ROOT } from '../config.js';
import { PYTHON_BIN } from '../platform.js';

/** Absolute path of the unchanged Python re-embedding hook. */
export const MEMORY_REEMBED_HOOK_PATH = path.join(
  PROJECT_ROOT,
  'scripts',
  'memory_tree_hook.py',
);

/** Generous bound for launching the hook's own fire-and-forget worker. */
export const MEMORY_REEMBED_TIMEOUT_MS = 5_000;

export type MemoryReembedToolName = 'Write' | 'Edit' | 'MultiEdit';

/** One canonical PostToolUse edit event accepted by the existing hook. */
export interface MemoryReembedRequest {
  toolName: MemoryReembedToolName;
  filePath: string;
}

/** Middleware-facing re-embedding adapter shape. */
export type MemoryReembedAdapter = (
  request: MemoryReembedRequest,
) => Promise<void>;

/**
 * Launches the existing memory-tree hook after a successful edit. The hook
 * returns promptly after starting its detached worker; every process failure
 * is contained so re-embedding can never replace a successful tool result.
 */
export const triggerMemoryReembed: MemoryReembedAdapter = (request) =>
  new Promise((resolve) => {
    let settled = false;
    const settle = () => {
      if (settled) return;
      settled = true;
      resolve();
    };

    let child: ReturnType<typeof spawn>;
    try {
      child = spawn(PYTHON_BIN, [MEMORY_REEMBED_HOOK_PATH], {
        cwd: PROJECT_ROOT,
        stdio: ['pipe', 'ignore', 'ignore'],
        timeout: MEMORY_REEMBED_TIMEOUT_MS,
      });
    } catch {
      settle();
      return;
    }

    child.on('error', settle);
    child.on('close', settle);
    child.stdin?.on('error', settle);

    try {
      child.stdin?.end(
        JSON.stringify({
          hook_event_name: 'PostToolUse',
          tool_name: request.toolName,
          tool_input: {
            file_path: request.filePath,
          },
        }),
      );
    } catch {
      settle();
    }
  });
