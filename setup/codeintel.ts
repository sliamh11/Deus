/**
 * Step: codeintel — install + register the two code-intelligence MCP servers
 * Deus relies on: **codegraph** (third-party npm global) and **code-search**
 * (first-party `scripts/code_search_mcp.py`).
 *
 * Non-fatal contract: setup must NEVER fail because optional tooling is absent,
 * so every external call is individually wrapped — a failure becomes a reported
 * skip, not a thrown error. The two sub-steps are independent (codegraph's npm
 * path never blocks code-search's python path).
 *
 * Registration delegates to `claude mcp add/remove --scope user` — we never
 * hand-edit `~/.claude.json` (no clobber risk). remove-then-add is idempotent
 * (safe re-runs) and scope-correct (immune to a stale project `.mcp.json`; a
 * re-run also refreshes a stale user-scope entry like the old `eval/.venv` one).
 *
 * Mirrors `setup/ollama.ts`.
 */
import { execFileSync, spawn } from 'child_process';
import fs from 'fs';
import path from 'path';

import { resolvePython } from '../src/checks.js';
import { CONFIG_DIR } from '../src/config.js';
import { logger } from '../src/logger.js';
// TODO(windows-sot-phase1): update import to '../src/platform.js' once
// setup/platform.ts is consolidated into src/platform.ts
// (see project_windows_sot_plan.md Phase 1).
import { commandExists, getPlatform } from './platform.js';
import { emitStatus } from './status.js';

/**
 * codegraph npm package, version-pinned for supply-chain reproducibility.
 * pinned 2026-06-03 (latest stable via `npm view @colbymchenry/codegraph`);
 * bump after testing.
 */
export const CODEGRAPH_PACKAGE = '@colbymchenry/codegraph@0.9.9';

/**
 * Python imports the code-search MCP **server** needs to actually start —
 * mirrors `scripts/code_search_mcp.py:40`. `mcp` is the hard requirement (the
 * server can't be an MCP endpoint without it); `sqlite_vec` backs vector search.
 * We probe both before registering so we never register a server that can't run
 * (a `import sqlite_vec`-only check let a `python3` without `mcp` slip through).
 */
export const CODE_SEARCH_DEP_CHECK =
  'import sqlite_vec; from mcp.server.fastmcp import FastMCP';

/** Background build logs (mirrors ollama's ~/.config/deus/ollama-downloads/). */
const LOG_DIR = path.join(CONFIG_DIR, 'codeintel');

type SubStatus = 'success' | 'skipped';
type McpStatus = 'registered' | 'failed' | 'skipped';
type IndexStatus = 'started' | 'failed' | 'skipped';

interface SubResult {
  status: SubStatus;
  reason?: string;
  mcp?: McpStatus;
  indexBuild?: IndexStatus;
}

// ── Pure helpers (unit-tested) ─────────────────────────────────────────────

/** Build argv for `claude mcp add --scope <scope> <name> -- <serverCommand...>`. */
export function buildMcpAddArgs(
  name: string,
  scope: string,
  serverCommand: string[],
): string[] {
  return ['mcp', 'add', '--scope', scope, name, '--', ...serverCommand];
}

/** stdio server command for code-search: `<python> <repo>/scripts/code_search_mcp.py`. */
export function codeSearchServerCommand(
  python: string,
  repoRoot: string,
): string[] {
  return [python, path.join(repoRoot, 'scripts', 'code_search_mcp.py')];
}

/** Roll up the two sub-step statuses into one step-level status. */
export function overallStatus(
  a: SubStatus,
  b: SubStatus,
): 'success' | 'partial' | 'skipped' {
  if (a === 'success' && b === 'success') return 'success';
  if (a === 'success' || b === 'success') return 'partial';
  return 'skipped';
}

// ── Side-effecting helpers (covered by live-run verification) ───────────────

/**
 * Register an MCP server at user scope, idempotently (remove-then-add).
 * Non-fatal: returns 'failed' rather than throwing.
 */
function registerMcpServer(name: string, serverCommand: string[]): McpStatus {
  // Remove first so a re-run refreshes a stale entry; ignore failure (the
  // entry may not exist, which is the common first-run case).
  try {
    execFileSync('claude', ['mcp', 'remove', '--scope', 'user', name], {
      stdio: 'ignore',
      timeout: 15000,
    });
  } catch {
    // not registered yet — expected on first run
  }
  try {
    execFileSync('claude', buildMcpAddArgs(name, 'user', serverCommand), {
      stdio: 'ignore',
      timeout: 15000,
    });
    return 'registered';
  } catch (err) {
    logger.warn({ name, err: (err as Error).message }, 'claude mcp add failed');
    return 'failed';
  }
}

/** Spawn a detached background build, logging to LOG_DIR/<stem>.log. */
function startBackgroundBuild(
  stem: string,
  command: string,
  args: string[],
  cwd: string,
): IndexStatus {
  try {
    if (!fs.existsSync(LOG_DIR)) fs.mkdirSync(LOG_DIR, { recursive: true });
    const out = fs.openSync(path.join(LOG_DIR, `${stem}.log`), 'a');
    const child = spawn(command, args, {
      cwd,
      detached: true,
      stdio: ['ignore', out, out],
    });
    child.unref();
    if (!child.pid) {
      logger.warn({ stem }, 'background build spawned without a pid');
      return 'failed';
    }
    return 'started';
  } catch (err) {
    logger.warn(
      { stem, err: (err as Error).message },
      'background build failed to start',
    );
    return 'failed';
  }
}

// ── Sub-step A: codegraph (attempted all platforms, gated on a runnable binary) ──

function setupCodegraph(repoRoot: string): SubResult {
  if (!commandExists('npm')) {
    return { status: 'skipped', reason: 'npm_not_installed' };
  }

  if (!commandExists('codegraph')) {
    try {
      // Array form (not a shell string) — no shell-interpolation surface.
      execFileSync('npm', ['install', '-g', CODEGRAPH_PACKAGE], {
        stdio: 'pipe',
        timeout: 300000,
      });
    } catch (err) {
      logger.warn(
        { err: (err as Error).message },
        'codegraph npm install failed',
      );
      return { status: 'skipped', reason: 'install_failed' };
    }
  }

  // Verify the binary actually runs — covers Windows-unsupported AND broken
  // installs uniformly, so we never report success while leaving a dead MCP
  // entry behind.
  try {
    execFileSync('codegraph', ['--version'], {
      stdio: 'ignore',
      timeout: 10000,
    });
  } catch {
    return { status: 'skipped', reason: 'binary_unverified' };
  }

  // Initialize the project index (cheap, idempotent) then background a full
  // index — large repos index slowly.
  let indexBuild: IndexStatus = 'skipped';
  try {
    if (!fs.existsSync(path.join(repoRoot, '.codegraph'))) {
      execFileSync('codegraph', ['init', repoRoot], {
        stdio: 'ignore',
        timeout: 30000,
      });
    }
    indexBuild = startBackgroundBuild(
      'codegraph-index',
      'codegraph',
      ['index', repoRoot],
      repoRoot,
    );
  } catch (err) {
    logger.warn({ err: (err as Error).message }, 'codegraph init failed');
    indexBuild = 'failed';
  }

  const mcp: McpStatus = commandExists('claude')
    ? registerMcpServer('codegraph', ['codegraph', 'serve', '--mcp'])
    : 'skipped';

  return { status: 'success', mcp, indexBuild };
}

// ── Sub-step B: code-search (macOS/Linux only, first-party) ──────────────────

function setupCodeSearch(repoRoot: string): SubResult {
  if (getPlatform() === 'windows') {
    // code_search_mcp.py hard-exits on win32 (needs sqlite_vec + Ollama).
    return { status: 'skipped', reason: 'windows_unsupported' };
  }

  const python = resolvePython();
  if (!python) {
    return { status: 'skipped', reason: 'python_not_found' };
  }

  // Probe the server's real imports BEFORE any registration. This stays ahead
  // of registerMcpServer (remove-then-add) on purpose: a deps_missing skip must
  // be a no-op on an existing registration, never clobbering a working entry.
  // NOTE(LIA-174): resolvePython() is not guaranteed to point at an MCP-capable
  // interpreter — the `memory` step installs sqlite_vec but not `mcp`, so on a
  // fresh machine this probe skips (deps_missing) rather than registering a
  // broken server. Reliably provisioning `mcp` (or targeting a venv that has it)
  // is tracked separately.
  try {
    execFileSync(python, ['-c', CODE_SEARCH_DEP_CHECK], {
      stdio: 'ignore',
      timeout: 10000,
    });
  } catch {
    return { status: 'skipped', reason: 'deps_missing' };
  }

  const mcp: McpStatus = commandExists('claude')
    ? registerMcpServer(
        'code-search',
        codeSearchServerCommand(python, repoRoot),
      )
    : 'skipped';

  // Index build needs Ollama embeddings; background it when Ollama is present.
  const indexBuild: IndexStatus = commandExists('ollama')
    ? startBackgroundBuild(
        'code-search-reindex',
        python,
        [path.join(repoRoot, 'scripts', 'code_search.py'), 'reindex', '.'],
        repoRoot,
      )
    : 'skipped';

  return { status: 'success', mcp, indexBuild };
}

// ── Step entry point ────────────────────────────────────────────────────────

export async function run(_args: string[]): Promise<void> {
  const repoRoot = process.cwd();

  const codegraph = setupCodegraph(repoRoot);
  const codeSearch = setupCodeSearch(repoRoot);

  // Human-facing notes: manual fallbacks + background-build pointers.
  const notes: string[] = [];
  if (codegraph.reason === 'install_failed') {
    notes.push(
      `codegraph install failed — run manually: npm install -g ${CODEGRAPH_PACKAGE}`,
    );
  }
  if (codeSearch.reason === 'deps_missing') {
    notes.push(
      'code-search needs the `mcp` and `sqlite_vec` Python packages — install them (`pip install mcp sqlite_vec`), then re-run `--step codeintel`.',
    );
  }
  if (codeSearch.status === 'success' && codeSearch.indexBuild === 'skipped') {
    notes.push(
      'code-search index not built (Ollama absent). After installing Ollama: python3 scripts/code_search.py reindex .',
    );
  }
  if (
    codegraph.indexBuild === 'started' ||
    codeSearch.indexBuild === 'started'
  ) {
    notes.push(
      `Index build(s) running in the background — logs in ${LOG_DIR}/`,
    );
  }
  for (const n of notes) console.log(`  - ${n}`);

  // The step itself always succeeds — optional tooling never fails setup.
  emitStatus('CODEINTEL', {
    STATUS: overallStatus(codegraph.status, codeSearch.status),
    CODEGRAPH: codegraph.status,
    CODEGRAPH_REASON: codegraph.reason ?? 'none',
    CODEGRAPH_MCP: codegraph.mcp ?? 'skipped',
    CODEGRAPH_INDEX: codegraph.indexBuild ?? 'skipped',
    CODE_SEARCH: codeSearch.status,
    CODE_SEARCH_REASON: codeSearch.reason ?? 'none',
    CODE_SEARCH_MCP: codeSearch.mcp ?? 'skipped',
    CODE_SEARCH_INDEX: codeSearch.indexBuild ?? 'skipped',
    LOG_DIR,
  });
}
