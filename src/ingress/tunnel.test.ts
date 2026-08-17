import http from 'http';
import type { AddressInfo } from 'net';
import { describe, it, expect } from 'vitest';
import {
  buildNgrokArgs,
  classifyProbeError,
  extractPublicUrl,
  probeNgrokApi,
} from './tunnel.js';

/** Serve one fixed body on loopback, and return its URL plus a close handle. */
async function serveOnce(
  body: string,
): Promise<{ url: string; close: () => Promise<void> }> {
  const server = http.createServer((_req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(body);
  });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${port}/api/tunnels`,
    close: () => new Promise<void>((resolve) => server.close(() => resolve())),
  };
}

// The regression this guards: an HTTP response whose body does not parse used
// to resolve as "port free", which let a second ngrok launch into the session
// conflict the pre-flight exists to prevent.
describe('probeNgrokApi', () => {
  it('reports occupied when something answers with valid JSON', async () => {
    const s = await serveOnce(
      JSON.stringify({
        tunnels: [{ proto: 'https', public_url: 'https://x' }],
      }),
    );
    try {
      const probe = await probeNgrokApi(s.url);
      expect(probe.state).toBe('occupied');
      expect(
        extractPublicUrl(probe.state === 'occupied' ? probe.api : null),
      ).toBe('https://x');
    } finally {
      await s.close();
    }
  });

  it('reports occupied — NOT free — when the responder returns unparseable JSON', async () => {
    const s = await serveOnce('<html>definitely not json</html>');
    try {
      const probe = await probeNgrokApi(s.url);
      expect(probe.state).toBe('occupied');
      expect(probe.state === 'occupied' && probe.api).toBeNull();
    } finally {
      await s.close();
    }
  });

  // A peer that sends headers then resets used to resolve null, which the
  // pre-flight read as "free". It must now never be "free". Whether it lands on
  // 'occupied' or 'indeterminate' depends on whether the buffered headers were
  // flushed before the reset, and both fail closed — so assert the invariant
  // that matters rather than a timing-dependent state.
  it('never reports free when the peer resets mid-response', async () => {
    const server = http.createServer((_req, res) => {
      res.writeHead(200, {
        'Content-Type': 'application/json',
        'Content-Length': '9999',
      });
      res.write('{"tunnels":');
      res.socket?.destroy();
    });
    await new Promise<void>((resolve) =>
      server.listen(0, '127.0.0.1', resolve),
    );
    const { port } = server.address() as AddressInfo;
    try {
      const probe = await probeNgrokApi(`http://127.0.0.1:${port}/api/tunnels`);
      expect(probe.state).not.toBe('free');
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  }, 5000);

  // The probe budget must be ABSOLUTE. A socket-inactivity timeout would be
  // reset by every chunk, so a responder that streams forever without ending
  // would hang the pre-flight — and with it, startup.
  it('settles under an absolute deadline when the responder never ends', async () => {
    const timers: NodeJS.Timeout[] = [];
    const server = http.createServer((_req, res) => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      // Keep dribbling data so an inactivity timer would never fire.
      const t = setInterval(() => res.write('{"keep":"going"}'), 50);
      timers.push(t as unknown as NodeJS.Timeout);
      res.on('close', () => clearInterval(t));
    });
    await new Promise<void>((resolve) =>
      server.listen(0, '127.0.0.1', resolve),
    );
    const { port } = server.address() as AddressInfo;
    try {
      const started = Date.now();
      const probe = await probeNgrokApi(`http://127.0.0.1:${port}/api/tunnels`);
      expect(probe.state).not.toBe('free');
      expect(Date.now() - started).toBeLessThan(4000);
    } finally {
      timers.forEach(clearInterval);
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  }, 8000);

  it('reports free only when nothing is listening', async () => {
    // Bind then immediately release, so the port is known-unused and refuses.
    const s = await serveOnce('{}');
    const url = s.url;
    await s.close();
    const probe = await probeNgrokApi(url);
    expect(probe.state).toBe('free');
  });
});

// The :4040 pre-flight is a fail-closed check, so "free" must mean proof of an
// absent listener — not merely "the request did not succeed". NGROK_API is a
// literal IPv4 loopback with no DNS, so an unoccupied port refuses immediately.
describe('classifyProbeError', () => {
  it('treats ECONNREFUSED as proof the port is free', () => {
    expect(classifyProbeError('ECONNREFUSED')).toEqual({ state: 'free' });
  });

  it('does not treat a reset connection as free — a live listener can reset', () => {
    const probe = classifyProbeError('ECONNRESET');
    expect(probe.state).toBe('indeterminate');
  });

  it.each(['EPIPE', 'ECONNABORTED', 'EHOSTUNREACH', 'ETIMEDOUT'])(
    'does not treat %s as free',
    (code) => {
      expect(classifyProbeError(code).state).toBe('indeterminate');
    },
  );

  it('does not treat an error with no code as free', () => {
    expect(classifyProbeError(undefined).state).toBe('indeterminate');
  });

  it('explains which error made the result indeterminate', () => {
    const probe = classifyProbeError('ECONNRESET');
    expect(probe.state === 'indeterminate' && probe.detail).toContain(
      'ECONNRESET',
    );
  });
});

describe('buildNgrokArgs', () => {
  it('builds the base http args', () => {
    expect(buildNgrokArgs(3007)).toEqual([
      'http',
      '3007',
      '--log',
      'stdout',
      '--log-format',
      'json',
    ]);
  });

  it('adds --url for a static domain (hostname form)', () => {
    const args = buildNgrokArgs(3007, 'foo.ngrok-free.dev');
    expect(args).toContain('--url');
    expect(args).toContain('https://foo.ngrok-free.dev');
  });

  it('strips an existing scheme from the static domain', () => {
    const args = buildNgrokArgs(3007, 'https://foo.ngrok-free.dev');
    expect(args).toContain('https://foo.ngrok-free.dev');
    expect(args).not.toContain('https://https://foo.ngrok-free.dev');
  });

  it('adds --authtoken when provided', () => {
    expect(buildNgrokArgs(3007, undefined, 'tok')).toEqual(
      expect.arrayContaining(['--authtoken', 'tok']),
    );
  });
});

describe('extractPublicUrl', () => {
  it('prefers the https tunnel', () => {
    const api = {
      tunnels: [
        { proto: 'http', public_url: 'http://x.ngrok' },
        { proto: 'https', public_url: 'https://x.ngrok' },
      ],
    };
    expect(extractPublicUrl(api)).toBe('https://x.ngrok');
  });

  it('falls back to the first tunnel if no https', () => {
    const api = { tunnels: [{ proto: 'tcp', public_url: 'tcp://x:1' }] };
    expect(extractPublicUrl(api)).toBe('tcp://x:1');
  });

  it('returns null for empty/invalid payloads', () => {
    expect(extractPublicUrl(null)).toBeNull();
    expect(extractPublicUrl({})).toBeNull();
    expect(extractPublicUrl({ tunnels: [] })).toBeNull();
    expect(extractPublicUrl({ tunnels: 'nope' })).toBeNull();
  });
});
