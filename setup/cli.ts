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

  // Remove existing symlink/file if present
  try {
    fs.unlinkSync(linkPath);
  } catch {
    // Doesn't exist
  }

  fs.symlinkSync(scriptPath, linkPath);
  logger.info({ linkPath, scriptPath }, 'Created deus CLI symlink');

  // Check if ~/.local/bin is in PATH
  const pathEnv = process.env.PATH || '';
  const inPath = pathEnv.split(':').some((p) => p === binDir);

  emitStatus('SETUP_CLI', {
    STATUS: 'success',
    LINK_PATH: linkPath,
    SCRIPT_PATH: scriptPath,
    IN_PATH: inPath,
  });
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
