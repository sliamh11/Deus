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

  if (platform === 'windows') {
    setupWindowsCli(projectRoot, homeDir);
  } else {
    setupUnixCli(projectRoot, homeDir);
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

  // Check if ~/.local/bin is in PATH; if not, add it to shell config
  const pathEnv = process.env.PATH || '';
  const delimiter = process.platform === 'win32' ? ';' : ':';
  let inPath = pathEnv.split(delimiter).some((p) => p === binDir);

  if (!inPath) {
    const exportLine = `export PATH="$HOME/.local/bin:$PATH"`;
    const shellConfigs = [
      path.join(homeDir, '.zshrc'),
      path.join(homeDir, '.bashrc'),
    ];

    for (const rc of shellConfigs) {
      if (!fs.existsSync(rc)) continue;
      const content = fs.readFileSync(rc, 'utf-8');
      if (content.includes('.local/bin')) {
        inPath = true;
        break;
      }
    }

    if (!inPath) {
      // Detect user's shell and append to the appropriate config
      const shell = process.env.SHELL || '/bin/bash';
      const rcFile = shell.endsWith('zsh')
        ? path.join(homeDir, '.zshrc')
        : path.join(homeDir, '.bashrc');

      try {
        fs.appendFileSync(rcFile, `\n# Added by Deus setup\n${exportLine}\n`);
        inPath = true;
        logger.info({ rcFile }, 'Added ~/.local/bin to PATH in shell config');
      } catch (err) {
        logger.warn({ err, rcFile }, 'Could not update shell config');
      }
    }
  }

  emitStatus('SETUP_CLI', {
    STATUS: 'success',
    LINK_PATH: linkPath,
    SCRIPT_PATH: scriptPath,
    IN_PATH: inPath,
  });
}

/**
 * Check what exists at the CLI symlink path.
 * Returns:
 * - 'none'    — nothing exists, safe to create
 * - 'ours'    — symlink pointing to a deus-cmd.sh, safe to replace
 * - 'dead'    — dead symlink, safe to replace
 * - 'foreign' — something else (different binary, regular file), DO NOT replace
 */
export function checkExistingCli(
  linkPath: string,
): 'none' | 'ours' | 'dead' | 'foreign' {
  try {
    const stat = fs.lstatSync(linkPath);

    if (stat.isSymbolicLink()) {
      const target = fs.readlinkSync(linkPath);
      // Check if target is alive
      if (!fs.existsSync(linkPath)) return 'dead';
      // Check if it points to any deus-cmd.sh (ours, possibly different install path)
      if (path.basename(target) === 'deus-cmd.sh') return 'ours';
      return 'foreign';
    }

    // Regular file or directory — not ours
    return 'foreign';
  } catch {
    return 'none';
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

  // Create a .cmd shim that invokes the PowerShell script
  const cmdContent =
    [
      '@echo off',
      `powershell -NoProfile -ExecutionPolicy Bypass -File "${ps1Path}" %*`,
    ].join('\r\n') + '\r\n';

  fs.writeFileSync(cmdPath, cmdContent);
  logger.info({ cmdPath, ps1Path }, 'Created deus.cmd shim');

  // Check if binDir is in user PATH and add it if not
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

  emitStatus('SETUP_CLI', {
    STATUS: 'success',
    CMD_PATH: cmdPath,
    SCRIPT_PATH: ps1Path,
    IN_PATH: inPath,
    PATH_DIR: binDir,
  });
}
