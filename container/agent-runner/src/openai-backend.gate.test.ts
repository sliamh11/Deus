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
  resolveGroupAttachmentPath: vi.fn((p: string) => `/workspace/group/${p}`),
}));

vi.mock('./context-registry.js', () => ({
  loadRegisteredContextFiles: vi.fn(() => []),
}));

vi.mock('./pre-tool-use-hook.js', () => ({
  dispatchPreToolUseGate: gateMock,
}));

import { DoomLoopDetector } from './doom-loop-detector.js';
import { runSingleTurn, type ContainerInput } from './openai-backend.js';

const containerInput = {
  chatJid: 'test@jid',
  groupFolder: 'test-group',
  prompt: 'hello',
  imageAttachments: [],
} as unknown as ContainerInput;

const jsonResponse = (body: unknown) =>
  new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });

// First model response asks for one Bash tool call; the second has no calls so
// runSingleTurn terminates.
const responseWithCall = {
  id: 'resp_1',
  output: [
    {
      type: 'function_call',
      name: 'Bash',
      call_id: 'call_1',
      arguments: JSON.stringify({ command: 'ls' }),
    },
  ],
};
const terminatingResponse = { id: 'resp_2', output: [], output_text: 'done' };

describe('runSingleTurn — PreToolUse gate seam (OpenAI backend)', () => {
  const originalEnv = { ...process.env };
  let fetchSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    process.env = { ...originalEnv, OPENAI_BASE_URL: 'http://stub/v1' };
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
      undefined,
      () => {},
      detector,
    );

    // Neither dispatch path ran for the blocked call.
    expect(mcpExecute).not.toHaveBeenCalled();
    expect(brokerExecute).not.toHaveBeenCalled();
    // A blocked call is not an execution → no doom record.
    expect(recordSpy).not.toHaveBeenCalled();
    // The gate was consulted for the call.
    expect(gateMock).toHaveBeenCalledWith(
      expect.objectContaining({ toolName: 'Bash', toolUseId: 'call_1' }),
    );
    // The loop still terminated normally.
    expect(result.result).toBe('done');
  });

  it('feeds the block reason back to the model as the tool result', async () => {
    gateMock.mockResolvedValue({ block: true, reason: 'denied by warden' });
    const detector = new DoomLoopDetector();

    await runSingleTurn('hello', containerInput, undefined, () => {}, detector);

    // Second createResponse call carries the refusal as the function_call_output.
    expect(fetchSpy).toHaveBeenCalledTimes(2);
    const secondBody = JSON.parse(
      (fetchSpy.mock.calls[1][1] as { body: string }).body,
    );
    const refusal = (secondBody.input as Array<Record<string, unknown>>).find(
      (i) => i.type === 'function_call_output' && i.call_id === 'call_1',
    );
    expect(refusal).toBeDefined();
    expect(String(refusal!.output)).toContain('denied by warden');
  });

  it('executes the tool normally when the gate allows it (control)', async () => {
    gateMock.mockResolvedValue({ block: false });
    mcpExecute.mockResolvedValue({ ok: true, exitCode: 0, output: 'files' });
    const detector = new DoomLoopDetector();
    const recordSpy = vi.spyOn(detector, 'record');

    await runSingleTurn('hello', containerInput, undefined, () => {}, detector);

    // Allowed → the tool ran and the doom detector recorded the execution.
    expect(mcpExecute).toHaveBeenCalledWith('Bash', { command: 'ls' });
    expect(recordSpy).toHaveBeenCalledTimes(1);
  });
});
