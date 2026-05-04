/**
 * Step: memory — Set up the memory system (vault directory, Python deps, config).
 *
 * Checks and configures everything the memory system needs:
 *   1. Python 3.11+ available
 *   2. sqlite-vec and google-genai installed (offers to install if missing)
 *   3. Vault directory created with expected structure
 *   4. Config file written (~/.config/deus/config.json)
 *   5. Memory database initialized
 *   6. Gemini API key (suggested, not required)
 */
import { execSync, spawnSync } from 'child_process';
import fs from 'fs';
import path from 'path';

import { HOME_DIR, CONFIG_DIR } from '../src/config.js';
import { resolvePython } from '../src/checks.js';
import { logger } from '../src/logger.js';
import { emitStatus } from './status.js';

const CONFIG_PATH = path.join(CONFIG_DIR, 'config.json');
const DEUS_ENV_PATH = path.join(process.cwd(), '.env');
const DEFAULT_VAULT_PATH = path.join(HOME_DIR, '.deus', 'vault');
const PROJECT_LOCAL_VAULT_PATH = path.join(process.cwd(), '.vault');
const MEMORY_INDEXER = path.join(process.cwd(), 'scripts', 'memory_indexer.py');

const VAULT_SUBDIRS = ['Session-Logs', 'Atoms', 'Checkpoints', 'Persona'];

export async function run(args: string[]): Promise<void> {
  logger.info('Starting memory system setup');

  // ── 1. Check Python ──────────────────────────────────────────────────────
  // Resolve python3 or python (Windows may only have `python` in PATH)
  const pythonCmd = resolvePython();
  if (!pythonCmd) {
    emitStatus('MEMORY', {
      STATUS: 'failed',
      ERROR:
        'Python 3 not found. Install Python 3.11+ to enable the memory system.',
      STEP: 'python_check',
    });
    return;
  }
  let pythonVersion = '';
  try {
    pythonVersion = execSync(`${pythonCmd} --version`, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      timeout: 5000,
    }).trim();
  } catch {
    emitStatus('MEMORY', {
      STATUS: 'failed',
      ERROR:
        'Python 3 not found. Install Python 3.11+ to enable the memory system.',
      STEP: 'python_check',
    });
    return;
  }

  logger.info({ pythonVersion }, 'Python found');

  // ── 2. Check/install Python dependencies ─────────────────────────────────
  const missing: string[] = [];
  try {
    execSync(`${pythonCmd} -c "import sqlite_vec"`, {
      stdio: 'pipe',
      timeout: 5000,
    });
  } catch {
    missing.push('sqlite-vec');
  }
  try {
    execSync(`${pythonCmd} -c "from google import genai"`, {
      stdio: 'pipe',
      timeout: 5000,
    });
  } catch {
    missing.push('google-genai');
  }

  if (missing.length > 0) {
    // Attempt to auto-install missing packages.
    logger.info({ missing }, 'Installing missing Python dependencies');
    const requirementsPath = path.join(
      process.cwd(),
      'evolution',
      'requirements.txt',
    );
    const installCmd = fs.existsSync(requirementsPath)
      ? `pip install -r "${requirementsPath}"`
      : `pip install ${missing.join(' ')}`;
    try {
      execSync(installCmd, {
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
        timeout: 120000,
      });
      logger.info('Python dependencies installed');
    } catch (pipErr) {
      const pipMsg =
        pipErr instanceof Error
          ? (pipErr as { stderr?: string }).stderr || pipErr.message
          : String(pipErr);
      logger.warn({ err: pipMsg }, 'pip install failed');
      emitStatus('MEMORY', {
        STATUS: 'warn',
        WARNING: `Could not install Python dependencies automatically: ${pipMsg.slice(0, 200)}`,
        MISSING_PACKAGES: missing.join(', '),
        INSTALL_COMMAND: installCmd,
        STEP: 'python_deps',
      });
      return;
    }

    // Re-check after install.
    const stillMissing: string[] = [];
    try {
      execSync(`${pythonCmd} -c "import sqlite_vec"`, {
        stdio: 'pipe',
        timeout: 5000,
      });
    } catch {
      stillMissing.push('sqlite-vec');
    }
    try {
      execSync(`${pythonCmd} -c "from google import genai"`, {
        stdio: 'pipe',
        timeout: 5000,
      });
    } catch {
      stillMissing.push('google-genai');
    }

    if (stillMissing.length > 0) {
      emitStatus('MEMORY', {
        STATUS: 'warn',
        WARNING: `Packages still missing after install attempt: ${stillMissing.join(', ')}`,
        MISSING_PACKAGES: stillMissing.join(', '),
        INSTALL_COMMAND: installCmd,
        STEP: 'python_deps',
      });
      return;
    }
  }

  logger.info('Python dependencies OK');

  // ── 3. Configure vault path ──────────────────────────────────────────────
  // Read existing config or use default
  let config: Record<string, unknown> = {};
  try {
    config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'));
  } catch {
    // No existing config
  }

  // Vault location options:
  //   --vault-path=<path>   Explicit path (Obsidian vault, NAS, Dropbox, etc.)
  //   --vault-mode=local    Project-local vault at .vault/ (gitignored, portable)
  //   --vault-mode=default  Standard location at ~/.deus/vault/ (default)
  //   (no args)             Use existing config, or default
  const vaultPathArg = args.find((a) => a.startsWith('--vault-path='));
  const vaultModeArg = args.find((a) => a.startsWith('--vault-mode='));
  const vaultMode = vaultModeArg?.split('=', 2)[1];

  let vaultPath: string;
  if (vaultPathArg) {
    vaultPath = vaultPathArg.split('=', 2)[1];
  } else if (vaultMode === 'local') {
    vaultPath = PROJECT_LOCAL_VAULT_PATH;
  } else if (vaultMode === 'default') {
    vaultPath = DEFAULT_VAULT_PATH;
  } else {
    vaultPath = (config.vault_path as string | undefined) || DEFAULT_VAULT_PATH;
  }

  // Expand ~ if needed
  if (vaultPath.startsWith('~')) {
    vaultPath = path.join(HOME_DIR, vaultPath.slice(1));
  }
  vaultPath = path.resolve(vaultPath);

  // Create vault directory structure
  fs.mkdirSync(vaultPath, { recursive: true });
  for (const subdir of VAULT_SUBDIRS) {
    fs.mkdirSync(path.join(vaultPath, subdir), { recursive: true });
  }

  // Seed CLAUDE.md if missing. CLAUDE.md holds identity, critical rules,
  // index pointers, and session state (previous/pending). Auto-loaded every
  // turn; written by /compress.
  const today = new Date().toISOString().split('T')[0];

  const claudeMdPath = path.join(vaultPath, 'CLAUDE.md');
  if (!fs.existsSync(claudeMdPath)) {
    const body = [
      '---',
      'type: permanent-memory',
      'title: Deus User Profile',
      'description: >',
      '  Identity + critical rules + index of where everything else lives.',
      '  Leaves load on demand via memory_tree.',
      `updated: ${today}`,
      'critical:',
      '  - identity',
      '  - security',
      '  - data-integrity',
      '  - wait-for-approval',
      '  - feature-branch',
      '---',
      '',
      '# Identity',
      'name:  # your name',
      '',
      '# Critical rules (enforced every turn)',
      'security: never commit credentials, API keys, or tokens — treat every repo as public',
      'data-integrity: never lose, overwrite, or downgrade data; merge, do not replace; ask before destructive ops',
      'wait-for-approval: risky or irreversible actions (rm -rf, force-push, hard-reset, drop table) require explicit user approval',
      'feature-branch: create a feature branch before non-trivial changes; never commit directly to main',
      '',
      '# User-specific rules',
      '# Add your own rules below in `key: value` format.',
      '',
      'previous:',
      '  # Last 3 session summaries, rewritten by /compress.',
      '',
      'pending:',
      '  # Open tasks in `- [ ] ...` format, rewritten by /compress.',
      '',
      '# Index (load on demand)',
      'Study:   STUDY.md         (load on demand for study sessions)',
      'Infra:   INFRA.md         (load on demand for tooling/infra work)',
      'Persona: Persona/INDEX.md (load on demand for user preferences / background)',
      '',
      '# Memory priority',
      '# 1. `python3 ~/deus/scripts/memory_tree.py query "<q>"` → read top result',
      '# 2. Persona/INDEX.md fallback if the tree abstains',
      '# 3. CLAUDE.md hints only as last resort',
      '',
    ].join('\n');

    fs.writeFileSync(claudeMdPath, body);
  }

  logger.info({ vaultPath }, 'Vault directory ready');

  // ── 4. Write config ──────────────────────────────────────────────────────
  fs.mkdirSync(CONFIG_DIR, { recursive: true });
  config.vault_path = vaultPath;

  if (!config.max_parallel_agents) {
    try {
      const hw = spawnSync(pythonCmd, ['-m', 'evolution.hardware'], {
        encoding: 'utf-8',
        cwd: process.cwd(),
        env: process.env,
      });
      if (hw.status === 0 && hw.stdout) {
        const report = JSON.parse(hw.stdout.trim());
        const cores: number = report?.hardware?.cores ?? 0;
        if (cores > 0) {
          config.max_parallel_agents = Math.max(
            2,
            Math.min(8, Math.floor(cores / 2)),
          );
          logger.info(
            { cores, max_parallel_agents: config.max_parallel_agents },
            'Agent limit set from hardware',
          );
        }
      }
    } catch {
      // Hardware detection is optional — no failure on exotic platforms
    }
  }

  fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + '\n');
  logger.info({ configPath: CONFIG_PATH }, 'Config written');

  // ── 5. Initialize memory database ────────────────────────────────────────
  try {
    execSync(`${pythonCmd} "${MEMORY_INDEXER}" --rebuild`, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      timeout: 30000,
      env: { ...process.env, DEUS_VAULT_PATH: vaultPath },
    });
    logger.info('Memory database initialized');
  } catch (err) {
    const message =
      err instanceof Error
        ? (err as { stderr?: string }).stderr || err.message
        : String(err);
    logger.warn(
      { err: message },
      'Memory database init failed (may need Gemini API key)',
    );
  }

  // ── 6. Check Gemini API key ──────────────────────────────────────────────
  let hasGeminiKey = false;
  try {
    const envContent = fs.readFileSync(DEUS_ENV_PATH, 'utf-8');
    hasGeminiKey = envContent.includes('GEMINI_API_KEY=');
  } catch {
    // File doesn't exist
  }

  const vaultLocation =
    vaultPath === path.resolve(PROJECT_LOCAL_VAULT_PATH)
      ? 'project-local (.vault/)'
      : vaultPath === path.resolve(DEFAULT_VAULT_PATH)
        ? 'default (~/.deus/vault/)'
        : 'custom';

  emitStatus('MEMORY', {
    STATUS: 'success',
    PYTHON_VERSION: pythonVersion,
    VAULT_PATH: vaultPath,
    VAULT_LOCATION: vaultLocation,
    CONFIG_PATH: CONFIG_PATH,
    HAS_GEMINI_KEY: hasGeminiKey,
    GEMINI_KEY_HINT: hasGeminiKey
      ? 'configured'
      : 'Not set — memory search disabled. Get a free key at https://aistudio.google.com/apikey',
    STEP: 'complete',
  });
}
