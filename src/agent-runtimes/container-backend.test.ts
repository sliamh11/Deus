import { describe, it, expect, vi, beforeEach } from 'vitest';

import type { RuntimeEvent, RuntimeEventSink } from './types.js';
import { defaultSession } from './types.js';
import type { ContainerOutput } from '../container-runner.js';

// Mock runContainerAgent so we can drive onOutput with an arbitrary marker
// sequence and observe the RuntimeEvents the backend maps them to.
const { runContainerAgentMock } = vi.hoisted(() => ({
  runContainerAgentMock: vi.fn(),
}));
vi.mock('../container-runner.js', () => ({
  runContainerAgent: runContainerAgentMock,
}));

const { ContainerRuntime } = await import('./container-backend.js');

function makeRuntime() {
  return new ContainerRuntime('claude', {} as never, {
    resolveGroup: () => ({ folder: 'main', name: 'Main' }) as never,
    assistantName: 'Deus',
    registerProcess: () => {},
  });
}

const baseCtx = {
  prompt: 'hi',
  groupFolder: 'main',
  chatJid: 'c@g.us',
  isControlGroup: true,
  effort: 'low' as const, // avoids resolveAgentEffort needing a full group
};

beforeEach(() => {
  runContainerAgentMock.mockReset();
});

describe('ContainerRuntime onOutput → eventSink mapping', () => {
  it('streams partial→output_text + activity→activity, and suppresses the final result when streamed', async () => {
    runContainerAgentMock.mockImplementation(
      async (
        _g: unknown,
        _i: unknown,
        _onProc: unknown,
        onOutput: (o: ContainerOutput) => Promise<void>,
      ) => {
        await onOutput({ status: 'activity', text: 'Running grep' });
        await onOutput({ status: 'partial', delta: 'Hel' });
        await onOutput({ status: 'partial', delta: 'lo' });
        await onOutput({
          status: 'success',
          result: 'Hello',
          streamed: true,
          newSessionId: 's1',
        });
        return { status: 'success', result: 'Hello', newSessionId: 's1' };
      },
    );

    const events: RuntimeEvent[] = [];
    const sink: RuntimeEventSink = (e) => {
      events.push(e);
    };
    await makeRuntime().runTurn(
      { ...baseCtx, stream: true },
      defaultSession('', 'claude'),
      sink,
    );

    const text = (t: RuntimeEvent['type']) =>
      events
        .filter((e) => e.type === t)
        .map((e) => (e as { text: string }).text);

    // Answer arrived as deltas; the final 'Hello' is NOT re-emitted (no dup).
    expect(text('output_text')).toEqual(['Hel', 'lo']);
    expect(text('activity')).toEqual(['Running grep']);
    expect(events.some((e) => e.type === 'turn_complete')).toBe(true);
  });

  it('emits the final result as output_text when NOT streamed (buffered/WhatsApp path)', async () => {
    runContainerAgentMock.mockImplementation(
      async (
        _g: unknown,
        _i: unknown,
        _onProc: unknown,
        onOutput: (o: ContainerOutput) => Promise<void>,
      ) => {
        await onOutput({
          status: 'success',
          result: 'Hello',
          newSessionId: 's1',
        });
        return { status: 'success', result: 'Hello', newSessionId: 's1' };
      },
    );

    const events: RuntimeEvent[] = [];
    await makeRuntime().runTurn(baseCtx, defaultSession('', 'claude'), (e) => {
      events.push(e);
    });

    expect(
      events
        .filter((e) => e.type === 'output_text')
        .map((e) => (e as { text: string }).text),
    ).toEqual(['Hello']);
  });
});
