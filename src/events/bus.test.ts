import { describe, expect, it, vi } from 'vitest';
import { EventBus, getBus } from './bus.js';
import type { EventEnvelope } from './types.js';

function mkEnv(overrides: Partial<EventEnvelope> = {}): EventEnvelope {
  return {
    type: 'agent.done',
    source: 'test',
    actor: 'system',
    correlationId: { kind: 'issue', id: 'ISS-1', identifier: 'LIA-1' },
    ts: new Date().toISOString(),
    payload: { output: 'ok' },
    ...overrides,
  };
}

describe('EventBus', () => {
  it('delivers an event to a type-matched subscriber', async () => {
    const bus = new EventBus();
    const seen: EventEnvelope[] = [];
    bus.subscribe('agent.done', (e) => {
      seen.push(e);
    });
    await bus.emit(mkEnv());
    expect(seen).toHaveLength(1);
    expect(seen[0].type).toBe('agent.done');
  });

  it('delivers every event to catch-all `on` handlers', async () => {
    const bus = new EventBus();
    const seen: string[] = [];
    bus.on((e) => {
      seen.push(e.type);
    });
    await bus.emit(mkEnv());
    expect(seen).toEqual(['agent.done']);
  });

  it('awaits async handlers sequentially in registration order', async () => {
    const bus = new EventBus();
    const order: number[] = [];
    // First handler is slower than the second. If emit awaited sequentially the
    // result is [1, 2]; a parallel/fire-and-forget emit would yield [2, 1].
    bus.subscribe('agent.done', async () => {
      await new Promise((r) => setTimeout(r, 20));
      order.push(1);
    });
    bus.subscribe('agent.done', async () => {
      await new Promise((r) => setTimeout(r, 1));
      order.push(2);
    });
    await bus.emit(mkEnv());
    expect(order).toEqual([1, 2]);
  });

  it('isolates a synchronously throwing handler — siblings run, emit resolves', async () => {
    const bus = new EventBus();
    const ran: string[] = [];
    bus.subscribe('agent.done', () => {
      throw new Error('boom');
    });
    bus.subscribe('agent.done', () => {
      ran.push('second');
    });
    await expect(bus.emit(mkEnv())).resolves.toBeUndefined();
    expect(ran).toEqual(['second']);
  });

  it('isolates a rejecting async handler too', async () => {
    const bus = new EventBus();
    const ran: string[] = [];
    bus.subscribe('agent.done', async () => {
      throw new Error('async boom');
    });
    bus.subscribe('agent.done', async () => {
      ran.push('after');
    });
    await expect(bus.emit(mkEnv())).resolves.toBeUndefined();
    expect(ran).toEqual(['after']);
  });

  it('unsubscribe removes a type subscriber', async () => {
    const bus = new EventBus();
    const fn = vi.fn();
    const off = bus.subscribe('agent.done', fn);
    off();
    await bus.emit(mkEnv());
    expect(fn).not.toHaveBeenCalled();
  });

  it('unsubscribe removes a catch-all handler', async () => {
    const bus = new EventBus();
    const fn = vi.fn();
    const off = bus.on(fn);
    off();
    await bus.emit(mkEnv());
    expect(fn).not.toHaveBeenCalled();
  });

  it('getBus returns a stable singleton', () => {
    expect(getBus()).toBe(getBus());
  });
});
