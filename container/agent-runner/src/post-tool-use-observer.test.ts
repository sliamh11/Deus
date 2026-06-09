import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { createPostToolUseObserverHook } from './post-tool-use-observer.js';

/**
 * Regression guard for LIA-199 part 2: the PostToolUse observer consults the
 * in-container HookDispatchService at 127.0.0.1:3002 via dispatchHost() — NOT
 * DEUS_PROXY_HOST / host.docker.internal, which addresses HOST-side services and
 * fails-to-reach from inside the container (observed live in the #744 smoke test
 * as `[post-tool-use-observer] dispatch failed: fetch failed`).
 */
describe('createPostToolUseObserverHook (consult targets the container-local :3002 service)', () => {
  const originalEnv = { ...process.env };

  const input = {
    hook_event_name: 'PostToolUse',
    tool_name: 'Bash',
    tool_input: { command: 'ls /workspace' },
    tool_response: { stdout: '' },
    tool_use_id: 'tu_1',
    session_id: 'sess_1',
  };

  beforeEach(() => {
    delete process.env.HOOK_DISPATCH_HOST;
    delete process.env.DEUS_PROXY_HOST;
    delete process.env.DEUS_PROXY_TOKEN;
    delete process.env.HOOK_DISPATCH_PORT;
  });

  afterEach(() => {
    process.env = { ...originalEnv };
    vi.restoreAllMocks();
  });

  it('defaults to 127.0.0.1 (dispatchHost), NOT DEUS_PROXY_HOST/host.docker.internal', async () => {
    process.env.DEUS_PROXY_HOST = 'host.docker.internal'; // must be ignored now
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue({} as Response);

    const hook = createPostToolUseObserverHook();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await hook(input as any, undefined as any, {} as any);

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy.mock.calls[0][0]).toBe(
      'http://127.0.0.1:3002/hooks/PostToolUse',
    );
  });

  it('honors a HOOK_DISPATCH_HOST override', async () => {
    process.env.HOOK_DISPATCH_HOST = '10.0.0.5';
    const fetchSpy = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue({} as Response);

    const hook = createPostToolUseObserverHook();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    await hook(input as any, undefined as any, {} as any);

    expect(fetchSpy.mock.calls[0][0]).toBe(
      'http://10.0.0.5:3002/hooks/PostToolUse',
    );
  });

  it('is non-blocking: returns {} even when the consult rejects', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('fetch failed'));
    // The fire-and-forget .catch() logs a single warn; suppress it to keep output clean.
    vi.spyOn(console, 'warn').mockImplementation(() => {});

    const hook = createPostToolUseObserverHook();
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const out = await hook(input as any, undefined as any, {} as any);

    expect(out).toEqual({});
  });
});
