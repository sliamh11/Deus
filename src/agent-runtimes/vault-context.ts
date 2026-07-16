/**
 * Session-open vault context for the `deus-native` runtime (LIA-416 / D2).
 *
 * TypeScript surface mirroring `scripts/vault_context_hook.py` — the
 * SessionStart hook that injects the personal vault (Second Brain) identity
 * into host Claude Code sessions. `deus-native` never runs that hook (it is a
 * Claude Code harness hook), so this module reproduces the same ordered
 * context sections for the runtime's own session-open path:
 *
 *   1. `=== MEMORY SYSTEM DEGRADED ===`   (memory_health.py parity, TS port)
 *   2. `=== VAULT: <filename> ===`        (one per configured autoload file)
 *   3. `=== MID-SESSION CHECKPOINT ===`   (newest same-day checkpoint)
 *   4. `=== RECENT SESSIONS ===`          (memory_indexer.py --recent 3,
 *                                          spawned async — NOT ported)
 *   5. `=== RELATED SESSIONS ===`         (fresh semantic resume cache)
 *
 * Design: one Facade (`loadVaultContext`) over an ordered pipeline of
 * independent, fail-soft providers — a missing/empty/stale/unreadable/failed
 * source contributes nothing and never prevents later providers from running.
 * Contributed sections join with exactly `\n\n` (the reference hook's join).
 *
 * Eligibility (checked BEFORE any config/filesystem/DB/subprocess work):
 * - Control group only (`runContext.isControlGroup === true`). The reference
 *   hook targets the host developer's personal sessions, and the control
 *   group is the only group whose container mount receives the complete
 *   canonical vault (container-mounter.ts). Non-control groups get a
 *   deliberately narrower surface — automatic injection there would broaden
 *   disclosure of personal identity/checkpoints/session history beyond the
 *   reference hook's scope.
 * - `DEUS_VAULT_PRELOADED=1` skips everything (duplicate-prevention parity
 *   with the hook's early return — including the degraded banner).
 * - The hook's `.git`-is-a-file worktree skip is deliberately NOT copied: it
 *   describes the host Claude Code process's CWD, whereas here a
 *   `RunContext.worktreePath` is a task workspace attachment, not evidence
 *   the session prompt already carries vault context.
 *
 * Once-per-session and prompt delivery are NOT this module's concern:
 * `runTurn`'s checkpointer-existence gate decides when the session-open
 * loader (and therefore this facade) runs, and `buildPromptLifecycleHook`
 * delivers the composed string (lifecycle-events.ts).
 */

import { spawn } from 'child_process';
import fs from 'fs';
import path from 'path';

import Database from 'better-sqlite3';

import type { RunContext } from './types.js';
import { resolveVaultPath } from '../container-mounter.js';
import { readDeusConfig } from '../checks.js';
import { HOME_DIR, PROJECT_ROOT } from '../config.js';
import { PYTHON_BIN } from '../platform.js';

/** Per-section truncation cap — MAX_SECTION_CHARS in the reference hook. */
export const MAX_SECTION_CHARS = 12000;
/** Semantic-cache freshness window (seconds) — SEMANTIC_TTL in the hook. */
export const SEMANTIC_TTL_SECONDS = 14400; // 4 hours
/** `memory_indexer.py --recent 3` timeout (ms) — the hook's timeout=5. */
export const RECENT_SESSIONS_TIMEOUT_MS = 5000;

export type VaultContextSkipReason = 'non-control-group' | 'already-preloaded';

export type VaultContextSection =
  | 'degraded-memory-health'
  | 'vault-files'
  | 'checkpoint'
  | 'recent-sessions'
  | 'semantic-cache';

/**
 * Inspectable outcome metadata — section identifiers and configured
 * filenames ONLY, never vault contents (privacy: this record may reach logs).
 */
export interface VaultContextRecord {
  /** False only when a skip reason applied (never entered the pipeline). */
  eligible: boolean;
  skipReason?: VaultContextSkipReason;
  /** Whether the resolved vault path existed and was a directory. */
  vaultAvailable: boolean;
  /** Whether ANY section contributed content. */
  contextLoaded: boolean;
  /** Contributing providers, in canonical output order. */
  loadedSections: VaultContextSection[];
  /** Configured autoload filenames that actually loaded, in order. */
  loadedVaultFiles: string[];
}

export interface VaultContextResult {
  content: string | undefined;
  record: VaultContextRecord;
}

/**
 * Minimal spawned-child shape — what the recent-sessions adapter needs from
 * `child_process.spawn`'s return value, injectable for tests.
 */
export interface SpawnedChildLike {
  stdout: {
    on(event: 'data', cb: (chunk: Buffer | string) => void): unknown;
  } | null;
  on(event: 'error' | 'close', cb: (...args: unknown[]) => void): unknown;
  kill(): unknown;
}

export type SpawnFn = (
  command: string,
  args: string[],
  options: {
    stdio: ['ignore', 'pipe', 'ignore'];
    env: Record<string, string | undefined>;
  },
) => SpawnedChildLike;

/**
 * Injectable seams. Every root path / process / clock dependency is
 * overridable so unit tests never touch the real user vault, cache, config,
 * memory database, or session logs. Production callers pass nothing.
 */
export interface VaultContextDeps {
  /** Parsed ~/.config/deus/config.json (readDeusConfig — never throws). */
  readConfig: () => Record<string, unknown>;
  /** Vault resolution — env `DEUS_VAULT_PATH` precedence, then config. */
  resolveVault: (config: Record<string, unknown>) => string | null;
  /** Home dir — semantic-cache location (`<home>/.deus/resume_semantic_cache.txt`). */
  homeDir: string;
  /** Memory-tree DB (memory_health.py's DEFAULT_DB_PATH parity). */
  dbPath: string;
  /** Scripts dir — memory_indexer.py spawn + memory_tree.py remediation text. */
  scriptsDir: string;
  pythonBin: string;
  /** Env consulted for DEUS_VAULT_PRELOADED and passed to the subprocess. */
  env: Record<string, string | undefined>;
  /** Clock (ms epoch) — checkpoint "today" + semantic-cache freshness. */
  now: () => number;
  spawnFn: SpawnFn;
  /** Test seam only — production always uses RECENT_SESSIONS_TIMEOUT_MS. */
  recentTimeoutMs: number;
}

function defaultDeps(): VaultContextDeps {
  return {
    readConfig: readDeusConfig,
    resolveVault: (config) => resolveVaultPath(config),
    homeDir: HOME_DIR,
    dbPath: path.join(HOME_DIR, '.deus', 'memory_tree.db'),
    scriptsDir: path.join(PROJECT_ROOT, 'scripts'),
    pythonBin: PYTHON_BIN,
    env: process.env,
    now: Date.now,
    spawnFn: spawn as unknown as SpawnFn,
    recentTimeoutMs: RECENT_SESSIONS_TIMEOUT_MS,
  };
}

// ── Providers (each fail-soft, each returns '' for "contributes nothing") ──

/** Code-point-aware truncation — parity with Python's `content[:N]` slicing. */
function truncateCodePoints(content: string, max: number): string {
  if (content.length <= max) return content;
  return Array.from(content).slice(0, max).join('');
}

/**
 * `vault_autoload` normalization: default `["CLAUDE.md"]` ONLY when the key
 * is absent; an explicit empty array stays empty; malformed/non-string
 * entries contribute nothing rather than throwing.
 */
export function normalizeVaultAutoload(
  config: Record<string, unknown>,
): string[] {
  if (!('vault_autoload' in config)) return ['CLAUDE.md'];
  const raw = config['vault_autoload'];
  if (!Array.isArray(raw)) return [];
  return raw.filter((entry): entry is string => typeof entry === 'string');
}

/** One `=== VAULT: <fname> ===` section, or '' (missing/empty/unreadable). */
function loadVaultFileSection(vaultPath: string, fname: string): string {
  try {
    // Node's utf8 decode substitutes U+FFFD for invalid sequences — same
    // observable result as Python's errors="replace".
    const raw = fs.readFileSync(path.join(vaultPath, fname), 'utf-8');
    const content = truncateCodePoints(raw, MAX_SECTION_CHARS);
    if (content.trim().length === 0) return '';
    return `=== VAULT: ${fname} ===\n${content}`;
  } catch {
    return '';
  }
}

/**
 * Newest same-day checkpoint (`Checkpoints/<YYYY-MM-DD>-*.md`, greatest
 * mtime), or ''. "Today" is the LOCAL calendar date from the injected clock —
 * parity with Python's `date.today()`.
 */
function loadCheckpointSection(vaultPath: string, nowMs: number): string {
  try {
    const d = new Date(nowMs);
    const today = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
    const cpDir = path.join(vaultPath, 'Checkpoints');
    if (!fs.existsSync(cpDir) || !fs.statSync(cpDir).isDirectory()) return '';
    const candidates = fs
      .readdirSync(cpDir)
      .filter((name) => name.startsWith(`${today}-`) && name.endsWith('.md'))
      .map((name) => {
        const full = path.join(cpDir, name);
        return { full, mtimeMs: fs.statSync(full).mtimeMs };
      })
      .sort((a, b) => b.mtimeMs - a.mtimeMs);
    if (candidates.length === 0) return '';
    const content = fs.readFileSync(candidates[0].full, 'utf-8');
    return content.trim().length > 0
      ? `=== MID-SESSION CHECKPOINT ===\n${content}`
      : '';
  } catch {
    return '';
  }
}

/**
 * `memory_indexer.py --recent 3` via async spawn — the reference hook's
 * subprocess, reused rather than ported (the rendering logic lives in ONE
 * place). Returns trimmed stdout, or '' on spawn error / timeout / empty
 * output. Like the hook's `subprocess.run(check=False)`, trimmed stdout is
 * used even when the process exits non-zero; stderr is ignored. The await
 * blocks only this session-open turn — the spawn is asynchronous, so the
 * process-wide event loop stays available to other groups/channels/backends.
 */
function loadRecentSessionsOutput(deps: VaultContextDeps): Promise<string> {
  return new Promise((resolve) => {
    let settled = false;
    const finish = (value: string) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };

    let child: SpawnedChildLike;
    try {
      child = deps.spawnFn(
        deps.pythonBin,
        [path.join(deps.scriptsDir, 'memory_indexer.py'), '--recent', '3'],
        { stdio: ['ignore', 'pipe', 'ignore'], env: deps.env },
      );
    } catch {
      finish('');
      return;
    }

    let stdout = '';
    child.stdout?.on('data', (chunk) => {
      stdout += chunk.toString();
    });

    // Timeout parity: TimeoutExpired in the hook discards output entirely.
    const timer = setTimeout(() => {
      try {
        child.kill();
      } catch {
        // best-effort cleanup only
      }
      finish('');
    }, deps.recentTimeoutMs);

    child.on('error', () => {
      clearTimeout(timer);
      finish('');
    });
    child.on('close', () => {
      clearTimeout(timer);
      finish(stdout.trim());
    });
  });
}

/** Fresh (< 4h old, strictly) semantic resume cache section, or ''. */
function loadSemanticCacheSection(deps: VaultContextDeps): string {
  const cachePath = path.join(
    deps.homeDir,
    '.deus',
    'resume_semantic_cache.txt',
  );
  try {
    if (!fs.existsSync(cachePath)) return '';
    const ageSeconds = (deps.now() - fs.statSync(cachePath).mtimeMs) / 1000;
    if (ageSeconds >= SEMANTIC_TTL_SECONDS) return '';
    const content = fs.readFileSync(cachePath, 'utf-8').trim();
    return content ? `=== RELATED SESSIONS ===\n${content}` : '';
  } catch {
    return '';
  }
}

// ── Degraded memory health (scripts/memory_health.py TS port) ──────────────

/**
 * Vault writability probe — memory_health.py's `_probe_vault_writable`.
 * A create + unlink confirms the volume is mounted AND writable (`is_dir()`
 * alone misses the macOS Full-Disk-Access case where reads work but writes
 * silently fail). The probe filename is uniquely suffixed so independent
 * sessions opening concurrently never collide; cleanup is always attempted.
 */
function probeVaultWritable(vaultPath: string | null): string {
  if (vaultPath === null) return 'vault path is not configured';
  let isDir: boolean;
  try {
    isDir = fs.statSync(vaultPath).isDirectory();
  } catch {
    isDir = false;
  }
  if (!isDir) return `vault not mounted at ${vaultPath}`;
  const probe = path.join(
    vaultPath,
    `.deus-health-probe-${process.pid}-${Math.random().toString(36).slice(2, 10)}`,
  );
  let writable = true;
  try {
    fs.writeFileSync(probe, 'ok', 'utf-8');
  } catch {
    writable = false;
  } finally {
    try {
      fs.unlinkSync(probe);
    } catch {
      // Always attempt cleanup; a failed unlink must not surface.
    }
  }
  return writable
    ? ''
    : `vault not writable at ${vaultPath} (grant Full Disk Access?)`;
}

/**
 * Catastrophic-only tree signals from a direct, READ-ONLY better-sqlite3
 * read — memory_health.py's `_tree_health`, reusing the existing
 * better-sqlite3 dependency instead of adding a second Python startup to the
 * session-open path. A schema/query error means "cannot determine tree
 * health" and returns [] (never blocks session opening).
 */
function treeHealthLines(dbPath: string): string[] {
  if (!fs.existsSync(dbPath)) return [`memory tree DB missing at ${dbPath}`];
  let db: InstanceType<typeof Database>;
  try {
    db = new Database(dbPath, { readonly: true, fileMustExist: true });
  } catch {
    return [`memory tree DB unreadable at ${dbPath}`];
  }
  try {
    let nodes: number;
    let edges: number;
    let root: unknown;
    try {
      nodes = (
        db
          .prepare(
            "SELECT COUNT(*) AS c FROM nodes WHERE orphaned_at IS NULL AND path NOT LIKE 'auto-memory/%'",
          )
          .get() as { c: number }
      ).c;
      edges = (
        db
          .prepare(
            "SELECT COUNT(*) AS c FROM edges WHERE kind = 'child' AND expired_at IS NULL",
          )
          .get() as { c: number }
      ).c;
      root = db
        .prepare(
          "SELECT 1 AS one FROM nodes WHERE path = 'MEMORY_TREE.md' AND orphaned_at IS NULL",
        )
        .get();
    } catch {
      // Schema/probe error: "can't tell" — parity with the Python probe.
      return [];
    }
    if (nodes === 0) return ['memory tree is empty (0 active nodes)'];
    const lines: string[] = [];
    if (root === undefined) {
      lines.push('navigation root MEMORY_TREE.md is missing from the tree');
    }
    if (edges === 0) {
      lines.push(
        `memory graph has 0 edges across ${nodes} nodes (ranking dead)`,
      );
    }
    return lines;
  } finally {
    db.close();
  }
}

/**
 * Loud DEGRADED banner rendering — memory_health.py's
 * `render_degraded_section`, with the executable rendered via the
 * platform-selected Python binary and the `memory_tree.py` path derived from
 * the injected scripts dir (PROJECT_ROOT-based in production). On POSIX with
 * the default `python3` binary the output is byte-identical to the Python
 * reference; on Windows it is intentionally actionable (platform-native
 * executable/path) rather than byte-identical.
 */
function renderDegradedSection(
  lines: string[],
  pythonBin: string,
  memoryTreePath: string,
): string {
  const out = [
    '=== MEMORY SYSTEM DEGRADED ===',
    'Memory recall may be silently failing. Detected:',
    ...lines.map((line) => `  - ${line}`),
  ];
  const blob = lines.join(' ').toLowerCase();
  const remedies: string[] = [];
  if (
    blob.includes('not writable') ||
    blob.includes('not mounted') ||
    blob.includes('not configured')
  ) {
    remedies.push(
      'vault unavailable: remount / grant Full Disk Access, then restart the session',
    );
  }
  if (blob.includes('root')) {
    remedies.push(
      `lost root: ${pythonBin} ${memoryTreePath} scaffold-root --force --reindex`,
    );
  }
  if (
    blob.includes('db missing') ||
    blob.includes('empty') ||
    blob.includes('0 edges') ||
    blob.includes('unreadable')
  ) {
    remedies.push(`rebuild tree: ${pythonBin} ${memoryTreePath} build`);
  }
  remedies.push(`verify: ${pythonBin} ${memoryTreePath} check`);
  out.push('', 'Remediation:');
  out.push(...remedies.map((r) => `  - ${r}`));
  return out.join('\n');
}

/**
 * Degraded section, or '' when healthy. Any unexpected health-provider
 * exception is swallowed (contributes nothing, never blocks other providers)
 * — parity with the hook's `_memory_degraded_section` catch-all.
 */
function memoryDegradedSection(
  vaultPath: string | null,
  deps: VaultContextDeps,
): string {
  try {
    const lines: string[] = [];
    const vaultProblem = probeVaultWritable(vaultPath);
    if (vaultProblem) lines.push(vaultProblem);
    lines.push(...treeHealthLines(deps.dbPath));
    if (lines.length === 0) return '';
    return renderDegradedSection(
      lines,
      deps.pythonBin,
      path.join(deps.scriptsDir, 'memory_tree.py'),
    );
  } catch {
    return '';
  }
}

// ── Facade ──────────────────────────────────────────────────────────────────

function skipResult(skipReason: VaultContextSkipReason): VaultContextResult {
  return {
    content: undefined,
    record: {
      eligible: false,
      skipReason,
      vaultAvailable: false,
      contextLoaded: false,
      loadedSections: [],
      loadedVaultFiles: [],
    },
  };
}

/**
 * Load the session-open vault context for `runContext` — the facade over the
 * ordered provider pipeline (module doc comment). Returns `undefined`
 * content when ineligible or when no provider contributes, plus an
 * inspectable metadata record either way.
 *
 * `overrides` is a TEST seam only; production callers pass nothing.
 */
export async function loadVaultContext(
  runContext: RunContext,
  overrides?: Partial<VaultContextDeps>,
): Promise<VaultContextResult> {
  const deps: VaultContextDeps = { ...defaultDeps(), ...overrides };

  // Eligibility exits — BEFORE any config/filesystem/DB/subprocess work.
  if (!runContext.isControlGroup) return skipResult('non-control-group');
  if (deps.env['DEUS_VAULT_PRELOADED'] === '1') {
    return skipResult('already-preloaded');
  }

  const config = deps.readConfig();
  const vaultPath = deps.resolveVault(config);

  // Health first: a catastrophic memory failure must surface LOUDLY rather
  // than silently nuke recall (the "memory died for 4 days" incident) —
  // evaluated even when the vault itself is unavailable.
  const degraded = memoryDegradedSection(vaultPath, deps);

  const sections: string[] = [];
  const loadedSections: VaultContextSection[] = [];
  const loadedVaultFiles: string[] = [];
  if (degraded) {
    sections.push(degraded);
    loadedSections.push('degraded-memory-health');
  }

  let vaultAvailable = false;
  if (vaultPath !== null) {
    try {
      vaultAvailable = fs.statSync(vaultPath).isDirectory();
    } catch {
      vaultAvailable = false;
    }
  }

  if (!vaultAvailable || vaultPath === null) {
    // Degraded-banner-even-when-vault-missing exception: no context to
    // inject, but do NOT fail silently when the health probe can explain it.
    // (vaultAvailable can only be true when vaultPath !== null, but the
    // `|| vaultPath === null` disjunct lets TS narrow vaultPath to `string`
    // for every subsequent use below, instead of leaving it `string | null`.)
    return {
      content: sections.length > 0 ? sections.join('\n\n') : undefined,
      record: {
        eligible: true,
        vaultAvailable: false,
        contextLoaded: sections.length > 0,
        loadedSections,
        loadedVaultFiles,
      },
    };
  }

  for (const fname of normalizeVaultAutoload(config)) {
    const section = loadVaultFileSection(vaultPath, fname);
    if (section) {
      sections.push(section);
      loadedVaultFiles.push(fname);
      if (!loadedSections.includes('vault-files')) {
        loadedSections.push('vault-files');
      }
    }
  }

  const checkpoint = loadCheckpointSection(vaultPath, deps.now());
  if (checkpoint) {
    sections.push(checkpoint);
    loadedSections.push('checkpoint');
  }

  const recentOutput = await loadRecentSessionsOutput(deps);
  if (recentOutput) {
    sections.push(`=== RECENT SESSIONS ===\n${recentOutput}`);
    loadedSections.push('recent-sessions');
  }

  const semantic = loadSemanticCacheSection(deps);
  if (semantic) {
    sections.push(semantic);
    loadedSections.push('semantic-cache');
  }

  return {
    content: sections.length > 0 ? sections.join('\n\n') : undefined,
    record: {
      eligible: true,
      vaultAvailable: true,
      contextLoaded: sections.length > 0,
      loadedSections,
      loadedVaultFiles,
    },
  };
}
