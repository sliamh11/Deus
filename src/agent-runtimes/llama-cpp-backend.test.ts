import { describe, it, expect } from 'vitest';

import { createLlamaCppRuntime } from './llama-cpp-backend.js';
import type { ContainerRuntimeDeps } from './container-backend.js';

const stubDeps: ContainerRuntimeDeps = {
  resolveGroup: () => undefined,
  assistantName: 'Deus',
  registerProcess: () => {},
};

describe('LlamaCppBackend', () => {
  const backend = createLlamaCppRuntime(stubDeps);

  it('returns correct name', () => {
    expect(backend.name()).toBe('llama-cpp');
  });

  it('returns correct capabilities', () => {
    const caps = backend.capabilities();
    expect(caps.shell).toBe(true);
    expect(caps.filesystem).toBe(true);
    // llama-cpp default GGUF is text-only and offline — no web browsing,
    // no multimodal. Documented parity gap; matches docs/MULTI_BACKEND.md.
    expect(caps.web).toBe(false);
    expect(caps.multimodal).toBe(false);
    expect(caps.handoffs).toBe(false);
    // Cross-restart session resume is intentionally not yet supported —
    // the container keeps history in-memory only. `persistent_sessions:
    // false` tells the host not to try replaying a stored session id.
    expect(caps.persistent_sessions).toBe(false);
    expect(caps.tool_streaming).toBe(false);
  });

  it('startOrResume returns default session ref', async () => {
    const ref = await backend.startOrResume({
      prompt: 'test',
      groupFolder: 'test-folder',
      chatJid: 'test@g.us',
      isControlGroup: false,
    });

    expect(ref.backend).toBe('llama-cpp');
    expect(ref.session_id).toBe('');
  });

  it('close resolves without error', async () => {
    await expect(
      backend.close({ backend: 'llama-cpp', session_id: 'llama-cpp-abc' }),
    ).resolves.toBeUndefined();
  });

  it('runTurn returns error when group not found', async () => {
    const result = await backend.runTurn(
      {
        prompt: 'test',
        groupFolder: 'nonexistent',
        chatJid: 'test@g.us',
        isControlGroup: false,
      },
      { backend: 'llama-cpp', session_id: '' },
      () => {},
    );

    expect(result.status).toBe('error');
    expect(result.error).toContain('Group not found');
  });
});
