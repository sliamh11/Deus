import { describe, it, expect, afterEach, vi } from 'vitest';
import http from 'http';
import type { IncomingMessage, Server } from 'http';
import type { AddressInfo } from 'net';

// This test drives the request-stream 'error' path directly; it never reaches
// auth/gate/registry logic, so these mocks only need to let startToolProxy bind.
vi.mock('./config.js', () => ({ DEUS_PROXY_AUTH_ENABLED: false }));
vi.mock('./db.js', () => ({
  getProjectById: vi.fn(() => undefined),
  getRegisteredGroupByFolder: vi.fn(() => undefined),
}));
vi.mock('./group-tokens.js', () => ({
  validateGroupToken: vi.fn(() => 'test-group'),
  isToolAllowedForToken: vi.fn(() => true),
}));
vi.mock('./logger.js', () => ({
  logger: { info: vi.fn(), error: vi.fn(), debug: vi.fn(), warn: vi.fn() },
}));
vi.mock('./tool-registry.js', () => ({
  loadRegistry: vi.fn(),
  isAllowed: vi.fn(() => false),
  getToolConfig: vi.fn(() => undefined),
}));

import { startToolProxy } from './tool-proxy.js';

describe('tool-proxy request stream error resilience (LIA-362)', () => {
  let server: Server | undefined;

  afterEach(async () => {
    if (server) {
      await new Promise<void>((resolve) => server!.close(() => resolve()));
      server = undefined;
    }
  });

  it("attaches a req 'error' handler so a client mid-upload reset can't crash the host", async () => {
    server = await startToolProxy(0);
    const port = (server.address() as AddressInfo).port;

    // The createServer handler runs first on each request and attaches its
    // req.on('error') before any await; a second 'request' listener lets us
    // capture that same IncomingMessage and emit 'error' on it. Without the
    // fix's listener, emitting 'error' on an EventEmitter throws synchronously
    // (the uncaught exception that crashes the daemon in production, LIA-362);
    // with it, the emit is inert.
    const captured = new Promise<IncomingMessage>((resolve) => {
      server!.on('request', (req) => resolve(req));
    });

    const client = http.request({
      host: '127.0.0.1',
      port,
      path: '/tool/whoami',
      method: 'POST',
    });
    client.on('error', () => {}); // client-side ECONNRESET is expected/ignored
    client.write('{}');
    // Don't end() — keep the request in-flight while we drive the error.

    const serverReq = await captured;
    expect(() =>
      serverReq.emit(
        'error',
        Object.assign(new Error('read ECONNRESET'), { code: 'ECONNRESET' }),
      ),
    ).not.toThrow();

    client.destroy();

    // The server must still be alive and answer a following request.
    const status = await new Promise<number>((resolve, reject) => {
      const req = http.request(
        { host: '127.0.0.1', port, path: '/tool/whoami', method: 'POST' },
        (res) => {
          res.resume();
          resolve(res.statusCode ?? 0);
        },
      );
      req.on('error', reject);
      req.end('{}');
    });
    expect(status).toBeGreaterThan(0);
  });
});
