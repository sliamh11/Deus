/**
 * Unit + differential tests for the session-open vault-context surface
 * (LIA-416 / D2, src/agent-runtimes/vault-context.ts).
 *
 * Every unit test injects the full seam set (paths, config, clock, env,
 * spawn) so nothing here ever reads the real user vault, cache, config,
 * memory database, or session logs — all filesystem work happens inside a
 * per-test temp dir.
 *
 * The differential suite ("vault context parity (LIA-416 AC5)") is
 * POSIX-only (the reference `vault_context_hook.py` declares macOS/Linux):
 * it builds ONE real fixture, runs BOTH the TypeScript facade and the actual
 * Python hook against it with an identical environment, and asserts literal
 * output equality.
 */
import { spawnSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';

import Database from 'better-sqlite3';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';

import {
  loadVaultContext,
  normalizeVaultAutoload,
  MAX_SECTION_CHARS,
  SEMANTIC_TTL_SECONDS,
  type SpawnFn,
  type SpawnedChildLike,
  type VaultContextDeps,
} from './vault-context.js';
import type { RunContext } from './types.js';
import { IS_WINDOWS } from '../platform.js';

// Real repo scripts dir (this file lives at src/agent-runtimes/) —
// realpath'd to mirror Python's `Path(__file__).resolve()`.
const SCRIPTS_DIR = fs.realpathSync(
  fileURLToPath(new URL('../../scripts', import.meta.url)),
);

let tmpRoot = '';

beforeEach(() => {
  tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'd2-vault-context-'));
});

afterEach(() => {
  fs.rmSync(tmpRoot, { recursive: true, force: true });
});

function ctx(overrides: Partial<RunContext> = {}): RunContext {
  return {
    prompt: 'hello',
    groupFolder: 'main',
    chatJid: 'test@g.us',
    isControlGroup: true,
    ...overrides,
  };
}

/** Fake spawn factory — scripts one child's behavior per call. */
function fakeSpawn(script: {
  stdout?: string;
  emitError?: boolean;
  neverClose?: boolean;
  onKill?: () => void;
  throwOnSpawn?: boolean;
}): SpawnFn {
  return () => {
    if (script.throwOnSpawn) throw new Error('spawn EACCES');
    let dataCb: ((chunk: Buffer | string) => void) | undefined;
    let errorCb: ((...args: unknown[]) => void) | undefined;
    let closeCb: ((...args: unknown[]) => void) | undefined;
    const child: SpawnedChildLike = {
      stdout: {
        on: (event, cb) => {
          if (event === 'data') dataCb = cb;
        },
      },
      on: (event, cb) => {
        if (event === 'error') errorCb = cb;
        if (event === 'close') closeCb = cb;
      },
      kill: () => script.onKill?.(),
    };
    setTimeout(() => {
      if (script.emitError) {
        errorCb?.(new Error('spawn ENOENT'));
        return;
      }
      if (script.stdout !== undefined) dataCb?.(Buffer.from(script.stdout));
      if (!script.neverClose) closeCb?.(0);
    }, 0);
    return child;
  };
}

/** Healthy memory_tree.db fixture — root node, one extra node, one edge. */
function writeHealthyDb(dbPath: string): void {
  fs.mkdirSync(path.dirname(dbPath), { recursive: true });
  const db = new Database(dbPath);
  db.exec(
    'CREATE TABLE nodes (path TEXT, orphaned_at TEXT); ' +
      'CREATE TABLE edges (kind TEXT, expired_at TEXT);',
  );
  const insertNode = db.prepare(
    'INSERT INTO nodes (path, orphaned_at) VALUES (?, NULL)',
  );
  insertNode.run('MEMORY_TREE.md');
  insertNode.run('Atoms/example.md');
  db.prepare(
    "INSERT INTO edges (kind, expired_at) VALUES ('child', NULL)",
  ).run();
  db.close();
}

interface Fixture {
  deps: VaultContextDeps;
  vaultPath: string;
  homeDir: string;
  config: Record<string, unknown>;
}

/**
 * Standard eligible-control-group fixture: real temp vault + home dirs,
 * healthy DB, and inert defaults for every provider (no checkpoint, no
 * cache, empty subprocess output) — individual tests turn sections on.
 */
function makeFixture(
  configOverrides: Record<string, unknown> = {},
  depOverrides: Partial<VaultContextDeps> = {},
): Fixture {
  const vaultPath = path.join(tmpRoot, 'vault');
  const homeDir = path.join(tmpRoot, 'home');
  fs.mkdirSync(vaultPath, { recursive: true });
  fs.mkdirSync(path.join(homeDir, '.deus'), { recursive: true });
  const dbPath = path.join(homeDir, '.deus', 'memory_tree.db');
  writeHealthyDb(dbPath);
  const config: Record<string, unknown> = {
    vault_path: vaultPath,
    ...configOverrides,
  };
  const deps: VaultContextDeps = {
    readConfig: () => config,
    resolveVault: () => vaultPath,
    homeDir,
    dbPath,
    scriptsDir: SCRIPTS_DIR,
    pythonBin: 'python3',
    env: {},
    now: Date.now,
    spawnFn: fakeSpawn({ stdout: '' }),
    recentTimeoutMs: 1000,
    ...depOverrides,
  };
  return { deps, vaultPath, homeDir, config };
}

function writeVaultFile(
  vaultPath: string,
  name: string,
  content: string,
): void {
  const full = path.join(vaultPath, name);
  fs.mkdirSync(path.dirname(full), { recursive: true });
  fs.writeFileSync(full, content, 'utf-8');
}

function localDateString(ms: number): string {
  const d = new Date(ms);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

// ───────────────────────────────────────────────────────────────────────────
// Eligibility and skip semantics
// ───────────────────────────────────────────────────────────────────────────
describe('loadVaultContext eligibility', () => {
  it('skips non-control groups without touching config, files, or subprocesses', async () => {
    const result = await loadVaultContext(ctx({ isControlGroup: false }), {
      readConfig: () => {
        throw new Error('config must not be read on the skip path');
      },
      resolveVault: () => {
        throw new Error('vault must not be resolved on the skip path');
      },
      spawnFn: fakeSpawn({ throwOnSpawn: true }),
      env: {},
    });
    expect(result.content).toBeUndefined();
    expect(result.record).toEqual({
      eligible: false,
      skipReason: 'non-control-group',
      vaultAvailable: false,
      contextLoaded: false,
      loadedSections: [],
      loadedVaultFiles: [],
    });
  });

  it('skips when DEUS_VAULT_PRELOADED=1 (duplicate-prevention parity), all providers suppressed', async () => {
    const result = await loadVaultContext(ctx(), {
      readConfig: () => {
        throw new Error('config must not be read on the preloaded path');
      },
      env: { DEUS_VAULT_PRELOADED: '1' },
    });
    expect(result.content).toBeUndefined();
    expect(result.record.eligible).toBe(false);
    expect(result.record.skipReason).toBe('already-preloaded');
  });

  it('a control-group RunContext with worktreePath remains eligible (no .git worktree skip)', async () => {
    const { deps, vaultPath } = makeFixture();
    writeVaultFile(vaultPath, 'CLAUDE.md', 'identity content');
    const result = await loadVaultContext(
      ctx({ worktreePath: path.join(tmpRoot, 'some-worktree') }),
      deps,
    );
    expect(result.record.eligible).toBe(true);
    expect(result.content).toContain('=== VAULT: CLAUDE.md ===');
  });
});

// ───────────────────────────────────────────────────────────────────────────
// vault_autoload normalization
// ───────────────────────────────────────────────────────────────────────────
describe('normalizeVaultAutoload', () => {
  it('defaults to ["CLAUDE.md"] only when the key is absent', () => {
    expect(normalizeVaultAutoload({})).toEqual(['CLAUDE.md']);
  });

  it('preserves configured order and an explicit empty array', () => {
    expect(
      normalizeVaultAutoload({ vault_autoload: ['b.md', 'a.md'] }),
    ).toEqual(['b.md', 'a.md']);
    expect(normalizeVaultAutoload({ vault_autoload: [] })).toEqual([]);
  });

  it('drops malformed/non-string entries without throwing', () => {
    expect(
      normalizeVaultAutoload({ vault_autoload: ['ok.md', 42, null, {}] }),
    ).toEqual(['ok.md']);
    expect(normalizeVaultAutoload({ vault_autoload: 'not-a-list' })).toEqual(
      [],
    );
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Vault-file provider
// ───────────────────────────────────────────────────────────────────────────
describe('vault file sections', () => {
  it('loads the default CLAUDE.md when vault_autoload is absent', async () => {
    const { deps, vaultPath } = makeFixture();
    writeVaultFile(vaultPath, 'CLAUDE.md', 'vault identity');
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBe('=== VAULT: CLAUDE.md ===\nvault identity');
    expect(result.record.loadedVaultFiles).toEqual(['CLAUDE.md']);
    expect(result.record.loadedSections).toEqual(['vault-files']);
  });

  it('loads multiple configured files preserving order', async () => {
    const { deps, vaultPath } = makeFixture({
      vault_autoload: ['second.md', 'Persona/INDEX.md'],
    });
    writeVaultFile(vaultPath, 'second.md', 'S');
    writeVaultFile(vaultPath, 'Persona/INDEX.md', 'P');
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBe(
      '=== VAULT: second.md ===\nS\n\n=== VAULT: Persona/INDEX.md ===\nP',
    );
    expect(result.record.loadedVaultFiles).toEqual([
      'second.md',
      'Persona/INDEX.md',
    ]);
  });

  it('explicit empty vault_autoload loads no files but later providers still run', async () => {
    const nowMs = Date.now();
    const { deps, vaultPath, homeDir } = makeFixture(
      { vault_autoload: [] },
      { now: () => nowMs },
    );
    writeVaultFile(vaultPath, 'CLAUDE.md', 'must not load');
    const cpDir = path.join(vaultPath, 'Checkpoints');
    writeVaultFile(
      vaultPath,
      path.join('Checkpoints', `${localDateString(nowMs)}-x.md`),
      'checkpoint body',
    );
    expect(fs.existsSync(cpDir)).toBe(true);
    fs.writeFileSync(
      path.join(homeDir, '.deus', 'resume_semantic_cache.txt'),
      'related things',
      'utf-8',
    );
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).not.toContain('must not load');
    expect(result.record.loadedVaultFiles).toEqual([]);
    expect(result.record.loadedSections).toEqual([
      'checkpoint',
      'semantic-cache',
    ]);
    expect(result.content).toBe(
      '=== MID-SESSION CHECKPOINT ===\ncheckpoint body\n\n=== RELATED SESSIONS ===\nrelated things',
    );
  });

  it('skips missing, empty, and unreadable files fail-soft', async () => {
    const { deps, vaultPath } = makeFixture({
      vault_autoload: ['missing.md', 'empty.md', 'unreadable.md', 'good.md'],
    });
    writeVaultFile(vaultPath, 'empty.md', '   \n  ');
    // A directory where a file is expected: readFileSync throws (EISDIR) —
    // the cross-platform "unreadable" case.
    fs.mkdirSync(path.join(vaultPath, 'unreadable.md'));
    writeVaultFile(vaultPath, 'good.md', 'good content');
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBe('=== VAULT: good.md ===\ngood content');
    expect(result.record.loadedVaultFiles).toEqual(['good.md']);
  });

  it('truncates each file section to MAX_SECTION_CHARS', async () => {
    const { deps, vaultPath } = makeFixture();
    writeVaultFile(vaultPath, 'CLAUDE.md', 'x'.repeat(MAX_SECTION_CHARS + 500));
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBe(
      `=== VAULT: CLAUDE.md ===\n${'x'.repeat(MAX_SECTION_CHARS)}`,
    );
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Checkpoint provider
// ───────────────────────────────────────────────────────────────────────────
describe('checkpoint section', () => {
  it('selects the newest same-day checkpoint by mtime', async () => {
    const nowMs = Date.now();
    const { deps, vaultPath } = makeFixture(
      { vault_autoload: [] },
      { now: () => nowMs },
    );
    const today = localDateString(nowMs);
    writeVaultFile(vaultPath, `Checkpoints/${today}-early.md`, 'early');
    writeVaultFile(vaultPath, `Checkpoints/${today}-late.md`, 'late');
    const early = path.join(vaultPath, 'Checkpoints', `${today}-early.md`);
    const late = path.join(vaultPath, 'Checkpoints', `${today}-late.md`);
    fs.utimesSync(
      early,
      new Date(nowMs - 7200_000),
      new Date(nowMs - 7200_000),
    );
    fs.utimesSync(late, new Date(nowMs - 600_000), new Date(nowMs - 600_000));
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBe('=== MID-SESSION CHECKPOINT ===\nlate');
    expect(result.record.loadedSections).toEqual(['checkpoint']);
  });

  it('contributes nothing when only other-day checkpoints exist', async () => {
    const nowMs = Date.now();
    const { deps, vaultPath } = makeFixture(
      { vault_autoload: [] },
      { now: () => nowMs },
    );
    const yesterday = localDateString(nowMs - 24 * 3600_000);
    writeVaultFile(vaultPath, `Checkpoints/${yesterday}-old.md`, 'stale');
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBeUndefined();
    expect(result.record.contextLoaded).toBe(false);
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Semantic-cache provider
// ───────────────────────────────────────────────────────────────────────────
describe('semantic cache section', () => {
  function writeCache(
    homeDir: string,
    content: string,
    ageSeconds: number,
    nowMs: number,
  ): void {
    const cachePath = path.join(homeDir, '.deus', 'resume_semantic_cache.txt');
    fs.writeFileSync(cachePath, content, 'utf-8');
    const mtime = new Date(nowMs - ageSeconds * 1000);
    fs.utimesSync(cachePath, mtime, mtime);
  }

  it('includes a fresh cache just inside the 4-hour boundary', async () => {
    const nowMs = Date.now();
    const { deps, homeDir } = makeFixture(
      { vault_autoload: [] },
      { now: () => nowMs },
    );
    writeCache(
      homeDir,
      '  related sessions text  ',
      SEMANTIC_TTL_SECONDS - 60,
      nowMs,
    );
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBe(
      '=== RELATED SESSIONS ===\nrelated sessions text',
    );
    expect(result.record.loadedSections).toEqual(['semantic-cache']);
  });

  it('excludes the cache exactly at the boundary (age >= TTL)', async () => {
    const nowMs = Date.now();
    const { deps, homeDir } = makeFixture(
      { vault_autoload: [] },
      { now: () => nowMs },
    );
    writeCache(homeDir, 'stale', SEMANTIC_TTL_SECONDS, nowMs);
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBeUndefined();
  });

  it('excludes an empty cache file', async () => {
    const nowMs = Date.now();
    const { deps, homeDir } = makeFixture(
      { vault_autoload: [] },
      { now: () => nowMs },
    );
    writeCache(homeDir, '   \n ', 10, nowMs);
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBeUndefined();
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Recent-sessions subprocess adapter
// ───────────────────────────────────────────────────────────────────────────
describe('recent sessions section', () => {
  it('includes trimmed stdout under the RECENT SESSIONS header', async () => {
    const { deps } = makeFixture(
      { vault_autoload: [] },
      { spawnFn: fakeSpawn({ stdout: '## Recent Sessions\n- one\n' }) },
    );
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBe(
      '=== RECENT SESSIONS ===\n## Recent Sessions\n- one',
    );
    expect(result.record.loadedSections).toEqual(['recent-sessions']);
  });

  it('skips on empty stdout', async () => {
    const { deps } = makeFixture(
      { vault_autoload: [] },
      { spawnFn: fakeSpawn({ stdout: '  \n ' }) },
    );
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBeUndefined();
  });

  it('skips on spawn error event (missing Python) without failing other sections', async () => {
    const { deps, vaultPath } = makeFixture(
      {},
      { spawnFn: fakeSpawn({ emitError: true }) },
    );
    writeVaultFile(vaultPath, 'CLAUDE.md', 'still loads');
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBe('=== VAULT: CLAUDE.md ===\nstill loads');
  });

  it('skips when spawn itself throws synchronously', async () => {
    const { deps } = makeFixture(
      { vault_autoload: [] },
      { spawnFn: fakeSpawn({ throwOnSpawn: true }) },
    );
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBeUndefined();
  });

  it('kills and skips on timeout (5s reference behavior, shortened seam)', async () => {
    let killed = false;
    const { deps } = makeFixture(
      { vault_autoload: [] },
      {
        spawnFn: fakeSpawn({
          stdout: 'partial output before hang',
          neverClose: true,
          onKill: () => {
            killed = true;
          },
        }),
        recentTimeoutMs: 25,
      },
    );
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBeUndefined();
    expect(killed).toBe(true);
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Degraded memory health
// ───────────────────────────────────────────────────────────────────────────
describe('degraded memory health section', () => {
  it('a healthy memory system stays silent', async () => {
    const { deps, vaultPath } = makeFixture();
    writeVaultFile(vaultPath, 'CLAUDE.md', 'identity');
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).not.toContain('MEMORY SYSTEM DEGRADED');
    expect(result.record.loadedSections).toEqual(['vault-files']);
  });

  it('unconfigured vault returns the degraded banner ALONE (vault-missing exception)', async () => {
    const { deps } = makeFixture({}, { resolveVault: () => null });
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toContain('=== MEMORY SYSTEM DEGRADED ===');
    expect(result.content).toContain('  - vault path is not configured');
    expect(result.content).toContain(
      'vault unavailable: remount / grant Full Disk Access, then restart the session',
    );
    expect(result.record).toMatchObject({
      eligible: true,
      vaultAvailable: false,
      contextLoaded: true,
      loadedSections: ['degraded-memory-health'],
      loadedVaultFiles: [],
    });
  });

  it('non-directory vault reports "vault not mounted at <path>"', async () => {
    const missing = path.join(tmpRoot, 'nonexistent-vault');
    const { deps } = makeFixture({}, { resolveVault: () => missing });
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toContain(`  - vault not mounted at ${missing}`);
    expect(result.record.vaultAvailable).toBe(false);
  });

  it.skipIf(IS_WINDOWS)(
    'unwritable vault degrades but the pipeline still loads readable sections',
    async () => {
      const { deps, vaultPath } = makeFixture();
      writeVaultFile(vaultPath, 'CLAUDE.md', 'still readable');
      fs.chmodSync(vaultPath, 0o555);
      try {
        const result = await loadVaultContext(ctx(), deps);
        expect(result.content).toContain(
          `  - vault not writable at ${vaultPath} (grant Full Disk Access?)`,
        );
        expect(result.content).toContain(
          '=== VAULT: CLAUDE.md ===\nstill readable',
        );
        expect(result.record.loadedSections).toEqual([
          'degraded-memory-health',
          'vault-files',
        ]);
      } finally {
        fs.chmodSync(vaultPath, 0o755);
      }
    },
  );

  it('missing tree DB degrades with the rebuild remedy', async () => {
    const { deps, homeDir } = makeFixture();
    const dbPath = path.join(homeDir, '.deus', 'memory_tree.db');
    fs.rmSync(dbPath);
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toContain(`  - memory tree DB missing at ${dbPath}`);
    expect(result.content).toContain(
      `rebuild tree: python3 ${path.join(SCRIPTS_DIR, 'memory_tree.py')} build`,
    );
    expect(result.content).toContain(
      `verify: python3 ${path.join(SCRIPTS_DIR, 'memory_tree.py')} check`,
    );
  });

  it('unopenable tree DB reports unreadable', async () => {
    const { deps, homeDir } = makeFixture();
    const dbPath = path.join(homeDir, '.deus', 'memory_tree.db');
    fs.rmSync(dbPath);
    fs.mkdirSync(dbPath); // a directory cannot be opened as a database
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toContain(
      `  - memory tree DB unreadable at ${dbPath}`,
    );
  });

  it('empty tree reports only the empty-tree line', async () => {
    const { deps, homeDir } = makeFixture();
    const dbPath = path.join(homeDir, '.deus', 'memory_tree.db');
    const db = new Database(dbPath);
    db.exec('DELETE FROM nodes; DELETE FROM edges;');
    db.close();
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toContain(
      '  - memory tree is empty (0 active nodes)',
    );
    expect(result.content).not.toContain('navigation root');
    expect(result.content).not.toContain('0 edges');
  });

  it('missing root and zero edges degrade with their own lines and remedies', async () => {
    const { deps, homeDir } = makeFixture();
    const dbPath = path.join(homeDir, '.deus', 'memory_tree.db');
    const db = new Database(dbPath);
    db.exec(
      "DELETE FROM nodes WHERE path = 'MEMORY_TREE.md'; DELETE FROM edges;",
    );
    db.close();
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toContain(
      '  - navigation root MEMORY_TREE.md is missing from the tree',
    );
    expect(result.content).toContain(
      '  - memory graph has 0 edges across 1 nodes (ranking dead)',
    );
    expect(result.content).toContain(
      `lost root: python3 ${path.join(SCRIPTS_DIR, 'memory_tree.py')} scaffold-root --force --reindex`,
    );
  });

  it('a schema/query error means "cannot determine" — no banner, session unaffected', async () => {
    const { deps, homeDir, vaultPath } = makeFixture();
    const dbPath = path.join(homeDir, '.deus', 'memory_tree.db');
    const db = new Database(dbPath);
    db.exec('DROP TABLE nodes;');
    db.close();
    writeVaultFile(vaultPath, 'CLAUDE.md', 'unaffected');
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBe('=== VAULT: CLAUDE.md ===\nunaffected');
  });

  it('an unexpected health-probe exception is swallowed and other sections still load', async () => {
    const { deps, vaultPath } = makeFixture(
      {},
      // dbPath alone missing is a DOCUMENTED degraded condition (produces a
      // real banner, not an exception — Node's fs.existsSync(undefined)
      // returns false rather than throwing on this runtime, verified). To
      // exercise the outer catch-all's truly-UNEXPECTED-exception path, also
      // break scriptsDir: once a real degraded line exists (from the missing
      // DB), rendering calls `path.join(deps.scriptsDir, 'memory_tree.py')`,
      // which throws a genuine TypeError for a non-string scriptsDir — the
      // catch-all must swallow that and contribute no degraded section,
      // matching the hook's try/except, while the vault file section still
      // loads independently.
      {
        dbPath: undefined as unknown as string,
        scriptsDir: undefined as unknown as string,
      },
    );
    writeVaultFile(vaultPath, 'CLAUDE.md', 'resilient');
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBe('=== VAULT: CLAUDE.md ===\nresilient');
  });
});

// ───────────────────────────────────────────────────────────────────────────
// Composition order + metadata
// ───────────────────────────────────────────────────────────────────────────
describe('composition and metadata', () => {
  it('composes all sections in canonical order joined with \\n\\n and records exact metadata', async () => {
    const nowMs = Date.now();
    const { deps, vaultPath, homeDir } = makeFixture(
      { vault_autoload: ['CLAUDE.md'] },
      {
        now: () => nowMs,
        spawnFn: fakeSpawn({ stdout: 'recent output' }),
      },
    );
    // Degrade health without breaking the vault: remove the tree DB.
    fs.rmSync(path.join(homeDir, '.deus', 'memory_tree.db'));
    writeVaultFile(vaultPath, 'CLAUDE.md', 'V');
    writeVaultFile(
      vaultPath,
      `Checkpoints/${localDateString(nowMs)}-cp.md`,
      'C',
    );
    fs.writeFileSync(
      path.join(homeDir, '.deus', 'resume_semantic_cache.txt'),
      'R',
      'utf-8',
    );

    const result = await loadVaultContext(ctx(), deps);
    const content = result.content!;
    const order = [
      '=== MEMORY SYSTEM DEGRADED ===',
      '=== VAULT: CLAUDE.md ===',
      '=== MID-SESSION CHECKPOINT ===',
      '=== RECENT SESSIONS ===',
      '=== RELATED SESSIONS ===',
    ].map((header) => content.indexOf(header));
    expect(order.every((idx) => idx >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
    expect(content).toContain(
      '=== VAULT: CLAUDE.md ===\nV\n\n=== MID-SESSION CHECKPOINT ===\nC\n\n=== RECENT SESSIONS ===\nrecent output\n\n=== RELATED SESSIONS ===\nR',
    );
    expect(result.record).toEqual({
      eligible: true,
      vaultAvailable: true,
      contextLoaded: true,
      loadedSections: [
        'degraded-memory-health',
        'vault-files',
        'checkpoint',
        'recent-sessions',
        'semantic-cache',
      ],
      loadedVaultFiles: ['CLAUDE.md'],
    });
  });

  it('returns undefined content with a truthful record when nothing contributes', async () => {
    const { deps } = makeFixture({ vault_autoload: [] });
    const result = await loadVaultContext(ctx(), deps);
    expect(result.content).toBeUndefined();
    expect(result.record).toEqual({
      eligible: true,
      vaultAvailable: true,
      contextLoaded: false,
      loadedSections: [],
      loadedVaultFiles: [],
    });
  });

  it('environment vault path takes precedence over config through the real resolver', async () => {
    const { resolveVaultPath } = await import('../container-mounter.js');
    const envVault = path.join(tmpRoot, 'env-vault');
    fs.mkdirSync(envVault, { recursive: true });
    fs.writeFileSync(
      path.join(envVault, 'CLAUDE.md'),
      'from env vault',
      'utf-8',
    );
    const { deps } = makeFixture(
      { vault_path: path.join(tmpRoot, 'config-vault') },
      {},
    );
    const original = process.env.DEUS_VAULT_PATH;
    process.env.DEUS_VAULT_PATH = envVault;
    try {
      const result = await loadVaultContext(ctx(), {
        ...deps,
        resolveVault: (config) => resolveVaultPath(config),
      });
      expect(result.content).toContain('from env vault');
    } finally {
      if (original !== undefined) process.env.DEUS_VAULT_PATH = original;
      else delete process.env.DEUS_VAULT_PATH;
    }
  });
});

// ───────────────────────────────────────────────────────────────────────────
// AC5 — differential parity against the real scripts/vault_context_hook.py
// ───────────────────────────────────────────────────────────────────────────
const python3Available =
  !IS_WINDOWS && spawnSync('python3', ['--version']).status === 0;

describe.skipIf(!python3Available)('vault context parity (LIA-416 AC5)', () => {
  const HOOK_PATH = path.join(SCRIPTS_DIR, 'vault_context_hook.py');

  function runPythonHook(
    env: Record<string, string | undefined>,
    cwd: string,
  ): string | undefined {
    const res = spawnSync('python3', [HOOK_PATH], {
      env: env as NodeJS.ProcessEnv,
      cwd,
      input: '{}',
      encoding: 'utf-8',
      timeout: 30_000,
    });
    expect(res.error).toBeUndefined();
    const stdout = (res.stdout ?? '').trim();
    if (!stdout) return undefined;
    const parsed = JSON.parse(stdout) as {
      hookSpecificOutput: { additionalContext: string };
    };
    return parsed.hookSpecificOutput.additionalContext;
  }

  /** Env shared by the TS subprocess adapter and the Python hook run. */
  function buildChildEnv(homeDir: string, vaultPath: string) {
    const env: Record<string, string | undefined> = {
      ...process.env,
      HOME: homeDir,
      DEUS_VAULT_PATH: vaultPath,
    };
    delete env.DEUS_VAULT_PRELOADED;
    delete env.DEUS_PYTHON;
    return env;
  }

  it('full healthy fixture: TS content literally equals the hook additionalContext', async () => {
    const homeDir = path.join(tmpRoot, 'home');
    const vaultPath = path.join(tmpRoot, 'vault');
    const cwd = path.join(tmpRoot, 'cwd'); // non-worktree CWD (no .git file)
    fs.mkdirSync(path.join(homeDir, '.deus'), { recursive: true });
    fs.mkdirSync(path.join(homeDir, '.config', 'deus'), { recursive: true });
    fs.mkdirSync(cwd, { recursive: true });

    const config = {
      vault_path: vaultPath,
      vault_autoload: ['CLAUDE.md', 'Persona/INDEX.md'],
    };
    fs.writeFileSync(
      path.join(homeDir, '.config', 'deus', 'config.json'),
      JSON.stringify(config),
      'utf-8',
    );

    writeVaultFile(
      vaultPath,
      'CLAUDE.md',
      '# Vault Identity\n\nParity test identity content.\n',
    );
    writeVaultFile(
      vaultPath,
      'Persona/INDEX.md',
      'Persona index body.\n' + 'y'.repeat(MAX_SECTION_CHARS),
    );

    const nowMs = Date.now();
    const today = localDateString(nowMs);
    writeVaultFile(
      vaultPath,
      `Checkpoints/${today}-early.md`,
      'EARLY checkpoint\n',
    );
    writeVaultFile(
      vaultPath,
      `Checkpoints/${today}-late.md`,
      'LATE checkpoint\n',
    );
    fs.utimesSync(
      path.join(vaultPath, 'Checkpoints', `${today}-early.md`),
      new Date(nowMs - 7200_000),
      new Date(nowMs - 7200_000),
    );
    fs.utimesSync(
      path.join(vaultPath, 'Checkpoints', `${today}-late.md`),
      new Date(nowMs - 300_000),
      new Date(nowMs - 300_000),
    );

    // Deterministic session log for memory_indexer.py --recent 3. Both
    // implementations invoke the SAME script with the SAME env, so whatever
    // it renders (including nothing, if optional indexer deps are absent on
    // this machine) must match literally on both sides.
    writeVaultFile(
      vaultPath,
      'Session-Logs/2026-07-10/parity-session.md',
      '---\ndate: 2026-07-10\ntldr: Parity fixture session.\ntopics: parity\ndecisions: Compare literally\n---\n\nBody.\n',
    );

    writeHealthyDb(path.join(homeDir, '.deus', 'memory_tree.db'));

    fs.writeFileSync(
      path.join(homeDir, '.deus', 'resume_semantic_cache.txt'),
      'Related session A\nRelated session B\n',
      'utf-8',
    );

    const childEnv = buildChildEnv(homeDir, vaultPath);

    const tsResult = await loadVaultContext(ctx(), {
      readConfig: () => config,
      resolveVault: () => vaultPath,
      homeDir,
      dbPath: path.join(homeDir, '.deus', 'memory_tree.db'),
      scriptsDir: SCRIPTS_DIR,
      pythonBin: 'python3',
      env: childEnv,
      now: Date.now,
      recentTimeoutMs: 15_000,
    });
    const pyContext = runPythonHook(childEnv, cwd);

    expect(tsResult.content).toBeDefined();
    expect(tsResult.content).toBe(pyContext);
    // Sanity: the fixture actually exercised the vault/checkpoint/cache path.
    expect(tsResult.content).toContain('=== VAULT: CLAUDE.md ===');
    expect(tsResult.content).toContain(
      '=== MID-SESSION CHECKPOINT ===\nLATE checkpoint',
    );
    expect(tsResult.content).toContain('=== RELATED SESSIONS ===');
  }, 60_000);

  it('degraded fixture (unavailable vault + missing tree DB): literal banner parity', async () => {
    const homeDir = path.join(tmpRoot, 'home');
    const missingVault = path.join(tmpRoot, 'not-a-vault');
    const cwd = path.join(tmpRoot, 'cwd');
    fs.mkdirSync(path.join(homeDir, '.deus'), { recursive: true });
    fs.mkdirSync(path.join(homeDir, '.config', 'deus'), { recursive: true });
    fs.mkdirSync(cwd, { recursive: true });
    const config = { vault_path: missingVault };
    fs.writeFileSync(
      path.join(homeDir, '.config', 'deus', 'config.json'),
      JSON.stringify(config),
      'utf-8',
    );

    const childEnv = buildChildEnv(homeDir, missingVault);

    const tsResult = await loadVaultContext(ctx(), {
      readConfig: () => config,
      resolveVault: () => missingVault,
      homeDir,
      dbPath: path.join(homeDir, '.deus', 'memory_tree.db'),
      scriptsDir: SCRIPTS_DIR,
      pythonBin: 'python3',
      env: childEnv,
      now: Date.now,
      recentTimeoutMs: 15_000,
    });
    const pyContext = runPythonHook(childEnv, cwd);

    // Banner despite the absent vault — the degraded-even-when-vault-missing
    // exception, on both sides, byte-identical.
    expect(tsResult.content).toBeDefined();
    expect(tsResult.content).toBe(pyContext);
    expect(tsResult.content).toContain('=== MEMORY SYSTEM DEGRADED ===');
    expect(tsResult.content).toContain(
      `  - vault not mounted at ${missingVault}`,
    );
    expect(tsResult.record.vaultAvailable).toBe(false);
    expect(tsResult.record.loadedSections).toEqual(['degraded-memory-health']);
  }, 60_000);
});
