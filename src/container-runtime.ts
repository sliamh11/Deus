/**
 * Container runtime abstraction for Deus.
 * All runtime-specific logic lives here so swapping runtimes means changing one file.
 */
import { execFileSync, execSync } from 'child_process';

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
export const PROXY_BIND_HOST =
  process.env.CREDENTIAL_PROXY_HOST || detectProxyBindHost();

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

/** Ensure the container runtime is running, starting it if needed. */
export function ensureContainerRuntimeRunning(): void {
  try {
    execSync(`${CONTAINER_RUNTIME_BIN} info`, {
      stdio: 'pipe',
      timeout: 10000,
    });
    logger.debug('Container runtime already running');
  } catch (err) {
    logger.error({ err }, 'Failed to reach container runtime');
    logger.error(
      'FATAL: Container runtime failed to start. ' +
        'Agents cannot run without a container runtime. ' +
        'To fix: 1) Ensure Docker is installed and running, ' +
        '2) Run: docker info, 3) Restart Deus',
    );
    throw new Error('Container runtime is required but failed to start');
  }
}

/** Kill orphaned Deus containers from previous runs. */
export function cleanupOrphans(): void {
  try {
    // No shell quoting around the Go template: single quotes don't work on
    // Windows cmd.exe and are unnecessary here since there are no spaces.
    const output = execSync(
      `${CONTAINER_RUNTIME_BIN} ps --filter name=deus- --format {{.Names}}`,
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
