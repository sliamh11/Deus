import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  fetchMemoryContext,
  createMemoryRetrievalHook,
} from './memory-retrieval-hook.js';

describe('fetchMemoryContext', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    process.env.DEUS_PROXY_TOKEN = 'test-token';
    process.env.CREDENTIAL_PROXY_PORT = '3001';
    process.env.DEUS_PROXY_HOST = '127.0.0.1';
  });

  afterEach(() => {
    process.env = { ...originalEnv };
    vi.restoreAllMocks();
  });

  it('returns empty string when DEUS_PROXY_TOKEN is not set', async () => {
    delete process.env.DEUS_PROXY_TOKEN;
    const result = await fetchMemoryContext('test query');
    expect(result).toBe('');
  });

  it('returns context on successful bridge response', async () => {
    const mockResponse = {
      context: '=== Auto-retrieved memory ===\ntest content\n=== End ===',
      paths: ['CLAUDE.md'],
      confidence: 0.72,
      fell_back: false,
    };

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mockResponse), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    );

    const result = await fetchMemoryContext('what is my timezone?');
    expect(result).toContain('Auto-retrieved memory');
    expect(result).toContain('test content');
  });

  it('sends correct headers and body', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ context: '', fell_back: true }), {
        status: 200,
      }),
    );

    await fetchMemoryContext('test query', 'container-claude');

    expect(fetch).toHaveBeenCalledWith(
      'http://127.0.0.1:3001/memory/query',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({
          'x-deus-proxy-token': 'test-token',
          'x-deus-source': 'container-claude',
        }),
        body: JSON.stringify({
          query: 'test query',
          source: 'container-claude',
        }),
      }),
    );
  });

  it('returns empty string on HTTP error', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('Internal Server Error', { status: 500 }),
    );

    const result = await fetchMemoryContext('test');
    expect(result).toBe('');
  });

  it('returns empty string on network error', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('ECONNREFUSED'));

    const result = await fetchMemoryContext('test');
    expect(result).toBe('');
  });

  it('returns empty string on timeout (abort)', async () => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          setTimeout(
            () => reject(new DOMException('Aborted', 'AbortError')),
            10,
          );
        }),
    );

    const result = await fetchMemoryContext('test');
    expect(result).toBe('');
  });

  it('returns empty string when fell_back is true', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          context: '',
          paths: [],
          confidence: 0.2,
          fell_back: true,
        }),
        { status: 200 },
      ),
    );

    const result = await fetchMemoryContext('gibberish');
    expect(result).toBe('');
  });
});

describe('createMemoryRetrievalHook', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    process.env.DEUS_PROXY_TOKEN = 'test-token';
    process.env.CREDENTIAL_PROXY_PORT = '3001';
    process.env.DEUS_PROXY_HOST = '127.0.0.1';
  });

  afterEach(() => {
    process.env = { ...originalEnv };
    vi.restoreAllMocks();
  });

  it('returns empty object when prompt is missing', async () => {
    const hook = createMemoryRetrievalHook();
    const result = await hook({});
    expect(result).toEqual({});
  });

  it('returns additionalContext on successful retrieval', async () => {
    const mockResponse = {
      context: '=== Memory ===\nsome context\n=== End ===',
      paths: ['test.md'],
      confidence: 0.7,
      fell_back: false,
    };

    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify(mockResponse), { status: 200 }),
    );

    const hook = createMemoryRetrievalHook();
    const result = await hook({ prompt: 'hello' });

    expect(result).toEqual({
      hookSpecificOutput: {
        hookEventName: 'UserPromptSubmit',
        additionalContext: '=== Memory ===\nsome context\n=== End ===',
      },
    });
  });

  it('returns empty object when bridge returns empty context', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ context: '', fell_back: true }), {
        status: 200,
      }),
    );

    const hook = createMemoryRetrievalHook();
    const result = await hook({ prompt: 'hello' });
    expect(result).toEqual({});
  });
});
