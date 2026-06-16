import { spawn } from 'child_process';
import { mkdirSync, writeFileSync } from 'fs';
import path from 'path';

import { fireAndForget } from './async/index.js';
import { PROJECT_ROOT } from './config.js';
import { PYTHON_BIN } from './platform.js';

const MEMORY_INDEXER_PATH = path.join(
  PROJECT_ROOT,
  'scripts',
  'memory_indexer.py',
);

/**
 * Write a markdown session log to the vault and fire-and-forget index it into
 * long-term memory via `memory_indexer.py --add` (atom extraction + embedding
 * dedup happen inside the indexer). Shared by the idle auto-compress path
 * (auto-compress.ts) and the Web UI conversation consolidation path
 * (webui-consolidation.ts, LIA-295) so the spawn + cross-platform PYTHON_BIN
 * wiring lives in exactly one place.
 *
 * `onSettle` (optional) fires when the indexer child closes or errors — callers
 * that hold an in-flight guard use it to release the guard once the detached
 * `--add` actually finishes, not merely when the spawn is launched.
 */
export function writeSessionLogAndIndex(
  savedPath: string,
  content: string,
  spawnLabel: string,
  onSettle?: () => void,
): void {
  mkdirSync(path.dirname(savedPath), { recursive: true });
  writeFileSync(savedPath, content, 'utf-8');

  fireAndForget(
    () =>
      new Promise<void>((resolve, reject) => {
        const child = spawn(
          PYTHON_BIN,
          [MEMORY_INDEXER_PATH, '--add', savedPath],
          { stdio: ['ignore', 'ignore', 'pipe'] },
        );
        // onSettle is idempotent at the call site (a Set.delete), so firing on
        // both 'error' and a subsequent 'close' is harmless.
        child.on('close', (code) => {
          onSettle?.();
          code === 0 ? resolve() : reject(new Error(`indexer exit ${code}`));
        });
        child.on('error', (err) => {
          onSettle?.();
          reject(err);
        });
      }),
    { name: spawnLabel },
  );
}
