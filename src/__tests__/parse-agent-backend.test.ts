import { describe, it, expect } from 'vitest';

import { parseAgentBackend } from '../agent-runtimes/types.js';

describe('parseAgentBackend', () => {
  it('accepts the three valid backend IDs verbatim', () => {
    expect(parseAgentBackend('claude')).toBe('claude');
    expect(parseAgentBackend('openai')).toBe('openai');
    expect(parseAgentBackend('llama-cpp')).toBe('llama-cpp');
  });

  it('rejects unknown strings', () => {
    expect(parseAgentBackend('gpt-4')).toBeUndefined();
    expect(parseAgentBackend('ollama')).toBeUndefined();
    expect(parseAgentBackend('llama')).toBeUndefined();
    expect(parseAgentBackend('')).toBeUndefined();
  });

  it('rejects non-string types', () => {
    expect(parseAgentBackend(null)).toBeUndefined();
    expect(parseAgentBackend(undefined)).toBeUndefined();
    expect(parseAgentBackend(42)).toBeUndefined();
    expect(parseAgentBackend({})).toBeUndefined();
    expect(parseAgentBackend([])).toBeUndefined();
    expect(parseAgentBackend(true)).toBeUndefined();
  });

  it('is case-sensitive (no automatic lowercasing)', () => {
    // Callers must lowercase first; the function is a strict gate.
    expect(parseAgentBackend('Claude')).toBeUndefined();
    expect(parseAgentBackend('OPENAI')).toBeUndefined();
    expect(parseAgentBackend('Llama-CPP')).toBeUndefined();
  });
});
