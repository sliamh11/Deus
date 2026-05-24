import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { HookDispatchService } from './hook-dispatch-service.js';

describe('HookDispatchService — fanOut', () => {
  let service: HookDispatchService;

  beforeEach(() => {
    service = new HookDispatchService();
  });

  afterEach(async () => {
    await service.stop();
  });

  it('returns {} when no observers are registered', async () => {
    const result = await service.fanOut('PreToolUse', { tool_name: 'Bash' });
    expect(result).toEqual({});
  });

  it('calls the registered observer with event and payload', async () => {
    const cb = vi.fn().mockResolvedValue({});
    service.registerObserver('PreToolUse', cb);

    const payload = { tool_name: 'Read', tool_input: { file_path: '/foo' } };
    await service.fanOut('PreToolUse', payload);

    expect(cb).toHaveBeenCalledOnce();
    expect(cb).toHaveBeenCalledWith('PreToolUse', payload);
  });

  it('calls all observers when multiple are registered for the same event', async () => {
    const cb1 = vi.fn().mockResolvedValue({});
    const cb2 = vi.fn().mockResolvedValue({});
    service.registerObserver('PostToolUse', cb1);
    service.registerObserver('PostToolUse', cb2);

    await service.fanOut('PostToolUse', {});

    expect(cb1).toHaveBeenCalledOnce();
    expect(cb2).toHaveBeenCalledOnce();
  });

  it('does not call observers registered for a different event', async () => {
    const cb = vi.fn().mockResolvedValue({});
    service.registerObserver('PostToolUse', cb);

    await service.fanOut('PreToolUse', {});

    expect(cb).not.toHaveBeenCalled();
  });

  it('aggregates additionalContext from multiple observers', async () => {
    service.registerObserver('PreToolUse', async () => ({
      hookSpecificOutput: {
        hookEventName: 'PreToolUse',
        additionalContext: 'context-a',
      },
    }));
    service.registerObserver('PreToolUse', async () => ({
      hookSpecificOutput: {
        hookEventName: 'PreToolUse',
        additionalContext: 'context-b',
      },
    }));

    const result = await service.fanOut('PreToolUse', {});
    const ctx = (result.hookSpecificOutput as Record<string, unknown>)
      ?.additionalContext as string;
    expect(ctx).toContain('context-a');
    expect(ctx).toContain('context-b');
  });

  it('continues fan-out and returns working observer result when one observer throws', async () => {
    const throwing = vi.fn().mockRejectedValue(new Error('observer boom'));
    const working = vi.fn().mockResolvedValue({ decision: 'approve' });

    service.registerObserver('PreToolUse', throwing);
    service.registerObserver('PreToolUse', working);

    const result = await service.fanOut('PreToolUse', {});

    expect(working).toHaveBeenCalledOnce();
    expect(result).toMatchObject({ decision: 'approve' });
  });

  it('returns {} when all observers throw', async () => {
    service.registerObserver(
      'PreToolUse',
      vi.fn().mockRejectedValue(new Error('boom')),
    );

    const result = await service.fanOut('PreToolUse', {});
    expect(result).toEqual({});
  });

  it('preserves non-additionalContext fields from observer responses', async () => {
    service.registerObserver('PreToolUse', async () => ({
      decision: 'block',
      reason: 'test block',
    }));

    const result = await service.fanOut('PreToolUse', {});
    expect(result).toMatchObject({ decision: 'block', reason: 'test block' });
  });
});

describe('HookDispatchService — HTTP server', () => {
  let service: HookDispatchService;
  // Use unique ports per describe block to avoid EADDRINUSE in parallel runs
  const BASE_PORT = 19200;

  beforeEach(() => {
    service = new HookDispatchService();
  });

  afterEach(async () => {
    await service.stop();
  });

  it('starts and responds to POST /hooks/:event', async () => {
    const port = BASE_PORT;
    await service.start(port);

    const cb = vi.fn().mockResolvedValue({ decision: 'approve' });
    service.registerObserver('PreToolUse', cb);

    const res = await fetch(`http://127.0.0.1:${port}/hooks/PreToolUse`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ tool_name: 'Bash' }),
    });

    expect(res.ok).toBe(true);
    const body = (await res.json()) as Record<string, unknown>;
    expect(body).toMatchObject({ decision: 'approve' });
    expect(cb).toHaveBeenCalledOnce();
  });

  it('returns 404 for GET /hooks/:event', async () => {
    const port = BASE_PORT + 1;
    await service.start(port);

    const res = await fetch(`http://127.0.0.1:${port}/hooks/PreToolUse`, {
      method: 'GET',
    });
    expect(res.status).toBe(404);
  });

  it('returns 404 for unknown paths', async () => {
    const port = BASE_PORT + 2;
    await service.start(port);

    const res = await fetch(`http://127.0.0.1:${port}/unknown`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    });
    expect(res.status).toBe(404);
  });

  it('returns 400 for invalid JSON body', async () => {
    const port = BASE_PORT + 3;
    await service.start(port);

    const res = await fetch(`http://127.0.0.1:${port}/hooks/PreToolUse`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: 'not valid json',
    });
    expect(res.status).toBe(400);
  });

  it('returns {} (200) when observer throws during HTTP request', async () => {
    const port = BASE_PORT + 4;
    await service.start(port);

    service.registerObserver(
      'PostToolUse',
      vi.fn().mockRejectedValue(new Error('observer fail')),
    );

    const res = await fetch(`http://127.0.0.1:${port}/hooks/PostToolUse`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ tool_name: 'Write' }),
    });
    expect(res.ok).toBe(true);
    const body = await res.json();
    expect(body).toEqual({});
  });

  it('returns {} when no observers registered for the event', async () => {
    const port = BASE_PORT + 5;
    await service.start(port);

    const res = await fetch(`http://127.0.0.1:${port}/hooks/PreToolUse`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ tool_name: 'Glob' }),
    });
    expect(res.ok).toBe(true);
    const body = await res.json();
    expect(body).toEqual({});
  });
});
