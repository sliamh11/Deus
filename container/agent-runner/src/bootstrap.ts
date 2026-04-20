/**
 * Process-level entry-point harness — container/agent-runner copy.
 *
 * Mirrors src/bootstrap.ts (PR #2/10), minus the pino logger and FatalError
 * dependency, since container/agent-runner has its own tsconfig + node_modules
 * and cannot import from src/. Logging goes through the same stderr convention
 * the file's `log()` helper uses elsewhere: `[<entry-name>] <message>`.
 *
 * Drift discipline: structural equivalence with src/bootstrap.ts is enforced
 * mechanically by `python3 scripts/drift_check.py --bootstrap-mirror` (and via
 * `--all` in CI). Anything wrapped in `// MIRROR-IGNORE-START / MIRROR-IGNORE-END`
 * is intentionally divergent (logger calls and the supporting helpers); the
 * rest must stay byte-for-byte aligned with the src/ copy. See issue #218 for
 * why these stay as two files instead of one shared package.
 */

// MIRROR-IGNORE-START -- container has no pino dep; uses console.error in the logError helper below (intentionally empty import block to align with src/'s imports section)
// MIRROR-IGNORE-END

export interface BootstrapOptions {
  /**
   * Identifier for the entry point (e.g. `'agent-runner'`).
   * Included in every error log for attribution.
   */
  readonly name: string;

  /** Exit code when `main()` rejects. Default: `1`. */
  readonly exitCode?: number;

  /** Terminate the process on `unhandledRejection`? Default: `false`. */
  readonly exitOnUnhandledRejection?: boolean;
}

let globalHandlersInstalled = false;

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
      logError(name, `${name} main() failed`, err);
      // eslint-disable-next-line no-restricted-syntax -- harness-managed exit on main() reject; the no-process-exit rule explicitly exempts the bootstrap harness itself
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
    logError(name, 'uncaughtException', err);
    // eslint-disable-next-line no-restricted-syntax -- harness-managed exit on uncaught exception; safer to crash than continue in undefined state
    process.exit(1);
    // MIRROR-IGNORE-END
  });

  process.on('unhandledRejection', (reason: unknown) => {
    // MIRROR-IGNORE-START -- intentional logger divergence (see file header)
    logError(name, 'unhandledRejection', reason);
    // eslint-disable-next-line no-restricted-syntax -- harness-managed conditional exit; default is opt-out so this only fires when a caller explicitly chose strict-mode
    if (exitOnUnhandledRejection) process.exit(1);
    // MIRROR-IGNORE-END
  });
}

// MIRROR-IGNORE-START -- helpers for the console-based variant; src/ has only `serializeError` and uses pino at the call sites instead
function logError(name: string, msg: string, err: unknown): void {
  const serialized = serializeError(err);
  console.error(
    `[${name}] ${msg}: ${typeof serialized === 'string' ? serialized : JSON.stringify(serialized)}`,
  );
}

function serializeError(err: unknown): unknown {
  if (err instanceof Error) {
    return { name: err.name, message: err.message, stack: err.stack };
  }
  return err;
}
// MIRROR-IGNORE-END

/** Test-only: reset the install-once guard. Do NOT call in production code. */
export function __resetBootstrapForTesting(): void {
  globalHandlersInstalled = false;
  process.removeAllListeners('uncaughtException');
  process.removeAllListeners('unhandledRejection');
}
