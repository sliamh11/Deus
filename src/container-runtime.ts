/**
 * Container runtime abstraction for Deus.
 * All runtime-specific logic lives here so swapping runtimes means changing one file.
 */
import { execFileSync } from 'child_process';

import { FatalError } from './errors/index.js';
import { logger } from './logger.js';
import { detectProxyBindHost, hostGatewayArgs } from './platform.js';

/** The container runtime binary name. */
export const CONTAINER_RUNTIME_BIN = process.env.CONTAINER_RUNTIME || 'docker';

/** Hostname containers use to reach the host machine. */
export const CONTAINER_HOST_GATEWAY = 'host.docker.internal';

/**
 * Address the credential proxy binds to.
 * Delegates to platform.ts for OS-aware detection.
 */
const rawProxyHost = process.env.CREDENTIAL_PROXY_HOST;
if (
  rawProxyHost &&
  ['0.0.0.0', '::'].includes(rawProxyHost) &&
  process.env.CREDENTIAL_PROXY_HOST_UNSAFE !== '1'
) {
  throw new Error(
    `CREDENTIAL_PROXY_HOST=${rawProxyHost} exposes the credential proxy to the network. ` +
      'Any host on your LAN could use your API credentials. ' +
      'If this is intentional, set CREDENTIAL_PROXY_HOST_UNSAFE=1.',
  );
}
export const PROXY_BIND_HOST = rawProxyHost || detectProxyBindHost();

// Re-export hostGatewayArgs so existing importers don't break.
export { hostGatewayArgs };

/** Returns CLI args for a readonly bind mount. */
export function readonlyMountArgs(
  hostPath: string,
  containerPath: string,
): string[] {
  return ['-v', `${hostPath}:${containerPath}:ro`];
}

/** Stop a container by name using execFileSync (no shell interpolation). */
export function stopContainerSync(name: string): void {
  execFileSync(CONTAINER_RUNTIME_BIN, ['stop', '-t', '1', name], {
    stdio: 'pipe',
    timeout: 15000,
  });
}

const DOCKER_MAX_RETRIES = 6;
const DOCKER_BASE_RETRY_MS = 5_000;
const DOCKER_MAX_RETRY_MS = 30_000;

// Sync sleep via Atomics.wait — blocks the event loop without busy-looping.
let sleepFn = (ms: number): void => {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);
};

/** Replace the sleep implementation (test-only). */
export function _setSleepFnForTests(fn: (ms: number) => void): void {
  sleepFn = fn;
}

/** Ensure the container runtime is running, retrying with exponential backoff. */
export function ensureContainerRuntimeRunning(): void {
  let lastErr: unknown;

  for (let attempt = 1; attempt <= DOCKER_MAX_RETRIES + 1; attempt++) {
    try {
      execFileSync(CONTAINER_RUNTIME_BIN, ['info'], {
        stdio: 'pipe',
        timeout: 10_000,
      });
      if (attempt > 1) {
        logger.info(
          { attempt },
          'Container runtime became available after retries',
        );
      } else {
        logger.debug('Container runtime already running');
      }
      return;
    } catch (err) {
      lastErr = err;
      if (attempt <= DOCKER_MAX_RETRIES) {
        const delayMs = Math.min(
          DOCKER_BASE_RETRY_MS * Math.pow(2, attempt - 1),
          DOCKER_MAX_RETRY_MS,
        );
        logger.warn(
          { attempt, maxRetries: DOCKER_MAX_RETRIES, delayMs },
          'Container runtime not ready, retrying...',
        );
        sleepFn(delayMs);
      }
    }
  }

  logger.error(
    { err: lastErr, attempts: DOCKER_MAX_RETRIES + 1 },
    'Container runtime failed to start after all retries',
  );
  logger.error(
    'FATAL: Container runtime unreachable. ' +
      'Agents cannot run without a container runtime. ' +
      'To fix: 1) Ensure Docker is installed and running, ' +
      '2) Run: docker info, 3) Restart Deus',
  );
  throw new FatalError('Container runtime is required but failed to start', {
    cause: lastErr,
  });
}

/** Kill orphaned Deus containers from previous runs. */
export function cleanupOrphans(): void {
  try {
    const output = execFileSync(
      CONTAINER_RUNTIME_BIN,
      ['ps', '--filter', 'name=deus-', '--format', '{{.Names}}'],
      { stdio: ['pipe', 'pipe', 'pipe'], encoding: 'utf-8' },
    );
    const orphans = output.trim().split('\n').filter(Boolean);
    for (const name of orphans) {
      try {
        stopContainerSync(name);
      } catch {
        /* already stopped */
      }
    }
    if (orphans.length > 0) {
      logger.info(
        { count: orphans.length, names: orphans },
        'Stopped orphaned containers',
      );
    }
  } catch (err) {
    logger.warn({ err }, 'Failed to clean up orphaned containers');
  }
}
