/**
 * Step: cli — Register `deus` as a global CLI command.
 *
 * - macOS / Linux: symlinks deus-cmd.sh → ~/.local/bin/deus
 * - Windows: creates deus.cmd shim → %USERPROFILE%\.local\bin\ and adds to user PATH
 */
import { execSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { logger } from '../src/logger.js';
import { getPlatform } from './platform.js';
import { emitStatus } from './status.js';

export async function run(_args: string[]): Promise<void> {
  const projectRoot = process.cwd();
  const platform = getPlatform();
  const homeDir = os.homedir();

  // v1 and v2 installs are independent in BOTH directions: each leg gets
  // its own try/catch so an uncaught throw from either (e.g. a filesystem
  // permission error mid-symlink) can never prevent the other from being
  // attempted. A code review caught that an earlier version only protected
  // one direction (v2's failure couldn't affect v1's already-emitted
  // status) but left v1 able to abort run() before v2 was ever reached.
  try {
    if (platform === 'windows') {
      setupWindowsCli(projectRoot, homeDir);
    } else {
      setupUnixCli(projectRoot, homeDir);
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    logger.error({ err }, 'deus (v1) CLI setup threw unexpectedly');
    emitStatus('SETUP_CLI', { STATUS: 'failed', ERROR: message });
  }

  try {
    if (platform === 'windows') {
      setupWindowsCliV2(projectRoot, homeDir);
    } else {
      setupUnixCliV2(projectRoot, homeDir);
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    logger.warn(
      { err },
      'deus-v2 CLI setup failed (v1 deus install unaffected)',
    );
    emitStatus('SETUP_CLI_V2', { STATUS: 'failed', ERROR: message });
  }
}

function setupUnixCli(projectRoot: string, homeDir: string): void {
  const binDir = path.join(homeDir, '.local', 'bin');
  const linkPath = path.join(binDir, 'deus');
  const scriptPath = path.join(projectRoot, 'deus-cmd.sh');

  if (!fs.existsSync(scriptPath)) {
    emitStatus('SETUP_CLI', {
      STATUS: 'failed',
      ERROR: 'deus-cmd.sh not found',
    });
    return;
  }

  // Ensure script is executable
  try {
    fs.chmodSync(scriptPath, 0o755);
  } catch {
    // May fail on some filesystems, non-critical
  }

  fs.mkdirSync(binDir, { recursive: true });

  // Check what exists at the target path before overwriting
  const existing = checkExistingCli(linkPath);
  if (existing === 'foreign') {
    logger.warn(
      { linkPath },
      'A non-Deus binary already exists at the CLI path. Skipping symlink creation to avoid data loss.',
    );
    emitStatus('SETUP_CLI', {
      STATUS: 'conflict',
      LINK_PATH: linkPath,
      SCRIPT_PATH: scriptPath,
      EXISTING: 'foreign',
      IN_PATH: false,
    });
    return;
  }

  // Safe to replace: either nothing, a dead symlink, or our own deus-cmd.sh symlink
  try {
    fs.unlinkSync(linkPath);
  } catch {
    // Doesn't exist
  }

  fs.symlinkSync(scriptPath, linkPath);
  logger.info({ linkPath, scriptPath }, 'Created deus CLI symlink');

  // Clean up stale /usr/local/bin/deus symlink that may shadow the new one
  cleanStaleLegacySymlink(logger);

  const inPath = ensureUnixBinDirInPath(homeDir, binDir);

  emitStatus('SETUP_CLI', {
    STATUS: 'success',
    LINK_PATH: linkPath,
    SCRIPT_PATH: scriptPath,
    IN_PATH: inPath,
  });
}

/**
 * Install the `deus-v2` launcher symlink (LIA-434) — a parallel, independent
 * command alongside `deus`, never replacing it. See deus-v2-cmd.mjs for the
 * checkout-location/validation/delegation logic; this only wires up the
 * symlink, mirroring setupUnixCli's structure.
 */
function setupUnixCliV2(projectRoot: string, homeDir: string): void {
  const binDir = path.join(homeDir, '.local', 'bin');
  const linkPath = path.join(binDir, 'deus-v2');
  const scriptPath = path.join(projectRoot, 'deus-v2-cmd.mjs');

  if (!fs.existsSync(scriptPath)) {
    emitStatus('SETUP_CLI_V2', {
      STATUS: 'failed',
      ERROR: 'deus-v2-cmd.mjs not found',
    });
    return;
  }

  try {
    fs.chmodSync(scriptPath, 0o755);
  } catch {
    // May fail on some filesystems, non-critical
  }

  fs.mkdirSync(binDir, { recursive: true });

  const existing = checkExistingCli(linkPath, 'deus-v2-cmd.mjs');
  if (existing === 'foreign') {
    logger.warn(
      { linkPath },
      'A non-Deus binary already exists at the deus-v2 CLI path. Skipping symlink creation to avoid data loss.',
    );
    emitStatus('SETUP_CLI_V2', {
      STATUS: 'conflict',
      LINK_PATH: linkPath,
      SCRIPT_PATH: scriptPath,
      EXISTING: 'foreign',
      IN_PATH: false,
    });
    return;
  }

  try {
    fs.unlinkSync(linkPath);
  } catch {
    // Doesn't exist
  }

  fs.symlinkSync(scriptPath, linkPath);
  logger.info({ linkPath, scriptPath }, 'Created deus-v2 CLI symlink');

  // No cleanStaleLegacySymlink call here — there's no historical
  // /usr/local/bin/deus-v2 to clean up for a brand-new command.

  const inPath = ensureUnixBinDirInPath(homeDir, binDir);

  emitStatus('SETUP_CLI_V2', {
    STATUS: 'success',
    LINK_PATH: linkPath,
    SCRIPT_PATH: scriptPath,
    IN_PATH: inPath,
  });
}

/**
 * Ensure `binDir` is on PATH, appending an export line to the user's shell
 * config if needed. Extracted from setupUnixCli so setupUnixCliV2 can reuse
 * it without duplicating the PATH-mutation logic (both install into the same
 * ~/.local/bin).
 */
function ensureUnixBinDirInPath(homeDir: string, binDir: string): boolean {
  const pathEnv = process.env.PATH || '';
  const delimiter = process.platform === 'win32' ? ';' : ':';
  let inPath = pathEnv.split(delimiter).some((p) => p === binDir);

  if (inPath) return true;

  const exportLine = `export PATH="$HOME/.local/bin:$PATH"`;
  const shellConfigs = [
    path.join(homeDir, '.zshrc'),
    path.join(homeDir, '.bashrc'),
  ];

  for (const rc of shellConfigs) {
    if (!fs.existsSync(rc)) continue;
    const content = fs.readFileSync(rc, 'utf-8');
    if (content.includes('.local/bin')) {
      return true;
    }
  }

  // Detect user's shell and append to the appropriate config
  const shell = process.env.SHELL || '/bin/bash';
  const rcFile = shell.endsWith('zsh')
    ? path.join(homeDir, '.zshrc')
    : path.join(homeDir, '.bashrc');

  try {
    fs.appendFileSync(rcFile, `\n# Added by Deus setup\n${exportLine}\n`);
    logger.info({ rcFile }, 'Added ~/.local/bin to PATH in shell config');
    return true;
  } catch (err) {
    logger.warn({ err, rcFile }, 'Could not update shell config');
    return false;
  }
}

/**
 * Check what exists at the CLI symlink path.
 * Returns:
 * - 'none'    — nothing exists, safe to create
 * - 'ours'    — symlink pointing to a script named `expectedBasename`, safe to replace
 * - 'dead'    — dead symlink, safe to replace
 * - 'foreign' — something else (different binary, regular file), DO NOT replace
 *
 * `expectedBasename` defaults to 'deus-cmd.sh' (the v1 launcher) so existing
 * call sites are unaffected; the deus-v2 launcher (LIA-434) passes
 * 'deus-v2-cmd.mjs'.
 */
export function checkExistingCli(
  linkPath: string,
  expectedBasename = 'deus-cmd.sh',
): 'none' | 'ours' | 'dead' | 'foreign' {
  try {
    const stat = fs.lstatSync(linkPath);

    if (stat.isSymbolicLink()) {
      const target = fs.readlinkSync(linkPath);
      // Check if target is alive
      if (!fs.existsSync(linkPath)) return 'dead';
      // Check if it points to our own script (ours, possibly different install path)
      if (path.basename(target) === expectedBasename) return 'ours';
      return 'foreign';
    }

    // Regular file or directory — not ours
    return 'foreign';
  } catch {
    return 'none';
  }
}

// Exact structural patterns for the two shims this step generates — see
// setupWindowsCli/setupWindowsCliV2's cmdContent below, which these must
// stay byte-for-byte in sync with. Anchored end-to-end (^...$, content has
// no other lines) with only the absolute script path left as a wildcard, so
// a relocated install (different projectRoot) is still recognized as ours.
// The `[\\/]` immediately before the basename is load-bearing, not
// decorative: without it, a code review caught that a FOREIGN shim naming
// e.g. "custom-deus-v2-cmd.mjs" (a different file whose name merely ends
// with the expected basename, no path-separator boundary) would also
// match and be wrongly classified 'ours', overwriting the user's file.
const CMD_SHIM_PATTERNS: Record<'v1' | 'v2', RegExp> = {
  v1: /^@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "[^"\r\n]*[\\/]deus-cmd\.ps1" %\*\r\n$/,
  v2: /^@echo off\r\nnode "[^"\r\n]*[\\/]deus-v2-cmd\.mjs" %\*\r\n$/,
};

/**
 * Windows equivalent of checkExistingCli's foreign-detection, for the .cmd
 * shims (no symlinks involved, so basename-of-target doesn't apply): a
 * missing path is 'none'; an existing symlink is always 'foreign' — never
 * followed, since fs.writeFileSync would silently overwrite whatever it
 * points at; a regular file is 'ours' only if its content EXACTLY matches
 * this step's own generated shim structure for `kind` (a loose
 * "contains the basename somewhere" check was caught by code review as
 * unsafe — a user-authored wrapper or unrelated command that merely
 * mentions the target script's name would have been silently overwritten).
 */
export function checkExistingCmdShim(
  cmdPath: string,
  kind: 'v1' | 'v2',
): 'none' | 'ours' | 'foreign' {
  let stat;
  try {
    stat = fs.lstatSync(cmdPath);
  } catch {
    return 'none';
  }
  if (stat.isSymbolicLink()) return 'foreign';
  try {
    const content = fs.readFileSync(cmdPath, 'utf-8');
    return CMD_SHIM_PATTERNS[kind].test(content) ? 'ours' : 'foreign';
  } catch {
    return 'foreign';
  }
}

/**
 * Remove a legacy CLI symlink if it points to a dead target.
 * Old manual installs can leave stale symlinks that shadow ~/.local/bin/deus.
 * @param legacyPath defaults to /usr/local/bin/deus; override for testing.
 */
export function cleanStaleLegacySymlink(
  log: {
    info: (...args: unknown[]) => void;
    warn: (...args: unknown[]) => void;
  },
  legacyPath = '/usr/local/bin/deus',
): void {
  try {
    const stat = fs.lstatSync(legacyPath);
    if (!stat.isSymbolicLink()) return; // regular file — don't touch

    // Check if the symlink target actually exists
    if (fs.existsSync(legacyPath)) return; // target is alive — nothing to do

    // Dead symlink — try to remove
    try {
      fs.unlinkSync(legacyPath);
      log.info({ legacyPath }, 'Removed stale legacy CLI symlink');
    } catch {
      log.warn(
        { legacyPath },
        'Stale symlink at /usr/local/bin/deus may shadow the CLI. Remove it manually: sudo rm /usr/local/bin/deus',
      );
    }
  } catch {
    // legacyPath doesn't exist — nothing to do
  }
}

function setupWindowsCli(projectRoot: string, homeDir: string): void {
  const binDir = path.join(homeDir, '.local', 'bin');
  const cmdPath = path.join(binDir, 'deus.cmd');
  const ps1Path = path.join(projectRoot, 'deus-cmd.ps1');

  if (!fs.existsSync(ps1Path)) {
    emitStatus('SETUP_CLI', {
      STATUS: 'failed',
      ERROR: 'deus-cmd.ps1 not found',
    });
    return;
  }

  fs.mkdirSync(binDir, { recursive: true });

  const existingShim = checkExistingCmdShim(cmdPath, 'v1');
  if (existingShim === 'foreign') {
    logger.warn(
      { cmdPath },
      'A non-Deus file or symlink already exists at the CLI path. Skipping shim creation to avoid data loss.',
    );
    emitStatus('SETUP_CLI', {
      STATUS: 'conflict',
      CMD_PATH: cmdPath,
      SCRIPT_PATH: ps1Path,
      EXISTING: 'foreign',
      IN_PATH: false,
    });
    return;
  }

  // Create a .cmd shim that invokes the PowerShell script
  const cmdContent =
    [
      '@echo off',
      `powershell -NoProfile -ExecutionPolicy Bypass -File "${ps1Path}" %*`,
    ].join('\r\n') + '\r\n';

  fs.writeFileSync(cmdPath, cmdContent);
  logger.info({ cmdPath, ps1Path }, 'Created deus.cmd shim');

  const inPath = ensureWindowsBinDirInPath(binDir);

  emitStatus('SETUP_CLI', {
    STATUS: 'success',
    CMD_PATH: cmdPath,
    SCRIPT_PATH: ps1Path,
    IN_PATH: inPath,
    PATH_DIR: binDir,
  });
}

/**
 * Install the `deus-v2.cmd` shim (LIA-434) — a parallel, independent command
 * alongside `deus`, never replacing it. Unlike deus.cmd (which wraps a native
 * .ps1 via `powershell -File`), deus-v2-cmd.mjs is a Node script, so the
 * shim invokes `node` directly.
 */
function setupWindowsCliV2(projectRoot: string, homeDir: string): void {
  const binDir = path.join(homeDir, '.local', 'bin');
  const cmdPath = path.join(binDir, 'deus-v2.cmd');
  const scriptPath = path.join(projectRoot, 'deus-v2-cmd.mjs');

  if (!fs.existsSync(scriptPath)) {
    emitStatus('SETUP_CLI_V2', {
      STATUS: 'failed',
      ERROR: 'deus-v2-cmd.mjs not found',
    });
    return;
  }

  fs.mkdirSync(binDir, { recursive: true });

  const existingShim = checkExistingCmdShim(cmdPath, 'v2');
  if (existingShim === 'foreign') {
    logger.warn(
      { cmdPath },
      'A non-Deus file or symlink already exists at the deus-v2 CLI path. Skipping shim creation to avoid data loss.',
    );
    emitStatus('SETUP_CLI_V2', {
      STATUS: 'conflict',
      CMD_PATH: cmdPath,
      SCRIPT_PATH: scriptPath,
      EXISTING: 'foreign',
      IN_PATH: false,
    });
    return;
  }

  const cmdContent =
    ['@echo off', `node "${scriptPath}" %*`].join('\r\n') + '\r\n';

  fs.writeFileSync(cmdPath, cmdContent);
  logger.info({ cmdPath, scriptPath }, 'Created deus-v2.cmd shim');

  const inPath = ensureWindowsBinDirInPath(binDir);

  emitStatus('SETUP_CLI_V2', {
    STATUS: 'success',
    CMD_PATH: cmdPath,
    SCRIPT_PATH: scriptPath,
    IN_PATH: inPath,
    PATH_DIR: binDir,
  });
}

/**
 * Ensure `binDir` is on the user's Windows PATH. Extracted from
 * setupWindowsCli so setupWindowsCliV2 can reuse it without duplicating the
 * PATH-mutation logic (both install into the same ~/.local/bin).
 */
function ensureWindowsBinDirInPath(binDir: string): boolean {
  let inPath = false;
  try {
    const currentPath = execSync(
      "powershell -NoProfile -Command \"[Environment]::GetEnvironmentVariable('PATH', 'User')\"",
      { encoding: 'utf-8' },
    ).trim();
    inPath = currentPath
      .split(';')
      .some((p) => p.toLowerCase() === binDir.toLowerCase());

    if (!inPath) {
      const newPath = currentPath ? `${currentPath};${binDir}` : binDir;
      execSync(
        `powershell -NoProfile -Command "[Environment]::SetEnvironmentVariable('PATH', '${newPath.replace(/'/g, "''")}', 'User')"`,
        { stdio: 'pipe' },
      );
      inPath = true;
      logger.info({ binDir }, 'Added to user PATH');
    }
  } catch (err) {
    logger.warn({ err }, 'Could not check/update user PATH');
  }
  return inPath;
}
