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

/** Absolute wall-clock budget for the whole :4040 probe. */
const PROBE_TIMEOUT_MS = 1500;

/** Body bytes to buffer before giving up on parsing (ngrok's payload is tiny). */
const MAX_PROBE_BODY_BYTES = 1024 * 1024;

/**
 * Outcome of probing :4040. Only `free` permits starting our own ngrok — the
 * pre-flight is a fail-closed check, so anything short of proof that the port
 * is unoccupied must block.
 */
export type PortProbe =
  | { state: 'free' }
  | { state: 'occupied'; api: unknown }
  | { state: 'indeterminate'; detail: string };

/**
 * Classify a failed request to ngrok's local API. Pure, so the fail-closed
 * policy is unit-testable without a socket.
 *
 * NGROK_API is a literal IPv4 loopback address with no DNS lookup, so a port
 * with no listener answers ECONNREFUSED essentially immediately. That makes
 * ECONNREFUSED the ONLY evidence that the port is genuinely free. Every other
 * error code means something engaged with the socket and then failed (e.g. a
 * live listener that accepts and then resets), which is not proof of absence.
 */
export function classifyProbeError(code: string | undefined): PortProbe {
  if (code === 'ECONNREFUSED') return { state: 'free' };
  return {
    state: 'indeterminate',
    detail: `request failed with ${code ?? 'an unknown error'}`,
  };
}

/**
 * Probe ngrok's local API on :4040.
 *
 * Any HTTP response at all means something is listening — including one whose
 * body does not parse, which previously read as "free" and let a second ngrok
 * launch into a session conflict.
 */
export function probeNgrokApi(apiUrl: string = NGROK_API): Promise<PortProbe> {
  return new Promise((resolve) => {
    // Response headers are all the pre-flight actually needs: if anything
    // answered, the port is taken. The body is only read so the post-spawn URL
    // poll can reuse this probe.
    let sawHeaders = false;
    let settled = false;
    // Assigned below, once `req` exists; `finish` only reads it at call time.
    let deadline: NodeJS.Timeout | undefined = undefined;

    /** Once headers arrived the port is occupied, whatever happened next. */
    const givenHeaders = (detail: string): PortProbe =>
      sawHeaders
        ? { state: 'occupied', api: null }
        : { state: 'indeterminate', detail };

    const finish = (probe: PortProbe): void => {
      if (settled) return;
      settled = true;
      if (deadline) clearTimeout(deadline);
      req.destroy();
      resolve(probe);
    };

    const req = http.get(apiUrl, (res) => {
      sawHeaders = true;
      const chunks: Buffer[] = [];
      let bytes = 0;
      res.on('data', (c: Buffer) => {
        bytes += c.length;
        if (bytes > MAX_PROBE_BODY_BYTES) {
          // Occupied beyond doubt — we simply cannot use this body.
          finish({ state: 'occupied', api: null });
          return;
        }
        chunks.push(c);
      });
      res.on('end', () => {
        try {
          finish({
            state: 'occupied',
            api: JSON.parse(Buffer.concat(chunks).toString('utf-8')),
          });
        } catch {
          // A response that is not JSON still proves the port is taken.
          finish({ state: 'occupied', api: null });
        }
      });
      // An unhandled 'error' on the response would otherwise throw.
      const aborted = (): void =>
        finish(givenHeaders('response aborted before the body completed'));
      res.on('error', aborted);
      res.on('aborted', aborted);
    });

    req.on('error', (err: NodeJS.ErrnoException) =>
      finish(
        sawHeaders
          ? { state: 'occupied', api: null }
          : classifyProbeError(err.code),
      ),
    );
    // 'close' always fires once the request finishes, for any reason.
    req.on('close', () =>
      finish(givenHeaders('connection closed before a usable response')),
    );

    // An ABSOLUTE deadline, deliberately not req.setTimeout: that one measures
    // socket INACTIVITY, so a peer streaming chunks forever would keep resetting
    // it while 'end'/'aborted'/'error'/'close' never fire — hanging the
    // pre-flight, and with it startup.
    deadline = setTimeout(() => {
      finish(givenHeaders(`no usable response within ${PROBE_TIMEOUT_MS}ms`));
    }, PROBE_TIMEOUT_MS);
  });
}

const sleep = (ms: number): Promise<void> =>
  new Promise((r) => setTimeout(r, ms));

/**
 * Start ngrok and resolve once the public URL is known. Fails CLOSED:
 *  - anything already answering on :4040 (e.g. the legacy launchd agent) → throw
 *    with an actionable unload hint, rather than silently sharing the session;
 *  - :4040 not provably free — a timeout, or any request error other than
 *    ECONNREFUSED → throw, since only a refused connection proves no listener;
 *  - ngrok binary missing (ENOENT) → throw;
 *  - URL not resolved within the timeout → throw.
 * Supervises a single restart on unexpected exit (not on an auth-error exit).
 */
export async function startTunnel(deps: TunnelDeps): Promise<TunnelHandle> {
  const timeoutMs = deps.startTimeoutMs ?? 15_000;

  // Pre-flight: detect a foreign ngrok holding :4040 (free tier = 1 session).
  // Fail closed — proceed only on positive proof that the port is free.
  const probe = await probeNgrokApi();
  if (probe.state === 'occupied') {
    throw new Error(
      'ingress-tunnel: something is already responding on :4040 — another ngrok ' +
        'is likely running. Stop it first (macOS launchd: launchctl unload ' +
        '~/Library/LaunchAgents/com.deus.ngrok.plist; Linux/WSL: kill $(pgrep ngrok)).',
    );
  }
  if (probe.state === 'indeterminate') {
    throw new Error(
      `ingress-tunnel: could not confirm :4040 is free (${probe.detail}). ` +
        'Refusing to start a second ngrok, because a free loopback port refuses ' +
        'the connection immediately — anything else suggests a process is holding it. ' +
        'Check what owns :4040 (macOS/Linux: lsof -nP -iTCP:4040 -sTCP:LISTEN).',
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
    // Here we want the payload if our own ngrok has come up yet, so anything
    // other than a parsed response simply means "not ready, keep polling".
    // This is deliberately NOT the fail-closed reading used by the pre-flight.
    const probed = await probeNgrokApi();
    const url = extractPublicUrl(
      probed.state === 'occupied' ? probed.api : null,
    );
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
