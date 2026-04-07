/**
 * Platform abstraction layer for Deus.
 *
 * This is the ONLY file allowed to call os.platform(), process.platform,
 * or process.env.HOME directly. All other source files import from here.
 *
 * Enforced by ESLint no-restricted-syntax rules — violations fail the build.
 *
 * See: docs/decisions/platform-abstraction-layer.md
 */

import { execFileSync } from 'child_process';
import fs from 'fs';
import os from 'os';

// ── Platform detection ─────────────────────────────────────────────────────

export const IS_WINDOWS = process.platform === 'win32';
export const IS_MACOS = process.platform === 'darwin';
export const IS_LINUX = process.platform === 'linux';

/** WSL detection — check /proc, not env vars (WSL_DISTRO_NAME isn't set under systemd). */
export const IS_WSL =
  IS_LINUX && fs.existsSync('/proc/sys/fs/binfmt_misc/WSLInterop');

// ── Directories ────────────────────────────────────────────────────────────

/** Home directory. Always use this — process.env.HOME is undefined on Windows. */
export const homeDir = os.homedir();

// ── Process management ─────────────────────────────────────────────────────

/**
 * Terminate a process cross-platform.
 * - Unix: SIGTERM to process group first (-pid), falls back to individual PID.
 * - Windows: `taskkill /F /T /PID` to kill the process tree.
 */
export function killProcess(pid: number): void {
  if (IS_WINDOWS) {
    try {
      execFileSync('taskkill', ['/F', '/T', '/PID', String(pid)], {
        stdio: 'pipe',
      });
    } catch {
      // already dead
    }
    return;
  }
  try {
    process.kill(-pid, 'SIGTERM');
  } catch {
    try {
      process.kill(pid, 'SIGTERM');
    } catch {
      // already dead
    }
  }
}

/**
 * Force-kill a child process cross-platform.
 * - Unix: sends SIGKILL.
 * - Windows: uses taskkill (SIGKILL throws ERR_UNKNOWN_SIGNAL on Windows).
 */
export function forceKillProcess(pid: number): void {
  if (IS_WINDOWS) {
    try {
      execFileSync('taskkill', ['/F', '/T', '/PID', String(pid)], {
        stdio: 'pipe',
      });
    } catch {
      // already dead
    }
    return;
  }
  try {
    process.kill(pid, 'SIGKILL');
  } catch {
    // already dead
  }
}

/** Check if a process is still alive (signal 0 probe). */
export function processExists(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

// ── Utilities ──────────────────────────────────────────────────────────────

/** Platform-appropriate startup hint for building the container image. */
export const containerBuildHint = IS_WINDOWS
  ? 'Agent container image not built. Run: docker build -t deus-agent ./container'
  : 'Agent container image not built. Run: ./container/build.sh';

// ── Container networking ───────────────────────────────────────────────────

/**
 * Detect the bind address for the credential proxy.
 * - macOS / Windows: 127.0.0.1 (Docker Desktop routes host.docker.internal to loopback).
 * - WSL: 127.0.0.1 (same VM routing as macOS).
 * - Linux: docker0 bridge IP (isolates proxy to container network only).
 */
export function detectProxyBindHost(): string {
  if (IS_MACOS || IS_WINDOWS) return '127.0.0.1';
  if (IS_WSL) return '127.0.0.1';

  // Bare-metal Linux: bind to the docker0 bridge IP instead of 0.0.0.0
  const ifaces = os.networkInterfaces();
  const docker0 = ifaces['docker0'];
  if (docker0) {
    const ipv4 = docker0.find((a) => a.family === 'IPv4');
    if (ipv4) return ipv4.address;
  }
  // Fallback: standard docker0 bridge IP. Never bind 0.0.0.0 — that
  // exposes the credential proxy to the entire network.
  return '172.17.0.1';
}

/** CLI args needed for the container to resolve the host gateway. */
export function hostGatewayArgs(): string[] {
  if (IS_LINUX) {
    return ['--add-host=host.docker.internal:host-gateway'];
  }
  return [];
}
