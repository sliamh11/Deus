import { describe, it, expect } from 'vitest';

import { createClaudeRuntime } from './claude-backend.js';
import type { ContainerRuntimeDeps } from './container-backend.js';

const stubDeps: ContainerRuntimeDeps = {
  resolveGroup: () => undefined,
  assistantName: 'Deus',
  registerProcess: () => {},
};

describe('ClaudeBackend', () => {
  const backend = createClaudeRuntime(stubDeps);

  it('returns correct name', () => {
    expect(backend.name()).toBe('claude');
  });

  it('returns correct capabilities', () => {
    const caps = backend.capabilities();
    expect(caps.shell).toBe(true);
    expect(caps.filesystem).toBe(true);
    expect(caps.web).toBe(true);
    expect(caps.multimodal).toBe(true);
    expect(caps.handoffs).toBe(false);
    expect(caps.persistent_sessions).toBe(true);
    expect(caps.tool_streaming).toBe(true);
  });

  it('startOrResume returns default session ref', async () => {
    const ref = await backend.startOrResume({
      prompt: 'test',
      groupFolder: 'test-folder',
      chatJid: 'test@g.us',
      isControlGroup: false,
    });

    expect(ref.backend).toBe('claude');
    expect(ref.session_id).toBe('');
  });

  it('close resolves without error', async () => {
    await expect(
      backend.close({ backend: 'claude', session_id: 'sess-1' }),
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
      { backend: 'claude', session_id: '' },
      () => {},
    );

    expect(result.status).toBe('error');
    expect(result.error).toContain('Group not found');
  });
});
