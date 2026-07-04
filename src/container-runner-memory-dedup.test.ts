/**
 * LIA-355: buildContainerArgs must forward the DEUS_MEMORY_DEDUP kill-switch —
 * container env is enumerated (no bulk passthrough), so without the forward the
 * in-container dedup toggle reads undefined and can never be turned off.
 *
 * Mirrors the mock + parseEnvArgs pattern of container-runner-phase2.oracle.test.ts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { RegisteredGroup } from './types.js';

vi.mock('./group-tokens.js', () => ({
  getOrCreateGroupToken: vi.fn(() => 'group-token'),
  getOrCreateScopedToken: vi.fn(() => 'scoped-token'),
}));

vi.mock('./logger.js', () => ({
  logger: {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
    debug: vi.fn(),
  },
}));

function parseEnvArgs(args: string[]): Record<string, string> {
  const env: Record<string, string> = {};
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '-e' && i + 1 < args.length) {
      const pair = args[i + 1];
      const eqIdx = pair.indexOf('=');
      if (eqIdx !== -1) {
        env[pair.slice(0, eqIdx)] = pair.slice(eqIdx + 1);
      }
    }
  }
  return env;
}

const group: RegisteredGroup = {
  name: 'Normal Group',
  folder: 'normal-group-dedup',
  trigger: '@Deus',
  added_at: new Date().toISOString(),
};

describe('buildContainerArgs DEUS_MEMORY_DEDUP forwarding (LIA-355)', () => {
  const saved = process.env.DEUS_MEMORY_DEDUP;

  beforeEach(() => {
    delete process.env.DEUS_MEMORY_DEDUP;
  });

  afterEach(() => {
    if (saved === undefined) delete process.env.DEUS_MEMORY_DEDUP;
    else process.env.DEUS_MEMORY_DEDUP = saved;
  });

  it('forwards the kill-switch value when set on the host', async () => {
    process.env.DEUS_MEMORY_DEDUP = '0';
    const { buildContainerArgs } = await import('./container-runner.js');
    const env = parseEnvArgs(
      buildContainerArgs([], 'c1', 'claude', 'iid-1', group),
    );
    expect(env['DEUS_MEMORY_DEDUP']).toBe('0');
  });

  it('omits the var when unset on the host (container default-on applies)', async () => {
    const { buildContainerArgs } = await import('./container-runner.js');
    const env = parseEnvArgs(
      buildContainerArgs([], 'c2', 'claude', 'iid-2', group),
    );
    expect(env).not.toHaveProperty('DEUS_MEMORY_DEDUP');
  });
});
