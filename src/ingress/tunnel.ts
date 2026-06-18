// Owns the ngrok tunnel as a CHILD PROCESS of this host process — a single
// centralized point that starts and supervises ingress. This replaces the
// macOS-only `com.deus.ngrok` launchd agent with a cross-platform child
// (spawn works on macOS/Linux/Windows/WSL), and couples the tunnel's lifetime
// to Deus's: if Deus is down the webhook is unreachable anyway, so there is no
// value in an independently-managed tunnel.
//
// Only the pure helpers (arg building, URL extraction, conflict detection) are
// unit-tested; the live spawn/supervise loop is exercised by the Phase-4 e2e
// and is gated behind INGRESS_TUNNEL_ENABLED (off by default), so it never runs
// in CI.

import { spawn, type ChildProcess } from 'child_process';
import http from 'http';
import { logger } from '../logger.js';
import { killProcess } from '../platform.js';

export interface TunnelDeps {
  /** Local port to forward to — always the ingress gateway port. */
  localPort: number;
  /** Static reserved domain (hostname or URL). Empty = ephemeral. */
  staticDomain?: string;
  /** ngrok authtoken. Empty = rely on ngrok.yml. */
  authtoken?: string;
  /** Max time to wait for the public URL before failing closed. */
  startTimeoutMs?: number;
}

export interface TunnelHandle {
  publicUrl: string;
  stop(): void;
}

const NGROK_API = 'http://127.0.0.1:4040/api/tunnels';

/** Build ngrok CLI args. Pure — unit-tested. */
export function buildNgrokArgs(
  localPort: number,
  staticDomain?: string,
  authtoken?: string,
): string[] {
  const args = [
    'http',
    String(localPort),
    '--log',
    'stdout',
    '--log-format',
    'json',
  ];
  if (staticDomain) {
    const host = staticDomain.replace(/^https?:\/\//, '');
    args.push('--url', `https://${host}`);
  }
  if (authtoken) args.push('--authtoken', authtoken);
  return args;
}

/** Extract the https public URL from ngrok's /api/tunnels payload. Pure. */
export function extractPublicUrl(api: unknown): string | null {
  if (!api || typeof api !== 'object') return null;
  const tunnels = (api as { tunnels?: unknown }).tunnels;
  if (!Array.isArray(tunnels)) return null;
  const https = tunnels.find(
    (t) =>
      t && typeof t === 'object' && (t as { proto?: string }).proto === 'https',
  ) as { public_url?: string } | undefined;
  const any = tunnels[0] as { public_url?: string } | undefined;
  return https?.public_url ?? any?.public_url ?? null;
}

/** GET ngrok's local API; resolves null if nothing is listening on :4040. */
function fetchTunnelsApi(): Promise<unknown> {
  return new Promise((resolve) => {
    const req = http.get(NGROK_API, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        try {
          resolve(JSON.parse(Buffer.concat(chunks).toString('utf-8')));
        } catch {
          resolve(null);
        }
      });
    });
    req.on('error', () => resolve(null));
    req.setTimeout(1500, () => {
      req.destroy();
      resolve(null);
    });
  });
}

const sleep = (ms: number): Promise<void> =>
  new Promise((r) => setTimeout(r, ms));

/**
 * Start ngrok and resolve once the public URL is known. Fails CLOSED:
 *  - a foreign ngrok already on :4040 (e.g. the legacy launchd agent) → throw
 *    with an actionable unload hint, rather than silently sharing the session;
 *  - ngrok binary missing (ENOENT) → throw;
 *  - URL not resolved within the timeout → throw.
 * Supervises a single restart on unexpected exit (not on an auth-error exit).
 */
export async function startTunnel(deps: TunnelDeps): Promise<TunnelHandle> {
  const timeoutMs = deps.startTimeoutMs ?? 15_000;

  // Pre-flight: detect a foreign ngrok holding :4040 (free tier = 1 session).
  const existing = await fetchTunnelsApi();
  if (existing !== null) {
    throw new Error(
      'ingress-tunnel: ngrok API already responding on :4040 — another ngrok ' +
        'is running. Stop it first (macOS launchd: launchctl unload ' +
        '~/Library/LaunchAgents/com.deus.ngrok.plist; Linux/WSL: kill $(pgrep ngrok)).',
    );
  }

  const args = buildNgrokArgs(
    deps.localPort,
    deps.staticDomain,
    deps.authtoken,
  );
  let restarts = 0;
  let stopped = false;
  let proc: ChildProcess;

  const spawnNgrok = (): ChildProcess => {
    const p = spawn('ngrok', args, { stdio: ['ignore', 'pipe', 'pipe'] });
    p.stdout?.on('data', (d) =>
      logger.debug({ ngrok: String(d).trim() }, 'ngrok'),
    );
    p.stderr?.on('data', (d) =>
      logger.warn({ ngrok: String(d).trim() }, 'ngrok'),
    );
    p.on('error', (err) =>
      logger.error({ err }, 'ingress-tunnel: spawn error'),
    );
    p.on('exit', (code) => {
      if (stopped) return;
      // Restart at most once, regardless of cause: a clean exit (code 0) or a
      // second failure is not retried — an auth/config error would otherwise loop.
      if (code === 0 || restarts >= 1) {
        logger.error({ code }, 'ingress-tunnel: ngrok exited, not restarting');
        return;
      }
      restarts += 1;
      logger.warn({ code }, 'ingress-tunnel: ngrok exited, restarting once');
      proc = spawnNgrok();
    });
    return p;
  };
  proc = spawnNgrok();

  // Poll the local API for the assigned URL.
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await sleep(500);
    const url = extractPublicUrl(await fetchTunnelsApi());
    if (url) {
      logger.info({ url }, 'ingress-tunnel: tunnel up');
      return {
        publicUrl: url,
        stop: () => {
          stopped = true;
          if (proc.pid) killProcess(proc.pid);
        },
      };
    }
  }

  stopped = true;
  if (proc.pid) killProcess(proc.pid);
  throw new Error(
    `ingress-tunnel: ngrok URL not available within ${timeoutMs}ms ` +
      '(is the ngrok binary installed and authed?)',
  );
}
