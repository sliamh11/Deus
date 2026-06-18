import { describe, it, expect, afterEach } from 'vitest';
import type { AddressInfo } from 'net';
import type { Server } from 'http';
import {
  startIngressGateway,
  type IngressHandler,
  type GatewayConfig,
} from './gateway.js';

const baseConfig: GatewayConfig = {
  host: '127.0.0.1',
  port: 0, // ephemeral
  maxBodyBytes: 1024,
  rateLimitMax: 100,
  rateLimitWindowMs: 60_000,
  ipAllowlist: [],
};

let server: Server | undefined;
afterEach(() => {
  server?.close();
  server = undefined;
});

async function start(
  handlers: IngressHandler[],
  cfg: Partial<GatewayConfig> = {},
): Promise<string> {
  server = await startIngressGateway({
    handlers,
    config: { ...baseConfig, ...cfg },
  });
  const { port } = server.address() as AddressInfo;
  return `http://127.0.0.1:${port}`;
}

const okHandler: IngressHandler = {
  pathPrefix: '/hook',
  verify: () => ({ ok: true }),
  handle: async (_req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ handled: true }));
  },
};

describe('ingress gateway', () => {
  it('serves GET /health without a handler', async () => {
    const base = await start([]);
    const r = await fetch(`${base}/health`);
    expect(r.status).toBe(200);
  });

  it('404s an unrouted path', async () => {
    const base = await start([okHandler]);
    const r = await fetch(`${base}/nope`, { method: 'POST', body: '{}' });
    expect(r.status).toBe(404);
  });

  it('routes a matching prefix to the handler when verify passes', async () => {
    const base = await start([okHandler]);
    const r = await fetch(`${base}/hook/github`, {
      method: 'POST',
      body: '{}',
    });
    expect(r.status).toBe(200);
    expect(await r.json()).toEqual({ handled: true });
  });

  it('403s when the handler verify fails (no body to handler)', async () => {
    let handled = false;
    const base = await start([
      {
        pathPrefix: '/hook',
        verify: () => ({ ok: false, reason: 'bad signature' }),
        handle: async () => {
          handled = true;
        },
      },
    ]);
    const r = await fetch(`${base}/hook/x`, { method: 'POST', body: '{}' });
    expect(r.status).toBe(403);
    expect(handled).toBe(false);
  });

  it('413s a body over the cap', async () => {
    const base = await start([okHandler], { maxBodyBytes: 16 });
    const r = await fetch(`${base}/hook/x`, {
      method: 'POST',
      body: 'x'.repeat(64),
    });
    expect(r.status).toBe(413);
  });

  it('429s once the per-IP rate limit is exceeded', async () => {
    const base = await start([okHandler], { rateLimitMax: 2 });
    const a = await fetch(`${base}/health`);
    const b = await fetch(`${base}/health`);
    const c = await fetch(`${base}/health`);
    expect([a.status, b.status]).toEqual([200, 200]);
    expect(c.status).toBe(429);
  });

  it('403s a peer not on the allowlist, and XFF cannot spoof past it', async () => {
    const base = await start([okHandler], { ipAllowlist: ['9.9.9.9'] });
    // Loopback peer is not 9.9.9.9 → denied even with a spoofed X-Forwarded-For.
    const denied = await fetch(`${base}/hook/x`, {
      method: 'POST',
      body: '{}',
      headers: { 'X-Forwarded-For': '9.9.9.9' },
    });
    expect(denied.status).toBe(403);
  });

  it('passes a peer that is on the allowlist', async () => {
    const base = await start([okHandler], {
      ipAllowlist: ['127.0.0.1', '::1', '::ffff:127.0.0.1'],
    });
    const r = await fetch(`${base}/hook/x`, { method: 'POST', body: '{}' });
    expect(r.status).toBe(200);
  });

  it('passes the raw body to verify+handle', async () => {
    let seen = '';
    const base = await start([
      {
        pathPrefix: '/hook',
        verify: (_req, body) => {
          seen = body.toString();
          return { ok: true };
        },
        handle: async (_req, res) => {
          res.end('done');
        },
      },
    ]);
    await fetch(`${base}/hook/x`, { method: 'POST', body: '{"a":1}' });
    expect(seen).toBe('{"a":1}');
  });
});
