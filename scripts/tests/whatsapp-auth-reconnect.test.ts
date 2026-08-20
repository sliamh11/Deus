/**
 * Regression tests for #1162 — whatsapp-auth never reconnected after a
 * retriable close, so pairing-code auth could never succeed.
 *
 * These assert the BEHAVIOUR that was broken (a retriable close tears down and
 * actually invokes a reconnect, and stays bounded), not merely that the reason
 * is classified as retriable — classification alone would pass on the buggy
 * code too and prove nothing.
 *
 * Reason 515 ("restart required") always follows a successful pairing, which is
 * why it is the case that matters most here.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { createConnectionUpdateHandler } from '../whatsapp-auth.js';

/** Build a `connection.update` payload for a close with the given status code. */
function close(statusCode: number) {
  return {
    connection: 'close',
    lastDisconnect: { error: { output: { statusCode } } },
  };
}

function makeDeps(overrides: Partial<Record<string, unknown>> = {}) {
  return {
    reconnect: vi.fn(),
    teardown: vi.fn(),
    fail: vi.fn(),
    succeed: vi.fn(),
    ...overrides,
  } as {
    reconnect: ReturnType<typeof vi.fn>;
    teardown: ReturnType<typeof vi.fn>;
    fail: ReturnType<typeof vi.fn>;
    succeed: ReturnType<typeof vi.fn>;
  };
}

describe('whatsapp-auth reconnect (#1162)', () => {
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  it('reconnects after a 515 restart-required close', () => {
    const deps = makeDeps();
    const handler = createConnectionUpdateHandler({
      ...deps,
      maxRetries: 3,
      retryState: { count: 0 },
    });

    handler(close(515));

    // The defect: on the unfixed code nothing ever called reconnect().
    expect(deps.reconnect).toHaveBeenCalledTimes(1);
    expect(deps.fail).not.toHaveBeenCalled();
  });

  it('tears the dead socket down before reconnecting', () => {
    const order: string[] = [];
    const deps = makeDeps({
      teardown: vi.fn(() => order.push('teardown')),
      reconnect: vi.fn(() => order.push('reconnect')),
    });
    const handler = createConnectionUpdateHandler({
      ...deps,
      maxRetries: 3,
      retryState: { count: 0 },
    });

    handler(close(515));

    // Listener accumulation across attempts is the #305 failure mode.
    expect(order).toEqual(['teardown', 'reconnect']);
  });

  it('stays bounded: stops reconnecting once retries are exhausted', () => {
    const deps = makeDeps();
    // ONE shared retry state across attempts — this is what production does, and
    // it is the reason the counter cannot live inside the handler: each reconnect
    // builds a fresh socket and a fresh handler.
    const retryState = { count: 0 };
    const attempt = () =>
      createConnectionUpdateHandler({ ...deps, maxRetries: 3, retryState });

    attempt()(close(515));
    attempt()(close(515));
    attempt()(close(515));

    expect(deps.fail).toHaveBeenCalledTimes(1);
    expect(deps.fail.mock.calls[0][0]).toContain('retries exhausted');
    // Bound preserved: the third close failed instead of reconnecting again.
    expect(deps.reconnect).toHaveBeenCalledTimes(2);
  });

  it('never reconnects on a logged-out close', () => {
    const deps = makeDeps();
    const handler = createConnectionUpdateHandler({
      ...deps,
      maxRetries: 3,
      retryState: { count: 0 },
    });

    handler(close(401));

    expect(deps.reconnect).not.toHaveBeenCalled();
    expect(deps.fail).toHaveBeenCalledTimes(1);
    expect(deps.fail.mock.calls[0][0]).toContain('logged_out');
  });

  it('never reconnects on a 405 rejection', () => {
    const deps = makeDeps();
    const handler = createConnectionUpdateHandler({
      ...deps,
      maxRetries: 3,
      retryState: { count: 0 },
    });

    handler(close(405));

    expect(deps.reconnect).not.toHaveBeenCalled();
    expect(deps.fail).toHaveBeenCalledTimes(1);
    expect(deps.fail.mock.calls[0][0]).toContain('405');
  });

  it('single-flights overlapping close events', () => {
    const deps = makeDeps();
    const handler = createConnectionUpdateHandler({ ...deps, maxRetries: 10 });

    handler(close(515));
    handler(close(515));
    handler(close(515));

    // One handler instance == one socket == at most one scheduled reconnect.
    expect(deps.reconnect).toHaveBeenCalledTimes(1);
  });

  it('reports success on an open connection', () => {
    const deps = makeDeps();
    const handler = createConnectionUpdateHandler({
      ...deps,
      maxRetries: 3,
      retryState: { count: 0 },
    });

    handler({ connection: 'open' });

    expect(deps.succeed).toHaveBeenCalledTimes(1);
    expect(deps.fail).not.toHaveBeenCalled();
  });
});
