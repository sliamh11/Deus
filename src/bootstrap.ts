/**
 * Process-level entry-point harness — see docs/decisions/error-discipline.md
 *
 * Every Node entry point (agent-runner, setup, channels, skills, etc.) calls
 * `bootstrap(main, { name })` instead of `main()`. This guarantees:
 *
 *   1. A `.catch()` on the top-level main() — no silently-swallowed async throws.
 *   2. Global `uncaughtException` / `unhandledRejection` handlers installed
 *      before main() runs, so a crash anywhere in the process lands in logs
 *      with entry-point attribution instead of vanishing.
 *   3. `FatalError` is logged at `fatal` level; everything else at `error`.
 *
 * The global handlers are installed exactly once per process. Safe to call
 * `bootstrap()` even if another module (e.g. `src/logger.ts`) also wires
 * `process.on('uncaughtException')` — multiple listeners coexist on Node;
 * whichever calls `process.exit` first terminates. We install our own
 * so attribution is correct regardless of import order.
 */

// MIRROR-IGNORE-START -- intentional logger divergence: src/ uses pino + FatalError; container/agent-runner/src/bootstrap.ts uses console.error (no pino dep)
import { FatalError, isDeusError } from './errors/index.js';
import { logger } from './logger.js';
// MIRROR-IGNORE-END

export interface BootstrapOptions {
  /**
   * Identifier for the entry point (e.g. `'agent-runner'`, `'setup'`).
   * Included in every error log for attribution across a multi-process daemon.
   */
  readonly name: string;

  /** Exit code when `main()` rejects. Default: `1`. */
  readonly exitCode?: number;

  /**
   * Terminate the process on `unhandledRejection`? Default: `false`.
   * Node's default since v15 is to crash; we keep the current Deus behavior
   * (log + continue) until every entry point has migrated. Flip to `true`
   * per-entry-point once its call sites have been audited.
   */
  readonly exitOnUnhandledRejection?: boolean;
}

let globalHandlersInstalled = false;

/**
 * Wrap `mainFn` with attribution + guaranteed catch, and install global
 * process-level error handlers (once per process).
 *
 * Usage at an entry point:
 *
 *   import { bootstrap } from './bootstrap.js';
 *   async function main() { ... }
 *   bootstrap(main, { name: 'agent-runner' });
 */
export function bootstrap(
  mainFn: () => Promise<void> | void,
  options: BootstrapOptions,
): void {
  const { name, exitCode = 1, exitOnUnhandledRejection = false } = options;

  installGlobalHandlers(name, exitOnUnhandledRejection);

  Promise.resolve()
    .then(() => mainFn())
    .catch((err: unknown) => {
      // MIRROR-IGNORE-START -- intentional logger divergence (see file header)
      const severity = err instanceof FatalError ? 'fatal' : 'error';
      logger[severity](
        { err: serializeError(err), entry: name },
        `${name} main() failed`,
      );
      process.exit(exitCode);
      // MIRROR-IGNORE-END
    });
}

function installGlobalHandlers(
  name: string,
  exitOnUnhandledRejection: boolean,
): void {
  if (globalHandlersInstalled) return;
  globalHandlersInstalled = true;

  process.on('uncaughtException', (err: Error) => {
    // MIRROR-IGNORE-START -- intentional logger divergence (see file header)
    logger.fatal(
      { err: serializeError(err), entry: name },
      'uncaughtException',
    );
    process.exit(1);
    // MIRROR-IGNORE-END
  });

  process.on('unhandledRejection', (reason: unknown) => {
    // MIRROR-IGNORE-START -- intentional logger divergence (see file header)
    logger.error(
      { err: serializeError(reason), entry: name },
      'unhandledRejection',
    );
    if (exitOnUnhandledRejection) process.exit(1);
    // MIRROR-IGNORE-END
  });
}

// MIRROR-IGNORE-START -- serializeError body diverges: src/ checks isDeusError; container/ has no DeusError dep so falls through to plain Error handling
function serializeError(err: unknown): unknown {
  if (isDeusError(err)) return err.toJSON();
  if (err instanceof Error) {
    return { name: err.name, message: err.message, stack: err.stack };
  }
  return err;
}
// MIRROR-IGNORE-END

/**
 * Test-only: reset the install-once guard so `bootstrap()` can be exercised
 * fresh in each unit test. Do NOT call in production code.
 *
 * @internal
 */
export function __resetBootstrapForTesting(): void {
  globalHandlersInstalled = false;
  process.removeAllListeners('uncaughtException');
  process.removeAllListeners('unhandledRejection');
}
