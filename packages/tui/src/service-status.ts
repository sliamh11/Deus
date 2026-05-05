import { execFileSync } from 'child_process';
import { existsSync, statSync, readFileSync } from 'fs';
import { join } from 'path';
import { homedir } from 'os';

// TODO: migrate to platform.ts Service API when project_windows_sot_plan Phase 2 lands
const IS_MACOS = process.platform === 'darwin';

export interface ServiceStatus {
  label: string;
  description: string;
  status: 'running' | 'stopped' | 'stale' | 'unsupported' | 'unknown';
  detail?: string;
}

interface HealthcheckJob {
  label: string;
  description: string;
  check: string;
  heartbeat_path?: string;
  max_staleness_sec?: number;
}

function checkLaunchctl(label: string): 'running' | 'stopped' | 'unknown' {
  if (!IS_MACOS) return 'unsupported' as 'unknown';
  try {
    const uid = process.getuid?.() ?? 501;
    const output = execFileSync('launchctl', ['print', `gui/${uid}/${label}`], {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    if (output.includes('state = running')) return 'running';
    return 'stopped';
  } catch {
    return 'stopped';
  }
}

function checkHeartbeat(
  path: string,
  maxStaleness: number,
): 'running' | 'stale' | 'stopped' {
  if (!existsSync(path)) return 'stopped';
  const mtime = statSync(path).mtimeMs;
  const age = (Date.now() - mtime) / 1000;
  return age <= maxStaleness ? 'running' : 'stale';
}

export function getServiceStatuses(): ServiceStatus[] {
  if (!IS_MACOS) {
    return [
      {
        label: 'platform',
        description: 'Service status',
        status: 'unsupported',
        detail: 'Not supported on this platform',
      },
    ];
  }

  const configPath = join(homedir(), '.config', 'deus', 'healthcheck.json');
  if (!existsSync(configPath)) {
    return [
      {
        label: 'deus',
        description: 'Main service',
        status: 'unknown',
        detail: 'healthcheck.json not found',
      },
    ];
  }

  try {
    const jobs: HealthcheckJob[] =
      JSON.parse(readFileSync(configPath, 'utf-8')).jobs ?? [];
    return jobs.map((job) => {
      let status: ServiceStatus['status'];
      let detail: string | undefined;

      if (job.check === 'loaded_and_running') {
        status = checkLaunchctl(job.label);
      } else if (job.check === 'heartbeat' && job.heartbeat_path) {
        const resolved = job.heartbeat_path.replace(/^~/, homedir());
        status = checkHeartbeat(resolved, job.max_staleness_sec ?? 300);
        if (status === 'stale') {
          const age = Math.round(
            (Date.now() - statSync(resolved).mtimeMs) / 60000,
          );
          detail = `heartbeat ${age}m ago`;
        }
      } else {
        status = 'unknown';
      }

      return { label: job.label, description: job.description, status, detail };
    });
  } catch {
    return [
      {
        label: 'deus',
        description: 'Main service',
        status: 'unknown',
        detail: 'Failed to read healthcheck.json',
      },
    ];
  }
}
