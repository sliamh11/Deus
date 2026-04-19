/**
 * Process-level entry-point harness — container/agent-runner copy.
 *
 * Mirrors src/bootstrap.ts (PR #2/10), minus the pino logger and FatalError
 * dependency, since container/agent-runner has its own tsconfig + node_modules
 * and cannot import from src/. Logging goes through the same stderr convention
 * the file's `log()` helper uses elsewhere: `[<entry-name>] <message>`.
 *
 * Drift risk: two copies of the harness must stay behaviorally aligned.
 * Extracting both into a shared packages/ workspace is tracked in issue #218;
 * until then, any change here MUST be mirrored in src/bootstrap.ts (and vice
 * versa).
 */

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
      logError(name, `${name} main() failed`, err);
      // eslint-disable-next-line no-restricted-syntax -- harness-managed exit on main() reject; the no-process-exit rule explicitly exempts the bootstrap harness itself
      process.exit(exitCode);
    });
}

function installGlobalHandlers(
  name: string,
  exitOnUnhandledRejection: boolean,
): void {
  if (globalHandlersInstalled) return;
  globalHandlersInstalled = true;

  process.on('uncaughtException', (err: Error) => {
    logError(name, 'uncaughtException', err);
    // eslint-disable-next-line no-restricted-syntax -- harness-managed exit on uncaught exception; safer to crash than continue in undefined state
    process.exit(1);
  });

  process.on('unhandledRejection', (reason: unknown) => {
    logError(name, 'unhandledRejection', reason);
    // eslint-disable-next-line no-restricted-syntax -- harness-managed conditional exit; default is opt-out so this only fires when a caller explicitly chose strict-mode
    if (exitOnUnhandledRejection) process.exit(1);
  });
}

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

/** Test-only: reset the install-once guard. Do NOT call in production code. */
export function __resetBootstrapForTesting(): void {
  globalHandlersInstalled = false;
  process.removeAllListeners('uncaughtException');
  process.removeAllListeners('unhandledRejection');
}
