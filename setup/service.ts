/**
 * Step: service — Generate and load service manager config.
 * Replaces 08-setup-service.sh
 *
 * Fixes: Root→system systemd, WSL nohup fallback, no `|| true` swallowing errors.
 */
import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';

import { HOME_DIR } from '../src/config.js';
import { logger } from '../src/logger.js';
import {
  getPlatform,
  getNodePath,
  getServiceManager,
  hasSystemd,
  isRoot,
  isWSL,
  commandExists,
} from './platform.js';
import { emitStatus } from './status.js';

export async function run(_args: string[]): Promise<void> {
  const projectRoot = process.cwd();
  const platform = getPlatform();
  const nodePath = getNodePath();
  const homeDir = HOME_DIR;

  logger.info({ platform, nodePath, projectRoot }, 'Setting up service');

  // Build first
  logger.info('Building TypeScript');
  try {
    execSync('npm run build', {
      cwd: projectRoot,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    logger.info('Build succeeded');
  } catch {
    logger.error('Build failed');
    emitStatus('SETUP_SERVICE', {
      SERVICE_TYPE: 'unknown',
      NODE_PATH: nodePath,
      PROJECT_PATH: projectRoot,
      STATUS: 'failed',
      ERROR: 'build_failed',
      LOG: 'logs/setup.log',
    });
    process.exit(1);
  }

  fs.mkdirSync(path.join(projectRoot, 'logs'), { recursive: true });

  if (platform === 'macos') {
    setupLaunchd(projectRoot, nodePath, homeDir);
    setupLogReviewLaunchd(projectRoot, homeDir);
    setupMaintenanceLaunchd(projectRoot, homeDir);
  } else if (platform === 'linux') {
    setupLinux(projectRoot, nodePath, homeDir);
    setupMaintenanceLinux(projectRoot, homeDir);
  } else if (platform === 'windows') {
    setupWindows(projectRoot, nodePath, homeDir);
    setupMaintenanceWindows(projectRoot, homeDir);
  } else {
    emitStatus('SETUP_SERVICE', {
      SERVICE_TYPE: 'unknown',
      NODE_PATH: nodePath,
      PROJECT_PATH: projectRoot,
      STATUS: 'failed',
      ERROR: 'unsupported_platform',
      LOG: 'logs/setup.log',
    });
    process.exit(1);
  }
}

function setupLaunchd(
  projectRoot: string,
  nodePath: string,
  homeDir: string,
): void {
  const plistPath = path.join(
    homeDir,
    'Library',
    'LaunchAgents',
    'com.deus.plist',
  );
  fs.mkdirSync(path.dirname(plistPath), { recursive: true });

  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.deus</string>
    <key>ProgramArguments</key>
    <array>
        <string>${nodePath}</string>
        <string>${projectRoot}/dist/index.js</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${projectRoot}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:${homeDir}/.local/bin</string>
        <key>HOME</key>
        <string>${homeDir}</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${projectRoot}/logs/deus.log</string>
    <key>StandardErrorPath</key>
    <string>${projectRoot}/logs/deus.error.log</string>
</dict>
</plist>`;

  fs.writeFileSync(plistPath, plist);
  logger.info({ plistPath }, 'Wrote launchd plist');

  try {
    execSync(`launchctl load ${JSON.stringify(plistPath)}`, {
      stdio: 'ignore',
    });
    logger.info('launchctl load succeeded');
  } catch {
    logger.warn('launchctl load failed (may already be loaded)');
  }

  // Verify
  let serviceLoaded = false;
  try {
    const output = execSync('launchctl list', { encoding: 'utf-8' });
    serviceLoaded = output.includes('com.deus');
  } catch {
    // launchctl list failed
  }

  emitStatus('SETUP_SERVICE', {
    SERVICE_TYPE: 'launchd',
    NODE_PATH: nodePath,
    PROJECT_PATH: projectRoot,
    PLIST_PATH: plistPath,
    SERVICE_LOADED: serviceLoaded,
    STATUS: 'success',
    LOG: 'logs/setup.log',
  });
}

function setupLogReviewLaunchd(projectRoot: string, homeDir: string): void {
  const pythonPath = (() => {
    for (const bin of ['python3', 'python']) {
      try {
        return execSync(`which ${bin}`, { encoding: 'utf-8' }).trim();
      } catch {
        /* try next */
      }
    }
    return 'python3';
  })();

  const plistPath = path.join(
    homeDir,
    'Library',
    'LaunchAgents',
    'com.deus.log-review.plist',
  );

  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.deus.log-review</string>
    <key>ProgramArguments</key>
    <array>
        <string>${pythonPath}</string>
        <string>${projectRoot}/scripts/log_review.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${projectRoot}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${homeDir}</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${projectRoot}/logs/log-review.log</string>
    <key>StandardErrorPath</key>
    <string>${projectRoot}/logs/log-review.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>`;

  fs.writeFileSync(plistPath, plist);
  try {
    execSync(`launchctl load ${JSON.stringify(plistPath)}`, {
      stdio: 'ignore',
    });
    logger.info({ plistPath }, 'Log review job scheduled (daily 08:00)');
  } catch {
    logger.warn('launchctl load for log-review failed (may already be loaded)');
  }
}

function setupLinux(
  projectRoot: string,
  nodePath: string,
  homeDir: string,
): void {
  const serviceManager = getServiceManager();

  if (serviceManager === 'systemd') {
    setupSystemd(projectRoot, nodePath, homeDir);
  } else {
    // WSL without systemd or other Linux without systemd
    setupNohupFallback(projectRoot, nodePath, homeDir);
  }
}

/**
 * Kill any orphaned deus node processes left from previous runs or debugging.
 * Prevents connection conflicts when two instances connect to the same channel simultaneously.
 */
function killOrphanedProcesses(projectRoot: string): void {
  try {
    if (process.platform === 'win32') {
      // Windows: taskkill by image name (best-effort, may affect other node processes)
      execSync('taskkill /F /IM node.exe /FI "STATUS eq RUNNING" 2>nul', {
        stdio: 'ignore',
      });
    } else {
      execSync(`pkill -f '${projectRoot}/dist/index\\.js' || true`, {
        stdio: 'ignore',
      });
    }
    logger.info('Stopped any orphaned deus processes');
  } catch {
    // pkill/taskkill not available or no orphans
  }
}

/**
 * Detect stale docker group membership in the user systemd session.
 *
 * When a user is added to the docker group mid-session, the user systemd
 * daemon (user@UID.service) keeps the old group list from login time.
 * Docker works in the terminal but not in the service context.
 *
 * Only relevant on Linux with user-level systemd (not root, not macOS, not WSL nohup).
 */
function checkDockerGroupStale(): boolean {
  try {
    execSync('systemd-run --user --pipe --wait docker info', {
      stdio: 'pipe',
      timeout: 10000,
    });
    return false; // Docker works from systemd session
  } catch {
    // Check if docker works from the current shell (to distinguish stale group vs broken docker)
    try {
      execSync('docker info', { stdio: 'pipe', timeout: 5000 });
      return true; // Works in shell but not systemd session → stale group
    } catch {
      return false; // Docker itself is not working, different issue
    }
  }
}

function setupSystemd(
  projectRoot: string,
  nodePath: string,
  homeDir: string,
): void {
  const runningAsRoot = isRoot();

  // Root uses system-level service, non-root uses user-level
  let unitPath: string;
  let systemctlPrefix: string;

  if (runningAsRoot) {
    unitPath = '/etc/systemd/system/deus.service';
    systemctlPrefix = 'systemctl';
    logger.info('Running as root — installing system-level systemd unit');
  } else {
    // Check if user-level systemd session is available
    try {
      execSync('systemctl --user daemon-reload', { stdio: 'pipe' });
    } catch {
      logger.warn(
        'systemd user session not available — falling back to nohup wrapper',
      );
      setupNohupFallback(projectRoot, nodePath, homeDir);
      return;
    }
    const unitDir = path.join(homeDir, '.config', 'systemd', 'user');
    fs.mkdirSync(unitDir, { recursive: true });
    unitPath = path.join(unitDir, 'deus.service');
    systemctlPrefix = 'systemctl --user';
  }

  const unit = `[Unit]
Description=Deus Personal Assistant
After=network.target

[Service]
Type=simple
ExecStart=${nodePath} ${projectRoot}/dist/index.js
WorkingDirectory=${projectRoot}
Restart=always
RestartSec=5
KillMode=process
Environment=HOME=${homeDir}
Environment=PATH=/usr/local/bin:/usr/bin:/bin:${homeDir}/.local/bin
StandardOutput=append:${projectRoot}/logs/deus.log
StandardError=append:${projectRoot}/logs/deus.error.log

[Install]
WantedBy=${runningAsRoot ? 'multi-user.target' : 'default.target'}`;

  fs.writeFileSync(unitPath, unit);
  logger.info({ unitPath }, 'Wrote systemd unit');

  // Detect stale docker group before starting (user systemd only)
  const dockerGroupStale = !runningAsRoot && checkDockerGroupStale();
  if (dockerGroupStale) {
    logger.warn(
      'Docker group not active in systemd session — user was likely added to docker group mid-session',
    );
  }

  // Kill orphaned deus processes to avoid channel connection conflicts
  killOrphanedProcesses(projectRoot);

  // Enable and start
  try {
    execSync(`${systemctlPrefix} daemon-reload`, { stdio: 'ignore' });
  } catch (err) {
    logger.error({ err }, 'systemctl daemon-reload failed');
  }

  try {
    execSync(`${systemctlPrefix} enable deus`, { stdio: 'ignore' });
  } catch (err) {
    logger.error({ err }, 'systemctl enable failed');
  }

  try {
    execSync(`${systemctlPrefix} start deus`, { stdio: 'ignore' });
  } catch (err) {
    logger.error({ err }, 'systemctl start failed');
  }

  // Verify
  let serviceLoaded = false;
  try {
    execSync(`${systemctlPrefix} is-active deus`, { stdio: 'ignore' });
    serviceLoaded = true;
  } catch {
    // Not active
  }

  emitStatus('SETUP_SERVICE', {
    SERVICE_TYPE: runningAsRoot ? 'systemd-system' : 'systemd-user',
    NODE_PATH: nodePath,
    PROJECT_PATH: projectRoot,
    UNIT_PATH: unitPath,
    SERVICE_LOADED: serviceLoaded,
    ...(dockerGroupStale ? { DOCKER_GROUP_STALE: true } : {}),
    STATUS: 'success',
    LOG: 'logs/setup.log',
  });
}

function setupWindows(
  projectRoot: string,
  nodePath: string,
  homeDir: string,
): void {
  const mgr = getServiceManager();

  if (mgr === 'servy') {
    setupServy(projectRoot, nodePath, homeDir);
  } else if (mgr === 'nssm') {
    setupNssm(projectRoot, nodePath, homeDir);
  } else {
    logger.warn(
      'No Windows service manager found (nssm/servy-cli). ' +
        'Generating a batch launcher. Install NSSM (choco install nssm) for auto-start and crash recovery.',
    );
    setupWindowsBatchFallback(projectRoot, nodePath, homeDir);
  }
}

function setupNssm(
  projectRoot: string,
  nodePath: string,
  _homeDir: string,
): void {
  const svc = 'deus';
  const logOut = path.join(projectRoot, 'logs', 'deus.log');
  const logErr = path.join(projectRoot, 'logs', 'deus.error.log');

  // Remove existing service if present (ignore errors — may not exist)
  try {
    execSync(`nssm stop ${svc}`, { stdio: 'ignore', timeout: 10000 });
  } catch {
    /* not running */
  }
  try {
    execSync(`nssm remove ${svc} confirm`, { stdio: 'ignore', timeout: 10000 });
  } catch {
    /* does not exist */
  }

  // Install and configure
  execSync(
    `nssm install ${svc} "${nodePath}" "${path.join(projectRoot, 'dist', 'index.js')}"`,
    { stdio: 'pipe' },
  );
  execSync(`nssm set ${svc} AppDirectory "${projectRoot}"`, { stdio: 'pipe' });
  execSync(`nssm set ${svc} AppStdout "${logOut}"`, { stdio: 'pipe' });
  execSync(`nssm set ${svc} AppStderr "${logErr}"`, { stdio: 'pipe' });
  execSync(`nssm set ${svc} AppRestartDelay 5000`, { stdio: 'pipe' });
  execSync(`nssm set ${svc} Start SERVICE_AUTO_START`, { stdio: 'pipe' });

  logger.info('NSSM service configured');

  try {
    execSync(`nssm start ${svc}`, { stdio: 'pipe', timeout: 15000 });
    logger.info('NSSM service started');
  } catch (err) {
    logger.warn({ err }, 'NSSM start failed — service may need manual start');
  }

  let serviceLoaded = false;
  try {
    const out = execSync(`nssm status ${svc}`, {
      encoding: 'utf-8',
      stdio: 'pipe',
    });
    serviceLoaded = out.trim() === 'SERVICE_RUNNING';
  } catch {
    /* status check failed */
  }

  emitStatus('SETUP_SERVICE', {
    SERVICE_TYPE: 'windows-nssm',
    NODE_PATH: nodePath,
    PROJECT_PATH: projectRoot,
    SERVICE_NAME: svc,
    SERVICE_LOADED: serviceLoaded,
    STATUS: 'success',
    LOG: 'logs/setup.log',
  });
}

function setupServy(
  projectRoot: string,
  nodePath: string,
  _homeDir: string,
): void {
  const svc = 'deus';
  const logOut = path.join(projectRoot, 'logs', 'deus.log');
  const logErr = path.join(projectRoot, 'logs', 'deus.error.log');

  // Remove existing service if present
  try {
    execSync(`servy-cli stop --name="${svc}" --quiet`, {
      stdio: 'ignore',
      timeout: 10000,
    });
  } catch {
    /* not running */
  }
  try {
    execSync(`servy-cli uninstall --name="${svc}" --quiet`, {
      stdio: 'ignore',
      timeout: 10000,
    });
  } catch {
    /* does not exist */
  }

  // Install with crash recovery via health monitor
  execSync(
    [
      `servy-cli install`,
      `--name="${svc}"`,
      `--path="${nodePath}"`,
      `--params="${path.join(projectRoot, 'dist', 'index.js')}"`,
      `--startupDir="${projectRoot}"`,
      `--startupType="Automatic"`,
      `--stdout="${logOut}"`,
      `--stderr="${logErr}"`,
      `--enableHealth`,
      `--recoveryAction="RestartService"`,
      `--maxRestartAttempts=5`,
      `--quiet`,
    ].join(' '),
    { stdio: 'pipe' },
  );

  logger.info('Servy service configured');

  try {
    execSync(`servy-cli start --name="${svc}" --quiet`, {
      stdio: 'pipe',
      timeout: 15000,
    });
    logger.info('Servy service started');
  } catch (err) {
    logger.warn({ err }, 'Servy start failed — service may need manual start');
  }

  let serviceLoaded = false;
  try {
    const out = execSync(`servy-cli status --name="${svc}"`, {
      encoding: 'utf-8',
      stdio: 'pipe',
    });
    serviceLoaded = out.trim() === 'Running';
  } catch {
    /* status check failed */
  }

  emitStatus('SETUP_SERVICE', {
    SERVICE_TYPE: 'windows-servy',
    NODE_PATH: nodePath,
    PROJECT_PATH: projectRoot,
    SERVICE_NAME: svc,
    SERVICE_LOADED: serviceLoaded,
    STATUS: 'success',
    LOG: 'logs/setup.log',
  });
}

function setupWindowsBatchFallback(
  projectRoot: string,
  nodePath: string,
  _homeDir: string,
): void {
  const batPath = path.join(projectRoot, 'start-deus.bat');
  const logOut = path.join(projectRoot, 'logs', 'deus.log');
  const logErr = path.join(projectRoot, 'logs', 'deus.error.log');

  const lines = [
    '@echo off',
    `cd /d "${projectRoot}"`,
    `start /B "" "${nodePath}" "${path.join(projectRoot, 'dist', 'index.js')}" >> "${logOut}" 2>> "${logErr}"`,
    'echo Deus started.',
    `echo Logs: ${logOut}`,
    `echo To stop: taskkill /IM node.exe /FI "WINDOWTITLE eq deus*"`,
  ];
  fs.writeFileSync(batPath, lines.join('\r\n') + '\r\n');
  logger.info({ batPath }, 'Wrote Windows batch launcher (no crash recovery)');

  emitStatus('SETUP_SERVICE', {
    SERVICE_TYPE: 'windows-batch',
    NODE_PATH: nodePath,
    PROJECT_PATH: projectRoot,
    WRAPPER_PATH: batPath,
    SERVICE_LOADED: false,
    FALLBACK: 'no_service_manager',
    STATUS: 'success',
    LOG: 'logs/setup.log',
  });
}

function setupNohupFallback(
  projectRoot: string,
  nodePath: string,
  homeDir: string,
): void {
  logger.warn('No systemd detected — generating nohup wrapper script');

  const wrapperPath = path.join(projectRoot, 'start-deus.sh');
  const pidFile = path.join(projectRoot, 'deus.pid');

  const lines = [
    '#!/bin/bash',
    '# start-deus.sh — Start Deus without systemd',
    `# To stop: kill \\$(cat ${pidFile})`,
    '',
    'set -euo pipefail',
    '',
    `cd ${JSON.stringify(projectRoot)}`,
    '',
    '# Stop existing instance if running',
    `if [ -f ${JSON.stringify(pidFile)} ]; then`,
    `  OLD_PID=$(cat ${JSON.stringify(pidFile)} 2>/dev/null || echo "")`,
    '  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then',
    '    echo "Stopping existing Deus (PID $OLD_PID)..."',
    '    kill "$OLD_PID" 2>/dev/null || true',
    '    sleep 2',
    '  fi',
    'fi',
    '',
    'echo "Starting Deus..."',
    `nohup ${JSON.stringify(nodePath)} ${JSON.stringify(projectRoot + '/dist/index.js')} \\`,
    `  >> ${JSON.stringify(projectRoot + '/logs/deus.log')} \\`,
    `  2>> ${JSON.stringify(projectRoot + '/logs/deus.error.log')} &`,
    '',
    `echo $! > ${JSON.stringify(pidFile)}`,
    'echo "Deus started (PID $!)"',
    `echo "Logs: tail -f ${projectRoot}/logs/deus.log"`,
  ];
  const wrapper = lines.join('\n') + '\n';

  fs.writeFileSync(wrapperPath, wrapper, { mode: 0o755 });
  logger.info({ wrapperPath }, 'Wrote nohup wrapper script');

  emitStatus('SETUP_SERVICE', {
    SERVICE_TYPE: 'nohup',
    NODE_PATH: nodePath,
    PROJECT_PATH: projectRoot,
    WRAPPER_PATH: wrapperPath,
    SERVICE_LOADED: false,
    FALLBACK: 'wsl_no_systemd',
    STATUS: 'success',
    LOG: 'logs/setup.log',
  });
}

// ── Maintenance service setup (all platforms) ─────────────────────────────

function getPythonPath(): string {
  for (const bin of ['python3', 'python']) {
    try {
      return execSync(`command -v ${bin}`, { encoding: 'utf-8' }).trim();
    } catch {
      /* try next */
    }
  }
  return 'python3';
}

function getWindowsPythonPath(): string {
  try {
    return execSync('where python3', { encoding: 'utf-8' }).trim().split('\n')[0].trim();
  } catch {
    try {
      return execSync('where python', { encoding: 'utf-8' }).trim().split('\n')[0].trim();
    } catch {
      return 'python3';
    }
  }
}

function setupMaintenanceLaunchd(projectRoot: string, homeDir: string): void {
  const pythonPath = getPythonPath();
  const plistPath = path.join(
    homeDir,
    'Library',
    'LaunchAgents',
    'com.deus.maintenance.plist',
  );

  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.deus.maintenance</string>
    <key>ProgramArguments</key>
    <array>
        <string>${pythonPath}</string>
        <string>${projectRoot}/scripts/maintenance.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>${projectRoot}</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>4</integer>
        <key>Minute</key>
        <integer>30</integer>
    </dict>
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${homeDir}</string>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
    <key>StandardOutPath</key>
    <string>${projectRoot}/logs/maintenance.log</string>
    <key>StandardErrorPath</key>
    <string>${projectRoot}/logs/maintenance.log</string>
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>`;

  fs.writeFileSync(plistPath, plist);
  try {
    execSync(`launchctl load ${JSON.stringify(plistPath)}`, {
      stdio: 'ignore',
    });
    logger.info({ plistPath }, 'Maintenance job scheduled (daily 04:30)');
  } catch {
    logger.warn('launchctl load for maintenance failed (may already be loaded)');
  }
}

function setupMaintenanceLinux(
  projectRoot: string,
  homeDir: string,
): void {
  const serviceManager = getServiceManager();
  if (serviceManager !== 'systemd') {
    logger.info('No systemd — skipping maintenance timer (run scripts/maintenance.py manually or via cron)');
    return;
  }

  const pythonPath = getPythonPath();
  const runningAsRoot = isRoot();
  const unitDir = runningAsRoot
    ? '/etc/systemd/system'
    : path.join(homeDir, '.config', 'systemd', 'user');
  const systemctlPrefix = runningAsRoot ? 'systemctl' : 'systemctl --user';

  fs.mkdirSync(unitDir, { recursive: true });

  // Service unit
  const serviceUnit = `[Unit]
Description=Deus KB maintenance

[Service]
Type=oneshot
ExecStart=${pythonPath} ${projectRoot}/scripts/maintenance.py
WorkingDirectory=${projectRoot}
Environment=HOME=${homeDir}
Environment=PATH=/usr/local/bin:/usr/bin:/bin:${homeDir}/.local/bin
StandardOutput=append:${projectRoot}/logs/maintenance.log
StandardError=append:${projectRoot}/logs/maintenance.log`;

  // Timer unit
  const timerUnit = `[Unit]
Description=Deus KB maintenance timer

[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true

[Install]
WantedBy=timers.target`;

  fs.writeFileSync(path.join(unitDir, 'deus-maintenance.service'), serviceUnit);
  fs.writeFileSync(path.join(unitDir, 'deus-maintenance.timer'), timerUnit);

  try {
    execSync(`${systemctlPrefix} daemon-reload`, { stdio: 'ignore' });
    execSync(`${systemctlPrefix} enable deus-maintenance.timer`, { stdio: 'ignore' });
    execSync(`${systemctlPrefix} start deus-maintenance.timer`, { stdio: 'ignore' });
    logger.info('Maintenance timer scheduled (daily 04:30)');
  } catch {
    logger.warn('systemd maintenance timer setup failed');
  }
}

function setupMaintenanceWindows(
  projectRoot: string,
  _homeDir: string,
): void {
  const pythonPath = getWindowsPythonPath();
  const taskName = 'DeusMaintenance';

  try {
    // Delete existing task if present
    execSync(`schtasks /Delete /TN "${taskName}" /F`, { stdio: 'ignore' });
  } catch {
    /* does not exist */
  }

  try {
    execSync(
      `schtasks /Create /TN "${taskName}" /TR "${pythonPath} ${projectRoot}\\scripts\\maintenance.py" /SC DAILY /ST 04:30 /F`,
      { stdio: 'pipe' },
    );
    logger.info('Windows Task Scheduler: maintenance scheduled (daily 04:30)');
  } catch (err) {
    logger.warn({ err }, 'Windows Task Scheduler setup failed — run scripts/maintenance.py manually');
  }
}
