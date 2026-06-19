import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Shared spies must be hoisted — vi.mock factories run before module-scope
// const initialization (TDZ), so reference them via vi.hoisted.
const { mcpExecute, brokerExecute, gateMock } = vi.hoisted(() => ({
  mcpExecute: vi.fn(),
  brokerExecute: vi.fn(),
  gateMock: vi.fn(),
}));

vi.mock('./tool-broker.js', () => ({
  createOpenAIMcpToolBridge: vi.fn(async () => ({
    definitions: [],
    execute: mcpExecute,
    close: vi.fn(),
  })),
  executeBrokerTool: brokerExecute,
  getOpenAIToolDefinitions: vi.fn(() => []),
}));

vi.mock('./context-registry.js', () => ({
  loadRegisteredContextFiles: vi.fn(() => []),
}));

vi.mock('./pre-tool-use-hook.js', () => ({
  dispatchPreToolUseGate: gateMock,
}));

import { DoomLoopDetector } from './doom-loop-detector.js';
import { runSingleTurn, type ContainerInput } from './llama-cpp-backend.js';

const containerInput = {
  chatJid: 'test@jid',
  groupFolder: 'test-group',
  prompt: 'hello',
} as unknown as ContainerInput;

const jsonResponse = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });

// First model response asks for one Bash tool call; the second has no calls so
// runSingleTurn terminates.
const responseWithCall = {
  choices: [
    {
      message: {
        role: 'assistant',
        content: '',
        tool_calls: [
          {
            id: 'call_1',
            type: 'function',
            function: {
              name: 'Bash',
              arguments: JSON.stringify({ command: 'ls' }),
            },
          },
        ],
      },
    },
  ],
};
const terminatingResponse = {
  choices: [{ message: { role: 'assistant', content: 'done' } }],
};

describe('runSingleTurn — PreToolUse gate seam (llama-cpp backend)', () => {
  const originalEnv = { ...process.env };
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    process.env = { ...originalEnv, LLAMA_CPP_BASE_URL: 'http://stub/v1' };
    mcpExecute.mockReset();
    brokerExecute.mockReset();
    gateMock.mockReset();
    fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse(responseWithCall))
      .mockResolvedValueOnce(jsonResponse(terminatingResponse));
  });

  afterEach(() => {
    process.env = { ...originalEnv };
    vi.restoreAllMocks();
  });

  it('does NOT execute a tool when the gate blocks it', async () => {
    gateMock.mockResolvedValue({ block: true, reason: 'denied by warden' });
    const detector = new DoomLoopDetector();
    const recordSpy = vi.spyOn(detector, 'record');

    const result = await runSingleTurn(
      'hello',
      containerInput,
      [],
      () => {},
      detector,
    );

    expect(mcpExecute).not.toHaveBeenCalled();
    expect(brokerExecute).not.toHaveBeenCalled();
    expect(recordSpy).not.toHaveBeenCalled();
    expect(gateMock).toHaveBeenCalledWith(
      expect.objectContaining({ toolName: 'Bash', toolUseId: 'call_1' }),
    );
    expect(result.result).toBe('done');
  });

  it('feeds the block reason back to the model as the tool result', async () => {
    gateMock.mockResolvedValue({ block: true, reason: 'denied by warden' });
    const detector = new DoomLoopDetector();

    await runSingleTurn('hello', containerInput, [], () => {}, detector);

    expect(fetchSpy).toHaveBeenCalledTimes(2);
    const secondBody = JSON.parse(
      (fetchSpy.mock.calls[1][1] as { body: string }).body,
    );
    const refusal = (
      secondBody.messages as Array<Record<string, unknown>>
    ).find((m) => m.role === 'tool' && m.tool_call_id === 'call_1');
    expect(refusal).toBeDefined();
    expect(String(refusal!.content)).toContain('denied by warden');
  });

  it('executes the tool normally when the gate allows it (control)', async () => {
    gateMock.mockResolvedValue({ block: false });
    mcpExecute.mockResolvedValue({ ok: true, exitCode: 0, output: 'files' });
    const detector = new DoomLoopDetector();
    const recordSpy = vi.spyOn(detector, 'record');

    await runSingleTurn('hello', containerInput, [], () => {}, detector);

    expect(mcpExecute).toHaveBeenCalledWith('Bash', { command: 'ls' });
    expect(recordSpy).toHaveBeenCalledTimes(1);
  });
});
