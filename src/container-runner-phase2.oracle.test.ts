/**
 * Oracle tests for Ingress Gateway Phase 2 — buildContainerArgs env isolation.
 * Authored from the spec BEFORE implementation exists (oracle-author warden).
 * These tests are RED against origin/main and must go GREEN once the
 * implementer adds:
 *   - publicIngress?: boolean to ContainerConfig (types.ts)
 *   - buildContainerArgs branch for publicIngress=true:
 *       * omit LINEAR_API_KEY
 *       * mint scoped token via getOrCreateScopedToken
 *       * push -e DEUS_TOOL_PROFILE=webhook
 *       * push -e DEUS_CURATED_TOOLS=<list>
 *   - default (no publicIngress) path is byte-identical to pre-change behavior
 *
 * Every test is tagged @oracle so the oracle-integrity gate can protect it.
 *
 * TEST-SEAM REQUIREMENTS imposed on the implementer:
 *   - buildContainerArgs MUST be exported from src/container-runner.ts.
 *     It is currently module-internal. The implementer must add:
 *       export function buildContainerArgs(...) { ... }
 *     This export is the ONLY addition needed to src/container-runner.ts for
 *     testability; the function's internal behavior is what the oracle tests.
 *   - getOrCreateScopedToken and isToolAllowedForToken must be exported from
 *     src/group-tokens.ts (per the Phase 2 spec).
 */

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import type { RegisteredGroup } from './types.js';

// ---------------------------------------------------------------------------
// Module mocks (same pattern as container-runner.test.ts)
// ---------------------------------------------------------------------------

vi.mock('./config.js', () => ({
  CONTAINER_IMAGE: 'deus-agent:latest',
  CONTAINER_MAX_OUTPUT_SIZE: 10485760,
  CONTAINER_TIMEOUT: 1800000,
  CONFIG_DIR: '/tmp/deus-test-config',
  CONTEXT_AUTO_COMPACT_PCT: 75,
  CONTEXT_WARN_PCT: 70,
  CREDENTIAL_PROXY_PORT: 3001,
  DATA_DIR: '/tmp/deus-test-data',
  DEUS_CONTEXT_FILE_MAX_CHARS: '',
  DEUS_OPENAI_MODEL: '',
  GROUPS_DIR: '/tmp/deus-test-groups',
  HOME_DIR: '/tmp/deus-test-home',
  IDLE_TIMEOUT: 1800000,
  LLAMA_CPP_AGENT_MODEL: '',
  LLAMA_CPP_MODEL: '',
  LLAMA_CPP_PORT: '8765',
  TIMEZONE: 'America/Los_Angeles',
  TOOL_PROXY_PORT: 3003,
}));

vi.mock('./logger.js', () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock('./container-runtime.js', () => ({
  CONTAINER_HOST_GATEWAY: 'host.docker.internal',
  CONTAINER_RUNTIME_BIN: 'docker',
  hostGatewayArgs: vi.fn(() => []),
  readonlyMountArgs: vi.fn((hostPath: string, containerPath: string) => [
    '-v',
    `${hostPath}:${containerPath}:ro`,
  ]),
}));

vi.mock('./credential-proxy.js', () => ({
  detectAuthMode: vi.fn(() => 'api-key'),
}));

vi.mock('./group-folder.js', () => ({
  resolveGroupFolderPath: vi.fn(
    (folder: string) => `/tmp/deus-test-groups/${folder}`,
  ),
  resolveGroupIpcPath: vi.fn(
    (folder: string) => `/tmp/deus-test-groups/${folder}/ipc`,
  ),
}));

// The group-tokens mock must forward to the REAL module so that
// getOrCreateScopedToken's scoping behavior is tested end-to-end.
// We use vi.importActual to let both the oracle and the new API work through
// the real module — the mock is needed only to give the oracle test visibility
// into scoped vs. unscoped token behavior via isToolAllowedForToken.
//
// NOTE: If the implementer also mocks group-tokens for their own test, they
// MUST ensure this oracle file uses the real module. The simplest way is to
// not mock group-tokens at all in this file — which is what we do here.
//
// We DO NOT mock group-tokens.js here on purpose. The real module is used.

vi.mock('./db.js', () => ({
  getProjectById: vi.fn(() => undefined),
}));

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Parse the flat args array from buildContainerArgs/spawn into a map of
 * env vars: { 'LINEAR_API_KEY': 'abc', 'DEUS_TOOL_PROFILE': 'webhook', ... }
 */
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

// ---------------------------------------------------------------------------
// Oracle case (b): default group — byte-identical pre-change behavior
// ---------------------------------------------------------------------------

describe('oracle (b): default group (no publicIngress) — regression guard', () => {
  const savedLinearKey = process.env.LINEAR_API_KEY;

  beforeEach(() => {
    process.env.LINEAR_API_KEY = 'valid-linear-key-abc123';
  });

  afterEach(() => {
    if (savedLinearKey === undefined) {
      delete process.env.LINEAR_API_KEY;
    } else {
      process.env.LINEAR_API_KEY = savedLinearKey;
    }
  });

  it('default group includes LINEAR_API_KEY env entry when key is set', async () => {
    // @oracle: default group args include LINEAR_API_KEY — ALL normal runs unaffected
    const { buildContainerArgs } = await import('./container-runner.js');
    const group: RegisteredGroup = {
      name: 'Normal Group',
      folder: 'normal-group',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
      // No containerConfig, no publicIngress
    };

    const args = buildContainerArgs(
      [],
      'test-container',
      'claude',
      'iid-123',
      group,
    );
    const env = parseEnvArgs(args);

    // LINEAR_API_KEY MUST be present for a normal group
    expect(env).toHaveProperty('LINEAR_API_KEY');
    expect(env['LINEAR_API_KEY']).toBe('valid-linear-key-abc123');
  });

  it('default group does NOT include DEUS_TOOL_PROFILE', async () => {
    // @oracle: no DEUS_TOOL_PROFILE on normal runs — flag is additive only
    const { buildContainerArgs } = await import('./container-runner.js');
    const group: RegisteredGroup = {
      name: 'Normal Group',
      folder: 'normal-group-b',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
    };

    const args = buildContainerArgs(
      [],
      'test-container-b',
      'claude',
      'iid-456',
      group,
    );
    const env = parseEnvArgs(args);

    expect(env).not.toHaveProperty('DEUS_TOOL_PROFILE');
    expect(env).not.toHaveProperty('DEUS_CURATED_TOOLS');
  });

  it('undefined group (anonymous) still includes LINEAR_API_KEY and no DEUS_TOOL_PROFILE', async () => {
    // @oracle: regression guard — no-group path is also byte-identical
    const { buildContainerArgs } = await import('./container-runner.js');

    const args = buildContainerArgs(
      [],
      'test-container-anon',
      'claude',
      'iid-789',
      undefined,
    );
    const env = parseEnvArgs(args);

    expect(env).toHaveProperty('LINEAR_API_KEY');
    expect(env).not.toHaveProperty('DEUS_TOOL_PROFILE');
  });
});

// ---------------------------------------------------------------------------
// Oracle case (a): publicIngress group — env isolation
// ---------------------------------------------------------------------------

describe('oracle (a): publicIngress=true group — reduced-privilege env', () => {
  const savedLinearKey = process.env.LINEAR_API_KEY;

  beforeEach(async () => {
    process.env.LINEAR_API_KEY = 'real-linear-key-xyz789';
    // Clear token state so each test starts fresh
    const { _clearTokens } = await import('./group-tokens.js');
    _clearTokens();
  });

  afterEach(async () => {
    if (savedLinearKey === undefined) {
      delete process.env.LINEAR_API_KEY;
    } else {
      process.env.LINEAR_API_KEY = savedLinearKey;
    }
    // Clean up token state after each test
    const { _clearTokens } = await import('./group-tokens.js');
    _clearTokens();
  });

  it('publicIngress group OMITS LINEAR_API_KEY even when process.env has it', async () => {
    // @oracle: R2 — no raw secrets in webhook container (LINEAR_API_KEY omitted)
    const { buildContainerArgs } = await import('./container-runner.js');
    const group: RegisteredGroup = {
      name: 'Webhook Group',
      folder: 'webhook-group',
      trigger: '@Webhook',
      added_at: new Date().toISOString(),
      containerConfig: {
        publicIngress: true,
        // curatedTools not set; implementation uses [] default per spec
      },
    };

    const args = buildContainerArgs(
      [],
      'webhook-container',
      'claude',
      'iid-wh1',
      group,
    );
    const env = parseEnvArgs(args);

    // LINEAR_API_KEY MUST NOT appear anywhere in args for a publicIngress group
    expect(env).not.toHaveProperty('LINEAR_API_KEY');
    // Belt-and-suspenders: raw string scan to catch any format variation
    const argString = args.join(' ');
    expect(argString).not.toMatch(/LINEAR_API_KEY=/);
  });

  it('publicIngress group includes DEUS_TOOL_PROFILE=webhook', async () => {
    // @oracle: R1 — webhook container gets reduced-privilege tool profile flag
    const { buildContainerArgs } = await import('./container-runner.js');
    const group: RegisteredGroup = {
      name: 'Webhook Group',
      folder: 'webhook-group-profile',
      trigger: '@Webhook',
      added_at: new Date().toISOString(),
      containerConfig: {
        publicIngress: true,
      },
    };

    const args = buildContainerArgs(
      [],
      'webhook-container-p',
      'claude',
      'iid-wh2',
      group,
    );
    const env = parseEnvArgs(args);

    expect(env['DEUS_TOOL_PROFILE']).toBe('webhook');
  });

  it('publicIngress group mints a SCOPED token (isToolAllowedForToken enforces scope)', async () => {
    // @oracle: R2 — the minted DEUS_PROXY_TOKEN is a scoped token, not unscoped
    const { buildContainerArgs } = await import('./container-runner.js');
    const { isToolAllowedForToken } = await import('./group-tokens.js');

    const curatedTools = ['Read', 'Glob'];
    const group: RegisteredGroup = {
      name: 'Webhook Group',
      folder: 'webhook-group-scoped',
      trigger: '@Webhook',
      added_at: new Date().toISOString(),
      containerConfig: {
        publicIngress: true,
        curatedTools,
      },
    };

    const args = buildContainerArgs(
      [],
      'webhook-container-s',
      'claude',
      'iid-wh3',
      group,
    );
    const env = parseEnvArgs(args);

    const mintedToken = env['DEUS_PROXY_TOKEN'];
    expect(mintedToken).toBeDefined();
    expect(mintedToken).toMatch(/^[0-9a-f]{64}$/);

    // The minted token must be scoped to curatedTools only
    // Tools in scope: allowed
    expect(isToolAllowedForToken(mintedToken, 'Read')).toBe(true);
    expect(isToolAllowedForToken(mintedToken, 'Glob')).toBe(true);

    // Tools NOT in scope: rejected — this is the R2 enforcement
    expect(isToolAllowedForToken(mintedToken, 'Bash')).toBe(false);
    expect(isToolAllowedForToken(mintedToken, 'Write')).toBe(false);
    expect(isToolAllowedForToken(mintedToken, 'mcp__deus__memory')).toBe(false);
  });

  // Code-review finding (GPT co-gate): the host mints the scoped token + exports
  // DEUS_CURATED_TOOLS, so it must bound the curated set by SAFE_CURATED — not the
  // raw config — or a sensitive tool named in config would be authorized by the
  // tool-proxy. NOT @oracle-tagged — added post-implementation from review.
  it('host filters a SENSITIVE curated tool out of the token scope AND env', async () => {
    const { buildContainerArgs } = await import('./container-runner.js');
    const { isToolAllowedForToken } = await import('./group-tokens.js');
    const group: RegisteredGroup = {
      name: 'Webhook Group',
      folder: 'webhook-group-sensitive',
      trigger: '@Webhook',
      added_at: new Date().toISOString(),
      containerConfig: {
        publicIngress: true,
        // 'Read' is in SAFE_CURATED; the rest are sensitive / not allowlisted.
        curatedTools: ['Read', 'mcp__deus__memory', 'Bash', 'Write'],
      },
    };
    const args = buildContainerArgs(
      [],
      'wh-sensitive',
      'claude',
      'iid-wh-x',
      group,
    );
    const env = parseEnvArgs(args);
    const token = env['DEUS_PROXY_TOKEN'];

    // The safe tool survives; the sensitive ones are dropped from BOTH layers.
    expect(isToolAllowedForToken(token, 'Read')).toBe(true);
    expect(isToolAllowedForToken(token, 'mcp__deus__memory')).toBe(false);
    expect(isToolAllowedForToken(token, 'Bash')).toBe(false);

    const curated = (env['DEUS_CURATED_TOOLS'] ?? '')
      .split(',')
      .filter(Boolean);
    expect(curated).toContain('Read');
    expect(curated).not.toContain('mcp__deus__memory');
    expect(curated).not.toContain('Bash');
    expect(curated).not.toContain('Write');
  });

  // Code-review finding (GPT co-gate): the webhook profile is enforced only on
  // the Claude path; openai/llama-cpp bypass buildAllowedTools. buildContainerArgs
  // must refuse to launch a publicIngress group on a non-Claude backend (fail-closed).
  // NOT @oracle-tagged — added post-implementation from review, not the blind spec.
  it('publicIngress group THROWS for a non-Claude backend (openai)', async () => {
    const { buildContainerArgs } = await import('./container-runner.js');
    const group: RegisteredGroup = {
      name: 'Webhook Group',
      folder: 'webhook-group-openai',
      trigger: '@Webhook',
      added_at: new Date().toISOString(),
      containerConfig: { publicIngress: true },
    };
    expect(() =>
      buildContainerArgs([], 'wh-openai', 'openai', 'iid-wh-o', group),
    ).toThrow(/claude/i);
  });

  it('publicIngress group THROWS for a non-Claude backend (llama-cpp)', async () => {
    const { buildContainerArgs } = await import('./container-runner.js');
    const group: RegisteredGroup = {
      name: 'Webhook Group',
      folder: 'webhook-group-llama',
      trigger: '@Webhook',
      added_at: new Date().toISOString(),
      containerConfig: { publicIngress: true },
    };
    expect(() =>
      buildContainerArgs([], 'wh-llama', 'llama-cpp', 'iid-wh-l', group),
    ).toThrow(/claude/i);
  });

  it('default group on a non-Claude backend is unaffected (no throw)', async () => {
    const { buildContainerArgs } = await import('./container-runner.js');
    const group: RegisteredGroup = {
      name: 'Normal Group',
      folder: 'normal-openai',
      trigger: '@Deus',
      added_at: new Date().toISOString(),
    };
    expect(() =>
      buildContainerArgs([], 'normal-openai-c', 'openai', 'iid-n-o', group),
    ).not.toThrow();
  });

  it('publicIngress group includes DEUS_CURATED_TOOLS env var with the curated list', async () => {
    // @oracle: host->container protocol — DEUS_CURATED_TOOLS injected as comma list
    const { buildContainerArgs } = await import('./container-runner.js');

    const curatedTools = ['Read', 'Glob', 'WebSearch'];
    const group: RegisteredGroup = {
      name: 'Webhook Group',
      folder: 'webhook-group-curated',
      trigger: '@Webhook',
      added_at: new Date().toISOString(),
      containerConfig: {
        publicIngress: true,
        curatedTools,
      },
    };

    const args = buildContainerArgs(
      [],
      'webhook-container-c',
      'claude',
      'iid-wh4',
      group,
    );
    const env = parseEnvArgs(args);

    expect(env).toHaveProperty('DEUS_CURATED_TOOLS');
    const curated = (env['DEUS_CURATED_TOOLS'] ?? '').split(',');
    expect(curated).toContain('Read');
    expect(curated).toContain('Glob');
    expect(curated).toContain('WebSearch');
  });
});
