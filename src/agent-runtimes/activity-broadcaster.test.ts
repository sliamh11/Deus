import { describe, it, expect } from 'vitest';

import {
  RuntimeActivityBroadcaster,
  withRuntimeActivityBroadcast,
  type RuntimeActivityEnvelope,
  type RuntimeActivitySource,
} from './activity-broadcaster.js';
import type {
  AgentRuntime,
  RunContext,
  RunResult,
  RuntimeCapabilities,
  RuntimeEvent,
  RuntimeEventSink,
  RuntimeSession,
} from './types.js';

const CAPS: RuntimeCapabilities = {
  shell: false,
  filesystem: false,
  web: true,
  multimodal: false,
  handoffs: false,
  persistent_sessions: true,
  tool_streaming: false,
};

const SOURCE_A: RuntimeActivitySource = {
  backend: 'deus-native',
  groupFolder: 'group-a',
  chatJid: 'a@g.us',
};
const SOURCE_B: RuntimeActivitySource = {
  backend: 'deus-native',
  groupFolder: 'group-b',
  chatJid: 'b@g.us',
};

function collect(
  broadcaster: RuntimeActivityBroadcaster,
): RuntimeActivityEnvelope[] {
  const received: RuntimeActivityEnvelope[] = [];
  broadcaster.subscribe((envelope) => received.push(envelope));
  return received;
}

describe('RuntimeActivityBroadcaster — schema and global ordering', () => {
  it('publishes an exhaustive, correctly-shaped envelope for every RuntimeEvent variant', () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);
    const runId = 'run-1';

    const events: RuntimeEvent[] = [
      { type: 'tool_call', name: 'web_search', arguments: { query: 'x' } },
      {
        type: 'usage',
        sessionId: 'sess-1',
        provider: 'anthropic',
        model: 'claude-opus-4-8',
        inputTokens: 10,
        outputTokens: 20,
        totalTokens: 30,
      },
      { type: 'output_text', text: 'hello' },
      { type: 'turn_complete' },
    ];
    for (const event of events) broadcaster.publish(runId, SOURCE_A, event);

    expect(received).toHaveLength(4);

    // Shared runId, one streamId, strictly increasing sequence, id === `${streamId}:${sequence}`.
    for (let i = 0; i < received.length; i++) {
      const envelope = received[i];
      expect(envelope.runId).toBe(runId);
      expect(envelope.streamId).toBe(broadcaster.streamId);
      expect(envelope.sequence).toBe(i + 1);
      expect(envelope.id).toBe(`${broadcaster.streamId}:${i + 1}`);
      expect(() => new Date(envelope.timestamp).toISOString()).not.toThrow();
      expect(new Date(envelope.timestamp).toISOString()).toBe(
        envelope.timestamp,
      );
      expect(envelope.source).toEqual(SOURCE_A);
    }

    expect(received[0]).toMatchObject({
      type: 'tool_call',
      payload: { name: 'web_search', arguments: { query: 'x' } },
    });
    expect(received[1]).toMatchObject({
      type: 'usage',
      payload: {
        sessionId: 'sess-1',
        provider: 'anthropic',
        model: 'claude-opus-4-8',
        inputTokens: 10,
        outputTokens: 20,
        totalTokens: 30,
      },
    });
    expect(received[2]).toMatchObject({
      type: 'output_text',
      payload: { text: 'hello' },
    });
    expect(received[3]).toMatchObject({
      type: 'turn_complete',
      payload: {},
    });
  });

  it('preserves undefined token semantics by omitting usage keys rather than fabricating zeroes', () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);

    broadcaster.publish('run-1', SOURCE_A, {
      type: 'usage',
      sessionId: 'sess-1',
      provider: 'anthropic',
      model: 'claude-opus-4-8',
      inputTokens: undefined,
      outputTokens: undefined,
      totalTokens: undefined,
    });

    const payload = received[0].payload as Record<string, unknown>;
    expect('inputTokens' in payload).toBe(false);
    expect('outputTokens' in payload).toBe(false);
    expect('totalTokens' in payload).toBe(false);
    expect(payload.sessionId).toBe('sess-1');
  });

  it('maps session and error events to their documented payload shapes', () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);
    const sessionRef: RuntimeSession = {
      backend: 'deus-native',
      session_id: 'thread-1',
    };

    broadcaster.publish('run-1', SOURCE_A, {
      type: 'session',
      sessionRef,
    });
    broadcaster.publish('run-1', SOURCE_A, {
      type: 'error',
      error: 'boom',
    });
    broadcaster.publish('run-1', SOURCE_A, {
      type: 'activity',
      text: 'thinking',
    });

    expect(received[0]).toMatchObject({
      type: 'session',
      payload: { sessionRef },
    });
    expect(received[1]).toMatchObject({
      type: 'error',
      payload: { error: 'boom' },
    });
    expect(received[2]).toMatchObject({
      type: 'activity',
      payload: { text: 'thinking' },
    });
  });

  it('establishes ONE total publication order across multiple run IDs and groups (global, not per-run, sequence)', () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);

    broadcaster.publish('run-A', SOURCE_A, { type: 'activity', text: '1' });
    broadcaster.publish('run-B', SOURCE_B, { type: 'activity', text: '2' });
    broadcaster.publish('run-A', SOURCE_A, { type: 'turn_complete' });
    broadcaster.publish('run-B', SOURCE_B, { type: 'turn_complete' });

    expect(received.map((e) => e.sequence)).toEqual([1, 2, 3, 4]);
    expect(received.map((e) => e.runId)).toEqual([
      'run-A',
      'run-B',
      'run-A',
      'run-B',
    ]);
    expect(received.map((e) => e.source.groupFolder)).toEqual([
      'group-a',
      'group-b',
      'group-a',
      'group-b',
    ]);
    // All four share the one broadcaster streamId.
    expect(new Set(received.map((e) => e.streamId)).size).toBe(1);
  });

  it('a fresh broadcaster instance has a different streamId (new ordering epoch)', () => {
    const a = new RuntimeActivityBroadcaster();
    const b = new RuntimeActivityBroadcaster();
    expect(a.streamId).not.toBe(b.streamId);
  });
});

describe('RuntimeActivityBroadcaster — subscribe/subscriberCount/isolation', () => {
  it('subscriberCount reflects active subscribers and drops to zero after unsubscribe', () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    expect(broadcaster.subscriberCount()).toBe(0);
    const unsubscribe = broadcaster.subscribe(() => {});
    expect(broadcaster.subscriberCount()).toBe(1);
    unsubscribe();
    expect(broadcaster.subscriberCount()).toBe(0);
  });

  it('fans out one envelope to every current subscriber, byte-equivalent', () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    const receivedA: RuntimeActivityEnvelope[] = [];
    const receivedB: RuntimeActivityEnvelope[] = [];
    broadcaster.subscribe((e) => receivedA.push(e));
    broadcaster.subscribe((e) => receivedB.push(e));

    broadcaster.publish('run-1', SOURCE_A, { type: 'turn_complete' });

    expect(receivedA).toEqual(receivedB);
    expect(receivedA).toHaveLength(1);
  });

  it('isolates a throwing subscriber — delivery to the remaining subscribers still happens', () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    const received: RuntimeActivityEnvelope[] = [];
    broadcaster.subscribe(() => {
      throw new Error('broken subscriber');
    });
    broadcaster.subscribe((e) => received.push(e));

    expect(() =>
      broadcaster.publish('run-1', SOURCE_A, { type: 'turn_complete' }),
    ).not.toThrow();
    expect(received).toHaveLength(1);
  });
});

describe('RuntimeActivityBroadcaster — close() lifecycle (safe no-op guarantee)', () => {
  it('close() is idempotent, removes subscribers, and post-close publish()/subscribe() are safe no-ops', () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);
    expect(broadcaster.subscriberCount()).toBe(1);

    broadcaster.close();
    expect(broadcaster.subscriberCount()).toBe(0);

    // Post-close subscribe() is inert: returns a no-op unsubscribe, does not
    // actually register a listener.
    const lateReceived: RuntimeActivityEnvelope[] = [];
    const unsubscribe = broadcaster.subscribe((e) => lateReceived.push(e));
    expect(broadcaster.subscriberCount()).toBe(0);
    expect(() => unsubscribe()).not.toThrow();

    // Post-close publish() allocates no id/sequence and invokes no subscriber.
    expect(() =>
      broadcaster.publish('run-1', SOURCE_A, { type: 'turn_complete' }),
    ).not.toThrow();
    expect(received).toHaveLength(0);
    expect(lateReceived).toHaveLength(0);
  });

  it('calling close() twice, then publish(), remains a documented safe no-op', () => {
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);

    broadcaster.close();
    expect(() => broadcaster.close()).not.toThrow();
    expect(() =>
      broadcaster.publish('run-1', SOURCE_A, { type: 'turn_complete' }),
    ).not.toThrow();

    expect(received).toHaveLength(0);
    expect(broadcaster.subscriberCount()).toBe(0);
  });
});

// ── withRuntimeActivityBroadcast() decorator ────────────────────────────────

function makeFakeRuntime(
  script: (sink: RuntimeEventSink) => Promise<RunResult>,
): AgentRuntime {
  return {
    name: () => 'deus-native',
    capabilities: () => CAPS,
    startOrResume: async (_ctx: RunContext) => ({
      backend: 'deus-native',
      session_id: 'stub',
    }),
    close: async (_sessionRef: RuntimeSession) => {},
    runTurn: (
      _runContext: RunContext,
      _sessionRef: RuntimeSession,
      sink: RuntimeEventSink,
    ) => script(sink),
  };
}

const RUN_CONTEXT: RunContext = {
  prompt: 'hi',
  groupFolder: 'group-a',
  chatJid: 'a@g.us',
  isControlGroup: false,
};
const SESSION_REF: RuntimeSession = {
  backend: 'deus-native',
  session_id: '',
};

describe('withRuntimeActivityBroadcast — decorator transparency', () => {
  it('forwards name()/capabilities()/startOrResume()/close() to the wrapped runtime unchanged', async () => {
    const runtime = makeFakeRuntime(async (sink) => {
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: 'ok' };
    });
    const broadcaster = new RuntimeActivityBroadcaster();
    const decorated = withRuntimeActivityBroadcast(runtime, broadcaster);

    expect(decorated.name()).toBe('deus-native');
    expect(decorated.capabilities()).toEqual(CAPS);
    await expect(decorated.startOrResume(RUN_CONTEXT)).resolves.toEqual({
      backend: 'deus-native',
      session_id: 'stub',
    });
    await expect(decorated.close(SESSION_REF)).resolves.toBeUndefined();
  });

  it('the downstream sink receives the original events unchanged and in order, while the broadcaster adds source/run metadata', async () => {
    const scriptedEvents: RuntimeEvent[] = [
      { type: 'activity', text: 'thinking' },
      { type: 'tool_call', name: 'web_search', arguments: { q: 'x' } },
      { type: 'output_text', text: 'done' },
      { type: 'turn_complete' },
    ];
    const runtime = makeFakeRuntime(async (sink) => {
      for (const event of scriptedEvents) await sink(event);
      return { status: 'success', result: 'done' };
    });
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);
    const decorated = withRuntimeActivityBroadcast(runtime, broadcaster);

    const downstream: RuntimeEvent[] = [];
    const result = await decorated.runTurn(RUN_CONTEXT, SESSION_REF, (e) => {
      downstream.push(e);
    });

    expect(result).toEqual({ status: 'success', result: 'done' });
    // Downstream sink: unchanged, in order.
    expect(downstream).toEqual(scriptedEvents);

    // Broadcaster: same events, in order, PLUS source/run metadata, and one
    // shared runId across the whole turn.
    expect(received.map((e) => e.type)).toEqual([
      'activity',
      'tool_call',
      'output_text',
      'turn_complete',
    ]);
    const runIds = new Set(received.map((e) => e.runId));
    expect(runIds.size).toBe(1);
    for (const envelope of received) {
      expect(envelope.source).toEqual({
        backend: 'deus-native',
        groupFolder: 'group-a',
        chatJid: 'a@g.us',
      });
    }
  });

  it('mints a distinct runId per runTurn() call', async () => {
    const runtime = makeFakeRuntime(async (sink) => {
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: 'ok' };
    });
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);
    const decorated = withRuntimeActivityBroadcast(runtime, broadcaster);

    await decorated.runTurn(RUN_CONTEXT, SESSION_REF, () => {});
    await decorated.runTurn(RUN_CONTEXT, SESSION_REF, () => {});

    expect(received).toHaveLength(2);
    expect(received[0].runId).not.toBe(received[1].runId);
  });

  it('publishes ONE synthetic terminal error when RunResult.status is "error" without an emitted error event, and never injects it into the caller sink or changes RunResult', async () => {
    const runtime = makeFakeRuntime(async (sink) => {
      await sink({ type: 'activity', text: 'trying' });
      return { status: 'error', result: null, error: 'downstream failure' };
    });
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);
    const decorated = withRuntimeActivityBroadcast(runtime, broadcaster);

    const downstream: RuntimeEvent[] = [];
    const result = await decorated.runTurn(RUN_CONTEXT, SESSION_REF, (e) => {
      downstream.push(e);
    });

    expect(result).toEqual({
      status: 'error',
      result: null,
      error: 'downstream failure',
    });
    // Caller's own sink never sees the synthetic error — only the real
    // events the runtime actually emitted.
    expect(downstream).toEqual([{ type: 'activity', text: 'trying' }]);

    // Broadcaster sees the real event PLUS exactly one synthetic terminal
    // error envelope carrying RunResult.error.
    expect(received.map((e) => e.type)).toEqual(['activity', 'error']);
    expect(received[1].payload).toEqual({ error: 'downstream failure' });
  });

  it('does not publish a synthetic error when the wrapped runtime already emitted one', async () => {
    const runtime = makeFakeRuntime(async (sink) => {
      await sink({ type: 'error', error: 'real error event' });
      return { status: 'error', result: null, error: 'real error event' };
    });
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);
    const decorated = withRuntimeActivityBroadcast(runtime, broadcaster);

    await decorated.runTurn(RUN_CONTEXT, SESSION_REF, () => {});

    expect(received).toHaveLength(1);
    expect(received[0].type).toBe('error');
  });

  it('when the wrapped call throws, publishes the same terminal error and rethrows unchanged', async () => {
    const runtime = makeFakeRuntime(async () => {
      throw new Error('unexpected throw');
    });
    const broadcaster = new RuntimeActivityBroadcaster();
    const received = collect(broadcaster);
    const decorated = withRuntimeActivityBroadcast(runtime, broadcaster);

    await expect(
      decorated.runTurn(RUN_CONTEXT, SESSION_REF, () => {}),
    ).rejects.toThrow('unexpected throw');

    expect(received).toHaveLength(1);
    expect(received[0]).toMatchObject({
      type: 'error',
      payload: { error: 'unexpected throw' },
    });
  });

  it("isolates one disconnected subscriber's throw from AgentRuntime.runTurn() itself (defense in depth via publish())", async () => {
    const runtime = makeFakeRuntime(async (sink) => {
      await sink({ type: 'turn_complete' });
      return { status: 'success', result: 'ok' };
    });
    const broadcaster = new RuntimeActivityBroadcaster();
    broadcaster.subscribe(() => {
      throw new Error('simulated disconnected SSE client write failure');
    });
    const decorated = withRuntimeActivityBroadcast(runtime, broadcaster);

    await expect(
      decorated.runTurn(RUN_CONTEXT, SESSION_REF, () => {}),
    ).resolves.toEqual({ status: 'success', result: 'ok' });
  });
});
