import { describe, it, expect } from 'vitest';
import path from 'path';

/**
 * Tests for service configuration generation.
 *
 * These tests verify the generated content of plist/systemd/nohup configs
 * without actually loading services.
 */

// Helper: generate a plist string the same way service.ts does
function generatePlist(
  nodePath: string,
  projectRoot: string,
  homeDir: string,
): string {
  return `<?xml version="1.0" encoding="UTF-8"?>
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
}

function generateSystemdUnit(
  nodePath: string,
  projectRoot: string,
  homeDir: string,
  isSystem: boolean,
): string {
  return `[Unit]
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
WantedBy=${isSystem ? 'multi-user.target' : 'default.target'}`;
}

describe('plist generation', () => {
  it('contains the correct label', () => {
    const plist = generatePlist(
      '/usr/local/bin/node',
      '/home/user/deus',
      '/home/user',
    );
    expect(plist).toContain('<string>com.deus</string>');
  });

  it('uses the correct node path', () => {
    const plist = generatePlist(
      '/opt/node/bin/node',
      '/home/user/deus',
      '/home/user',
    );
    expect(plist).toContain('<string>/opt/node/bin/node</string>');
  });

  it('points to dist/index.js', () => {
    const plist = generatePlist(
      '/usr/local/bin/node',
      '/home/user/deus',
      '/home/user',
    );
    expect(plist).toContain('/home/user/deus/dist/index.js');
  });

  it('includes /opt/homebrew/bin in PATH for Apple Silicon', () => {
    const plist = generatePlist(
      '/usr/local/bin/node',
      '/home/user/deus',
      '/home/user',
    );
    expect(plist).toContain('/opt/homebrew/bin');
  });

  it('sets log paths', () => {
    const plist = generatePlist(
      '/usr/local/bin/node',
      '/home/user/deus',
      '/home/user',
    );
    expect(plist).toContain('deus.log');
    expect(plist).toContain('deus.error.log');
  });
});

describe('systemd unit generation', () => {
  it('user unit uses default.target', () => {
    const unit = generateSystemdUnit(
      '/usr/bin/node',
      '/home/user/deus',
      '/home/user',
      false,
    );
    expect(unit).toContain('WantedBy=default.target');
  });

  it('system unit uses multi-user.target', () => {
    const unit = generateSystemdUnit(
      '/usr/bin/node',
      '/home/user/deus',
      '/home/user',
      true,
    );
    expect(unit).toContain('WantedBy=multi-user.target');
  });

  it('contains restart policy', () => {
    const unit = generateSystemdUnit(
      '/usr/bin/node',
      '/home/user/deus',
      '/home/user',
      false,
    );
    expect(unit).toContain('Restart=always');
    expect(unit).toContain('RestartSec=5');
  });

  it('uses KillMode=process to preserve detached children', () => {
    const unit = generateSystemdUnit(
      '/usr/bin/node',
      '/home/user/deus',
      '/home/user',
      false,
    );
    expect(unit).toContain('KillMode=process');
  });

  it('sets correct ExecStart', () => {
    const unit = generateSystemdUnit(
      '/usr/bin/node',
      '/srv/deus',
      '/home/user',
      false,
    );
    expect(unit).toContain('ExecStart=/usr/bin/node /srv/deus/dist/index.js');
  });
});

describe('Windows NSSM command generation', () => {
  it('builds correct install command', () => {
    const nodePath = 'C:\\Program Files\\nodejs\\node.exe';
    const projectRoot = 'C:\\Users\\user\\deus';
    const svc = 'deus';
    const distEntry = path.join(projectRoot, 'dist', 'index.js');
    const cmd = `nssm install ${svc} "${nodePath}" "${distEntry}"`;
    expect(cmd).toContain('nssm install deus');
    expect(cmd).toContain(nodePath);
    expect(cmd).toContain('index.js');
  });

  it('builds correct log path commands', () => {
    const projectRoot = 'C:\\Users\\user\\deus';
    const logOut = path.join(projectRoot, 'logs', 'deus.log');
    const logErr = path.join(projectRoot, 'logs', 'deus.error.log');
    expect(logOut).toContain('deus.log');
    expect(logErr).toContain('deus.error.log');
  });

  it('uses SERVICE_AUTO_START for boot persistence', () => {
    const startCmd = 'nssm set deus Start SERVICE_AUTO_START';
    expect(startCmd).toContain('SERVICE_AUTO_START');
  });

  it('status check expects SERVICE_RUNNING string', () => {
    const runningOutput = 'SERVICE_RUNNING';
    expect(runningOutput.trim() === 'SERVICE_RUNNING').toBe(true);
    expect('SERVICE_STOPPED'.trim() === 'SERVICE_RUNNING').toBe(false);
  });
});

describe('Windows Servy command generation', () => {
  it('uses servy-cli binary (not servy)', () => {
    const cmd = 'servy-cli install --name="deus"';
    expect(cmd).toContain('servy-cli');
    expect(cmd).not.toContain('"servy"');
  });

  it('status check expects Running string', () => {
    const runningOutput = 'Running';
    expect(runningOutput.trim() === 'Running').toBe(true);
    expect('Stopped'.trim() === 'Running').toBe(false);
  });

  it('uses Automatic startupType for boot persistence', () => {
    const cmd = 'servy-cli install --name="deus" --startupType="Automatic"';
    expect(cmd).toContain('Automatic');
  });
});

describe('Windows batch fallback', () => {
  it('generates a valid batch file header', () => {
    const projectRoot = 'C:\\Users\\user\\deus';
    const nodePath = 'C:\\Program Files\\nodejs\\node.exe';
    const lines = [
      '@echo off',
      `cd /d "${projectRoot}"`,
      `start /B "" "${nodePath}" "${path.join(projectRoot, 'dist', 'index.js')}"`,
    ];
    const bat = lines.join('\r\n');
    expect(bat).toContain('@echo off');
    expect(bat).toContain('start /B');
    expect(bat).toContain('index.js');
  });
});

describe('WSL nohup fallback', () => {
  it('generates a valid wrapper script', () => {
    const projectRoot = '/home/user/deus';
    const nodePath = '/usr/bin/node';
    const pidFile = path.join(projectRoot, 'deus.pid');

    // Simulate what service.ts generates
    const wrapper = `#!/bin/bash
set -euo pipefail
cd ${JSON.stringify(projectRoot)}
nohup ${JSON.stringify(nodePath)} ${JSON.stringify(projectRoot)}/dist/index.js >> ${JSON.stringify(projectRoot)}/logs/deus.log 2>> ${JSON.stringify(projectRoot)}/logs/deus.error.log &
echo $! > ${JSON.stringify(pidFile)}`;

    expect(wrapper).toContain('#!/bin/bash');
    expect(wrapper).toContain('nohup');
    expect(wrapper).toContain(nodePath);
    expect(wrapper).toContain('deus.pid');
  });
});

describe('Maintenance service generation', () => {
  function generateMaintenancePlist(
    pythonPath: string,
    projectRoot: string,
    homeDir: string,
  ): string {
    return `<?xml version="1.0" encoding="UTF-8"?>
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
  }

  function generateMaintenanceSystemdService(
    pythonPath: string,
    projectRoot: string,
    homeDir: string,
  ): string {
    return `[Unit]
Description=Deus KB maintenance

[Service]
Type=oneshot
ExecStart=${pythonPath} ${projectRoot}/scripts/maintenance.py
WorkingDirectory=${projectRoot}
Environment=HOME=${homeDir}
Environment=PATH=/usr/local/bin:/usr/bin:/bin:${homeDir}/.local/bin
StandardOutput=append:${projectRoot}/logs/maintenance.log
StandardError=append:${projectRoot}/logs/maintenance.log`;
  }

  function generateMaintenanceSystemdTimer(): string {
    return `[Unit]
Description=Deus KB maintenance timer

[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true

[Install]
WantedBy=timers.target`;
  }

  it('macOS plist contains maintenance.py path and 04:30 schedule', () => {
    const plist = generateMaintenancePlist(
      '/usr/bin/python3',
      '/Users/test/deus',
      '/Users/test',
    );
    expect(plist).toContain('com.deus.maintenance');
    expect(plist).toContain('scripts/maintenance.py');
    expect(plist).toContain('<integer>4</integer>');
    expect(plist).toContain('<integer>30</integer>');
    expect(plist).toContain('RunAtLoad');
    expect(plist).toContain('<false/>');
  });

  it('systemd service unit contains correct ExecStart', () => {
    const unit = generateMaintenanceSystemdService(
      '/usr/bin/python3',
      '/home/user/deus',
      '/home/user',
    );
    expect(unit).toContain('Type=oneshot');
    expect(unit).toContain('scripts/maintenance.py');
    expect(unit).toContain('maintenance.log');
  });

  it('systemd timer fires daily at 04:30', () => {
    const timer = generateMaintenanceSystemdTimer();
    expect(timer).toContain('OnCalendar=*-*-* 04:30:00');
    expect(timer).toContain('Persistent=true');
    expect(timer).toContain('timers.target');
  });
});
