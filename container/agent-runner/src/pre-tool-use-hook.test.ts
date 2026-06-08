import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createPreToolUseHook,
  dispatchPreToolUseGate,
} from './pre-tool-use-hook.js';

const okResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

describe('dispatchPreToolUseGate', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    process.env.HOOK_DISPATCH_ENABLED = 'true';
    process.env.HOOK_DISPATCH_PORT = '3002';
    process.env.DEUS_PROXY_HOST = '127.0.0.1';
    process.env.DEUS_PROXY_TOKEN = 'test-token';
  });

  afterEach(() => {
    process.env = { ...originalEnv };
    vi.restoreAllMocks();
  });

  it('returns {block:false} with NO fetch when HOOK_DISPATCH_ENABLED is unset', async () => {
    delete process.env.HOOK_DISPATCH_ENABLED;
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    const result = await dispatchPreToolUseGate({
      toolName: 'Bash',
      toolInput: { command: 'ls' },
    });

    expect(result).toEqual({ block: false });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('returns {block:false} with NO fetch when HOOK_DISPATCH_ENABLED is not exactly "true"', async () => {
    process.env.HOOK_DISPATCH_ENABLED = '1';
    const fetchSpy = vi.spyOn(globalThis, 'fetch');

    const result = await dispatchPreToolUseGate({
      toolName: 'Bash',
      toolInput: {},
    });

    expect(result).toEqual({ block: false });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('returns {block:true, reason} when the service responds decision:block', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      okResponse({ decision: 'block', reason: 'denied by warden' }),
    );

    const result = await dispatchPreToolUseGate({
      toolName: 'Bash',
      toolInput: { command: 'rm -rf /' },
    });

    expect(result).toEqual({ block: true, reason: 'denied by warden' });
  });

  it('supplies a default reason when a block omits one', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      okResponse({ decision: 'block' }),
    );

    const result = await dispatchPreToolUseGate({
      toolName: 'Edit',
      toolInput: {},
    });

    expect(result.block).toBe(true);
    expect(result.reason).toBe('Blocked by PreToolUse observer');
  });

  it('returns {block:false, response} on a non-block success (forwards additionalContext)', async () => {
    const data = {
      hookSpecificOutput: { additionalContext: 'note from observer' },
    };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(okResponse(data));

    const result = await dispatchPreToolUseGate({
      toolName: 'Read',
      toolInput: { file_path: '/x' },
    });

    expect(result.block).toBe(false);
    expect(result.response).toEqual(data);
  });

  it('fails open ({block:false}) on a non-OK status', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      okResponse({ decision: 'block' }, 500),
    );

    const result = await dispatchPreToolUseGate({
      toolName: 'Bash',
      toolInput: {},
    });

    expect(result).toEqual({ block: false });
  });

  it('fails open ({block:false}) on a network error / timeout abort', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(
      new Error('AbortError: timed out'),
    );

    const result = await dispatchPreToolUseGate({
      toolName: 'Bash',
      toolInput: {},
    });

    expect(result).toEqual({ block: false });
  });

  it('POSTs a byte-identical body (hardcoded hook_event_name) with the proxy token header', async () => {
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(okResponse({}));

    await dispatchPreToolUseGate({
      toolName: 'Bash',
      toolInput: { command: 'ls' },
      toolUseId: 'tu_1',
      sessionId: 'sess_1',
    });

    expect(fetchSpy).toHaveBeenCalledWith(
      'http://127.0.0.1:3002/hooks/PreToolUse',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          'content-type': 'application/json',
          'x-deus-proxy-token': 'test-token',
        }),
        body: JSON.stringify({
          hook_event_name: 'PreToolUse',
          tool_name: 'Bash',
          tool_input: { command: 'ls' },
          tool_use_id: 'tu_1',
          session_id: 'sess_1',
        }),
      }),
    );
  });
});

describe('createPreToolUseHook (Claude SDK adapter, no regression)', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    process.env.HOOK_DISPATCH_ENABLED = 'true';
    process.env.DEUS_PROXY_TOKEN = 'test-token';
  });

  afterEach(() => {
    process.env = { ...originalEnv };
    vi.restoreAllMocks();
  });

  const input = {
    hook_event_name: 'PreToolUse',
    tool_name: 'Bash',
    tool_input: { command: 'ls' },
    tool_use_id: 'tu_1',
    session_id: 'sess_1',
  };

  it('emits {decision:block, reason} on a block', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      okResponse({ decision: 'block', reason: 'nope' }),
    );

    const hook = createPreToolUseHook('127.0.0.1', 3002, 'test-token');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const out = await hook(input as any, undefined as any, {} as any);

    expect(out).toEqual({ decision: 'block', reason: 'nope' });
  });

  it('forwards the raw dispatch response on a non-block success', async () => {
    const data = {
      hookSpecificOutput: { additionalContext: 'ctx' },
    };
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(okResponse(data));

    const hook = createPreToolUseHook('127.0.0.1', 3002, 'test-token');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const out = await hook(input as any, undefined as any, {} as any);

    expect(out).toEqual(data);
  });

  it('returns {} on an unreachable service (fail-open)', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('ECONNREFUSED'));

    const hook = createPreToolUseHook('127.0.0.1', 3002, 'test-token');
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const out = await hook(input as any, undefined as any, {} as any);

    expect(out).toEqual({});
  });
});
