/**
 * Tests for src/tool-registry.ts (LIA-222)
 *
 * Strategy: The module bakes REGISTRY_PATH at load time using `homeDir` from
 * ./platform.js and `fs.readFileSync` at runtime. To control both:
 *   1. Mock `./platform.js` so `homeDir` is a predictable string.
 *   2. Mock `fs` so `readFileSync` returns our fixture JSON.
 *   3. Mock `./logger.js` to silence warn/debug output in tests.
 *   4. Use `vi.resetModules()` + dynamic `import()` inside `freshModule()` so
 *      the module-level `cachedRegistry` starts null for every test (no
 *      export-level reset hook exists in the source).
 *
 * The controlled homeDir is '/tmp/deus-test-home', so REGISTRY_PATH becomes
 * '/tmp/deus-test-home/.deus/tool-registry.json'.
 *
 * Important mock-ordering rule: after `vi.resetModules()` the module registry
 * is cleared, so we MUST import `fs` before `tool-registry` to capture the
 * mock instance that `tool-registry` will receive on its own import of `fs`.
 * Both calls share the same mock because `vi.mock('fs', ...)` is hoisted, but
 * `vi.resetModules()` resets the singleton — re-importing `fs` first pins the
 * reference before `tool-registry` imports it.
 */

import path from 'path';
import { describe, expect, it, vi } from 'vitest';

// ── Module-level mocks (hoisted, apply to every import in this file) ─────────

vi.mock('./platform.js', () => ({
  homeDir: '/tmp/deus-test-home',
}));

vi.mock('./logger.js', () => ({
  logger: {
    debug: vi.fn(),
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      readFileSync: vi.fn(),
    },
  };
});

// ── Helpers ───────────────────────────────────────────────────────────────────

/**
 * The controlled registry path the module will read. Derived via path.join so
 * it matches the module's own path.join(homeDir, ...) on every OS — on Windows
 * that yields backslash separators, so a hardcoded forward-slash literal would
 * fail the `toHaveBeenCalledWith(REGISTRY_PATH, ...)` assertion below.
 */
const REGISTRY_PATH = path.join(
  '/tmp/deus-test-home',
  '.deus',
  'tool-registry.json',
);

/** Minimal valid registry with two tools used across most tests. */
const VALID_REGISTRY = JSON.stringify({
  tools: {
    espn: {
      binary: '/usr/local/bin/espn-pp-cli',
      env: {},
    },
    flights: {
      binary: '/usr/local/bin/flight-goat-pp-cli',
      env: { KAYAK_API_KEY: '${KAYAK_API_KEY}' },
    },
  },
});

/**
 * Re-import tool-registry with a fresh module state (cachedRegistry = null).
 *
 * Returns the module exports plus the `mockReadFileSync` that the freshly-
 * imported module will actually call. Callers MUST configure the mock AFTER
 * this call (not before) because `vi.resetModules()` clears all instances.
 */
async function freshModule() {
  vi.resetModules();
  // Import fs FIRST so we pin the mock instance before tool-registry imports it.
  const fs = await import('fs');
  const mockReadFileSync = vi.mocked(fs.default.readFileSync);
  mockReadFileSync.mockReset();

  const registry = await import('./tool-registry.js');
  return { ...registry, mockReadFileSync };
}

// ── isAllowed ─────────────────────────────────────────────────────────────────

describe('isAllowed', () => {
  it('returns false for an unknown tool', async () => {
    const { isAllowed, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    expect(isAllowed('unknown-tool')).toBe(false);
  });

  it('returns true for a tool that is in the allowlist', async () => {
    const { isAllowed, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    expect(isAllowed('espn')).toBe(true);
  });

  it('returns true for a second known tool', async () => {
    const { isAllowed, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    expect(isAllowed('flights')).toBe(true);
  });

  it('uses hasOwnProperty semantics — prototype key "toString" is NOT allowed', async () => {
    const { isAllowed, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    // 'toString' exists on Object.prototype but must not be an allowed tool name
    expect(isAllowed('toString')).toBe(false);
  });

  it('uses hasOwnProperty semantics — prototype key "constructor" is NOT allowed', async () => {
    const { isAllowed, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    expect(isAllowed('constructor')).toBe(false);
  });

  it('uses hasOwnProperty semantics — "__proto__" is NOT allowed', async () => {
    const { isAllowed, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    expect(isAllowed('__proto__')).toBe(false);
  });

  it('returns false when the registry is malformed JSON', async () => {
    const { isAllowed, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue('not valid json at all {{{');
    expect(isAllowed('espn')).toBe(false);
  });

  it('returns false for any tool when the registry file is missing', async () => {
    const { isAllowed, mockReadFileSync } = await freshModule();
    const enoent = Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
    mockReadFileSync.mockImplementation(() => {
      throw enoent;
    });
    expect(isAllowed('espn')).toBe(false);
    expect(isAllowed('unknown')).toBe(false);
  });
});

// ── loadRegistry ──────────────────────────────────────────────────────────────

describe('loadRegistry', () => {
  it('returns { tools: {} } when the file is missing (ENOENT) — fail-closed', async () => {
    const { loadRegistry, mockReadFileSync } = await freshModule();
    const enoent = Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
    mockReadFileSync.mockImplementation(() => {
      throw enoent;
    });
    expect(loadRegistry()).toEqual({ tools: {} });
  });

  it('returns { tools: {} } when the file contains malformed JSON — fail-closed', async () => {
    const { loadRegistry, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue('{ this is : not valid json }');
    expect(loadRegistry()).toEqual({ tools: {} });
  });

  it('returns { tools: {} } when the "tools" key is missing — fail-closed', async () => {
    const { loadRegistry, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(JSON.stringify({ notTools: {} }));
    expect(loadRegistry()).toEqual({ tools: {} });
  });

  it('returns { tools: {} } when "tools" value is null — fail-closed', async () => {
    const { loadRegistry, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(JSON.stringify({ tools: null }));
    // null is falsy, so the `!parsed.tools` branch fires → fail-closed
    expect(loadRegistry()).toEqual({ tools: {} });
  });

  it('fails closed when "tools" is an array (not a plain object)', async () => {
    // An array satisfies typeof === 'object', so without an explicit
    // Array.isArray guard its numeric index keys would leak through
    // hasOwnProperty as allowlisted tools. loadRegistry must reject it.
    const { loadRegistry, isAllowed, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(JSON.stringify({ tools: ['espn'] }));
    const result = loadRegistry();
    expect(result).toEqual({ tools: {} }); // array rejected, fail-closed
    expect(isAllowed('0')).toBe(false); // numeric index key not allowlisted
  });

  it('returns the parsed registry when the file is valid', async () => {
    const { loadRegistry, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    const result = loadRegistry();
    expect(result.tools).toHaveProperty('espn');
    expect(result.tools['espn'].binary).toBe('/usr/local/bin/espn-pp-cli');
  });

  it('reads from the correct path derived from homeDir', async () => {
    const { loadRegistry, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    loadRegistry();
    expect(mockReadFileSync).toHaveBeenCalledWith(REGISTRY_PATH, 'utf-8');
  });

  it('documents that loadRegistry() always reads from disk — cache is checked only by isAllowed/getToolConfig', async () => {
    // loadRegistry() does NOT check cachedRegistry before reading.
    // Only isAllowed() and getToolConfig() short-circuit via the cache.
    const { loadRegistry, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    loadRegistry();
    loadRegistry();
    // Two calls → two reads (loadRegistry always goes to disk)
    expect(mockReadFileSync).toHaveBeenCalledTimes(2);
  });

  it('isAllowed uses the cache — readFileSync is called only once after initial load', async () => {
    const { isAllowed, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    // First isAllowed call: cachedRegistry is null → calls loadRegistry() → reads fs
    expect(isAllowed('espn')).toBe(true);
    // Second isAllowed call: cachedRegistry is populated → skips loadRegistry()
    expect(isAllowed('flights')).toBe(true);
    expect(mockReadFileSync).toHaveBeenCalledTimes(1);
  });

  it('does NOT cache on ENOENT — retries on next call when file becomes available', async () => {
    const { loadRegistry, mockReadFileSync } = await freshModule();
    const enoent = Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
    // First call: file missing → fail-closed, cachedRegistry stays null
    mockReadFileSync.mockImplementationOnce(() => {
      throw enoent;
    });
    // Second call: file now exists
    mockReadFileSync.mockReturnValueOnce(VALID_REGISTRY);

    const first = loadRegistry();
    expect(first).toEqual({ tools: {} });

    const second = loadRegistry();
    expect(second.tools).toHaveProperty('espn');
  });
});

// ── getToolConfig ─────────────────────────────────────────────────────────────

describe('getToolConfig', () => {
  it('returns null for an unknown tool', async () => {
    const { getToolConfig, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    expect(getToolConfig('unknown')).toBeNull();
  });

  it('returns config for a known tool with empty env', async () => {
    const { getToolConfig, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    const config = getToolConfig('espn');
    expect(config).not.toBeNull();
    expect(config!.binary).toBe('/usr/local/bin/espn-pp-cli');
    expect(config!.env).toEqual({});
  });

  it('resolves ${VAR} placeholders in env values against process.env', async () => {
    const { getToolConfig, mockReadFileSync } = await freshModule();
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    process.env.KAYAK_API_KEY = 'test-api-key-123';
    try {
      const config = getToolConfig('flights');
      expect(config!.env.KAYAK_API_KEY).toBe('test-api-key-123');
    } finally {
      delete process.env.KAYAK_API_KEY;
    }
  });

  it('resolves unset ${MISSING_VAR} to empty string — no raw placeholder leaks', async () => {
    const { getToolConfig, mockReadFileSync } = await freshModule();
    delete process.env.KAYAK_API_KEY;
    mockReadFileSync.mockReturnValue(VALID_REGISTRY);
    const config = getToolConfig('flights');
    expect(config).not.toBeNull();
    const resolved = config!.env.KAYAK_API_KEY;
    // Must be empty string, NOT the literal '${KAYAK_API_KEY}'
    expect(resolved).toBe('');
    expect(resolved).not.toContain('${');
    expect(resolved).not.toContain('KAYAK_API_KEY');
  });

  it('resolves a mix of set and unset placeholders in a single tool', async () => {
    const { getToolConfig, mockReadFileSync } = await freshModule();
    const registry = JSON.stringify({
      tools: {
        mixed: {
          binary: '/usr/bin/mixed',
          env: {
            A: '${DEUS_TEST_SET_A}',
            B: '${DEUS_TEST_UNSET_B}',
            C: 'prefix-${DEUS_TEST_SET_A}-suffix',
          },
        },
      },
    });
    process.env.DEUS_TEST_SET_A = 'hello';
    delete process.env.DEUS_TEST_UNSET_B;
    try {
      mockReadFileSync.mockReturnValue(registry);
      const config = getToolConfig('mixed');
      expect(config!.env.A).toBe('hello');
      expect(config!.env.B).toBe('');
      expect(config!.env.C).toBe('prefix-hello-suffix');
      // B must not leak the raw placeholder
      expect(config!.env.B).not.toContain('${');
    } finally {
      delete process.env.DEUS_TEST_SET_A;
    }
  });

  it('returns null when registry is missing', async () => {
    const { getToolConfig, mockReadFileSync } = await freshModule();
    const enoent = Object.assign(new Error('ENOENT'), { code: 'ENOENT' });
    mockReadFileSync.mockImplementation(() => {
      throw enoent;
    });
    expect(getToolConfig('espn')).toBeNull();
  });
});

// ── resolveEnvPlaceholders (exercised through getToolConfig) ──────────────────

describe('resolveEnvPlaceholders (exercised through getToolConfig)', () => {
  it('leaves a plain string with no placeholders unchanged', async () => {
    const { getToolConfig, mockReadFileSync } = await freshModule();
    const registry = JSON.stringify({
      tools: {
        simple: {
          binary: '/usr/bin/simple',
          env: { PLAIN: 'no-placeholder-here' },
        },
      },
    });
    mockReadFileSync.mockReturnValue(registry);
    expect(getToolConfig('simple')!.env.PLAIN).toBe('no-placeholder-here');
  });

  it('resolves multiple ${VAR} placeholders in a single env value', async () => {
    const { getToolConfig, mockReadFileSync } = await freshModule();
    const registry = JSON.stringify({
      tools: {
        multi: {
          binary: '/usr/bin/multi',
          env: { URL: 'http://${DEUS_TEST_HOST}:${DEUS_TEST_PORT}/api' },
        },
      },
    });
    process.env.DEUS_TEST_HOST = 'localhost';
    process.env.DEUS_TEST_PORT = '8080';
    try {
      mockReadFileSync.mockReturnValue(registry);
      expect(getToolConfig('multi')!.env.URL).toBe('http://localhost:8080/api');
    } finally {
      delete process.env.DEUS_TEST_HOST;
      delete process.env.DEUS_TEST_PORT;
    }
  });

  it('never returns the raw ${PLACEHOLDER} string — unset var → empty string', async () => {
    const { getToolConfig, mockReadFileSync } = await freshModule();
    delete process.env.DEUS_TEST_NEVER_SET;
    const registry = JSON.stringify({
      tools: {
        t: {
          binary: '/usr/bin/t',
          env: { VAL: '${DEUS_TEST_NEVER_SET}' },
        },
      },
    });
    mockReadFileSync.mockReturnValue(registry);
    const val = getToolConfig('t')!.env.VAL;
    expect(val).toBe('');
    // Security: the literal placeholder must never reach the caller
    expect(val).not.toMatch(/\$\{/);
  });
});
