/**
 * Tests for setup/codeintel.ts — code-intelligence MCP setup step.
 *
 * Mirrors ollama.test.ts: unit-tests the pure exported helpers (registration
 * command construction, server-command derivation, status roll-up, the pinned
 * package const) plus the shared commandExists integration.
 *
 * NOT unit-tested (parity with ollama.test.ts): the detached background-build
 * path (`child.unref()` via `startBackgroundBuild`) and the subprocess-driven
 * sub-steps (`setupCodegraph`/`setupCodeSearch`). Those are covered by the
 * step's live-run verification (see the plan's Verification section).
 */
import { describe, it, expect } from 'vitest';
import path from 'path';

// ── buildMcpAddArgs ─────────────────────────────────────────────────────────

describe('buildMcpAddArgs', () => {
  it('builds the codegraph registration argv with a -- separator', async () => {
    const { buildMcpAddArgs } = await import('../codeintel.js');
    expect(
      buildMcpAddArgs('codegraph', 'user', ['codegraph', 'serve', '--mcp']),
    ).toEqual([
      'mcp',
      'add',
      '--scope',
      'user',
      'codegraph',
      '--',
      'codegraph',
      'serve',
      '--mcp',
    ]);
  });

  it('places the server command after -- so flags are not parsed by claude', async () => {
    const { buildMcpAddArgs } = await import('../codeintel.js');
    const args = buildMcpAddArgs('code-search', 'user', [
      '/usr/bin/python3',
      '/repo/scripts/code_search_mcp.py',
    ]);
    const sep = args.indexOf('--');
    expect(sep).toBeGreaterThan(-1);
    expect(args.slice(sep + 1)).toEqual([
      '/usr/bin/python3',
      '/repo/scripts/code_search_mcp.py',
    ]);
    // name comes before the separator
    expect(args.slice(0, sep)).toContain('code-search');
  });

  it('honors the requested scope', async () => {
    const { buildMcpAddArgs } = await import('../codeintel.js');
    expect(buildMcpAddArgs('x', 'project', ['cmd'])).toContain('project');
  });
});

// ── codeSearchServerCommand ─────────────────────────────────────────────────

describe('codeSearchServerCommand', () => {
  it('derives [python, <repo>/scripts/code_search_mcp.py] — never hardcoded', async () => {
    const { codeSearchServerCommand } = await import('../codeintel.js');
    const [python, script] = codeSearchServerCommand('python3', '/home/u/deus');
    expect(python).toBe('python3');
    // Use path.join for the expectation so it matches the impl's native
    // separator on every OS (Windows uses backslashes).
    expect(script).toBe(
      path.join('/home/u/deus', 'scripts', 'code_search_mcp.py'),
    );
    // The portability bug being fixed: must NOT reference the old eval venv
    // (separator-agnostic so the assertion holds on Windows too).
    expect(script).not.toContain('.venv');
  });

  it('uses the resolved interpreter (not a fixed venv path)', async () => {
    const { codeSearchServerCommand } = await import('../codeintel.js');
    const [python] = codeSearchServerCommand('python', '/repo');
    expect(python).toBe('python');
  });
});

// ── deusMemoryServerCommand ─────────────────────────────────────────────────

describe('deusMemoryServerCommand', () => {
  it('derives [<repo>/scripts/deus-memory-mcp] — the launcher, no python prefix', async () => {
    const { deusMemoryServerCommand } = await import('../codeintel.js');
    const cmd = deusMemoryServerCommand('/home/u/deus');
    // The launcher selects its own python (choose_python), so the command is
    // just the shim path — not a [python, script] pair like code-search.
    expect(cmd).toEqual([
      path.join('/home/u/deus', 'scripts', 'deus-memory-mcp'),
    ]);
  });

  it('builds a registration argv with the launcher after the -- separator', async () => {
    const { buildMcpAddArgs, deusMemoryServerCommand } =
      await import('../codeintel.js');
    const args = buildMcpAddArgs(
      'deus-memory',
      'user',
      deusMemoryServerCommand('/repo'),
    );
    const sep = args.indexOf('--');
    // No `-e` env flag — procedures default ON in the server itself, and an env
    // would be clobbered by a re-run anyway (we register-if-absent).
    expect(args).not.toContain('-e');
    expect(args.slice(0, sep)).toContain('deus-memory');
    expect(args.slice(sep + 1)).toEqual([
      path.join('/repo', 'scripts', 'deus-memory-mcp'),
    ]);
  });
});

// ── DEUS_MEMORY_DEP_CHECK ───────────────────────────────────────────────────

describe('DEUS_MEMORY_DEP_CHECK', () => {
  it('gates on the mcp server import (mirrors the launcher probe)', async () => {
    const { DEUS_MEMORY_DEP_CHECK } = await import('../codeintel.js');
    // Exact import line so the probe and the server's real requirement can't drift.
    expect(DEUS_MEMORY_DEP_CHECK).toBe(
      'from mcp.server.fastmcp import FastMCP',
    );
  });
});

// ── CODE_SEARCH_DEP_CHECK ───────────────────────────────────────────────────

describe('CODE_SEARCH_DEP_CHECK', () => {
  it('gates on BOTH sqlite_vec and the mcp server import', async () => {
    // Regression guard: the server needs `mcp` (code_search_mcp.py:40), not just
    // sqlite_vec — probing only sqlite_vec let a python3 without mcp register a
    // server that fails to connect. Lock both into the probe.
    const { CODE_SEARCH_DEP_CHECK } = await import('../codeintel.js');
    expect(CODE_SEARCH_DEP_CHECK).toContain('sqlite_vec');
    expect(CODE_SEARCH_DEP_CHECK).toContain('mcp');
    // Mirrors the server's actual import line so the probe and server agree.
    expect(CODE_SEARCH_DEP_CHECK).toContain('FastMCP');
  });
});

// ── overallStatus roll-up ───────────────────────────────────────────────────

describe('overallStatus', () => {
  it('success only when both succeed', async () => {
    const { overallStatus } = await import('../codeintel.js');
    expect(overallStatus('success', 'success')).toBe('success');
  });

  it('partial when exactly one succeeds (either order)', async () => {
    const { overallStatus } = await import('../codeintel.js');
    expect(overallStatus('success', 'skipped')).toBe('partial');
    expect(overallStatus('skipped', 'success')).toBe('partial');
  });

  it('skipped when neither succeeds', async () => {
    const { overallStatus } = await import('../codeintel.js');
    expect(overallStatus('skipped', 'skipped')).toBe('skipped');
  });

  it('is variadic — every sub-step counts (deus-memory included)', async () => {
    const { overallStatus } = await import('../codeintel.js');
    // All three succeed -> success.
    expect(overallStatus('success', 'success', 'success')).toBe('success');
    // A skipped deus-memory must drop a 2-success roll-up to partial, not hide
    // it as success (the bug GPT ai-eng caught).
    expect(overallStatus('success', 'success', 'skipped')).toBe('partial');
    expect(overallStatus('skipped', 'skipped', 'skipped')).toBe('skipped');
  });
});

// ── pinned package const (supply-chain) ─────────────────────────────────────

describe('CODEGRAPH_PACKAGE', () => {
  it('is version-pinned for supply-chain reproducibility', async () => {
    const { CODEGRAPH_PACKAGE } = await import('../codeintel.js');
    expect(CODEGRAPH_PACKAGE).toMatch(
      /^@colbymchenry\/codegraph@\d+\.\d+\.\d+$/,
    );
  });
});

// ── commandExists (shared platform helper the step gates on) ─────────────────

describe('commandExists integration', () => {
  it('returns true for a command that definitely exists', async () => {
    const { commandExists } = await import('../platform.js');
    expect(commandExists('node')).toBe(true);
  });

  it('returns false for a command that does not exist', async () => {
    const { commandExists } = await import('../platform.js');
    expect(commandExists('definitely_not_a_real_command_xyz_abc_123')).toBe(
      false,
    );
  });
});
