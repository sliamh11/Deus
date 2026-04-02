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
import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';

import { HOME_DIR, CONFIG_DIR } from '../src/config.js';
import { resolvePython } from '../src/checks.js';
import { logger } from '../src/logger.js';
import { emitStatus } from './status.js';

const CONFIG_PATH = path.join(CONFIG_DIR, 'config.json');
const DEUS_ENV_PATH = path.join(process.cwd(), '.env');
const DEFAULT_VAULT_PATH = path.join(HOME_DIR, '.deus', 'vault');
const MEMORY_INDEXER = path.join(process.cwd(), 'scripts', 'memory_indexer.py');

const VAULT_SUBDIRS = [
  'Session-Logs',
  'Atoms',
  'Checkpoints',
  'Persona',
];

export async function run(args: string[]): Promise<void> {
  logger.info('Starting memory system setup');

  // ── 1. Check Python ──────────────────────────────────────────────────────
  // Resolve python3 or python (Windows may only have `python` in PATH)
  const pythonCmd = resolvePython();
  if (!pythonCmd) {
    emitStatus('MEMORY', {
      STATUS: 'failed',
      ERROR: 'Python 3 not found. Install Python 3.11+ to enable the memory system.',
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
      ERROR: 'Python 3 not found. Install Python 3.11+ to enable the memory system.',
      STEP: 'python_check',
    });
    return;
  }

  logger.info({ pythonVersion }, 'Python found');

  // ── 2. Check/install Python dependencies ─────────────────────────────────
  const missing: string[] = [];
  try {
    execSync('${pythonCmd} -c "import sqlite_vec"', { stdio: 'pipe', timeout: 5000 });
  } catch {
    missing.push('sqlite-vec');
  }
  try {
    execSync('${pythonCmd} -c "from google import genai"', { stdio: 'pipe', timeout: 5000 });
  } catch {
    missing.push('google-genai');
  }

  if (missing.length > 0) {
    // Attempt to auto-install missing packages.
    logger.info({ missing }, 'Installing missing Python dependencies');
    const requirementsPath = path.join(process.cwd(), 'evolution', 'requirements.txt');
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
      execSync('${pythonCmd} -c "import sqlite_vec"', { stdio: 'pipe', timeout: 5000 });
    } catch {
      stillMissing.push('sqlite-vec');
    }
    try {
      execSync('${pythonCmd} -c "from google import genai"', { stdio: 'pipe', timeout: 5000 });
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

  // If vault_path is provided as arg, use it; otherwise check config; otherwise default
  const vaultArg = args.find((a) => a.startsWith('--vault-path='));
  let vaultPath = vaultArg
    ? vaultArg.split('=', 2)[1]
    : (config.vault_path as string | undefined) || DEFAULT_VAULT_PATH;

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

  // Create a minimal CLAUDE.md if it doesn't exist
  const claudeMdPath = path.join(vaultPath, 'CLAUDE.md');
  if (!fs.existsSync(claudeMdPath)) {
    fs.writeFileSync(
      claudeMdPath,
      [
        '---',
        'type: permanent-memory',
        `updated: ${new Date().toISOString().split('T')[0]}`,
        '---',
        '',
        '# Deus Memory',
        '',
        'This file is the root of your Deus memory vault.',
        'Session logs, atoms, and checkpoints are stored alongside it.',
        '',
      ].join('\n'),
    );
  }

  logger.info({ vaultPath }, 'Vault directory ready');

  // ── 4. Write config ──────────────────────────────────────────────────────
  fs.mkdirSync(CONFIG_DIR, { recursive: true });
  config.vault_path = vaultPath;
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2) + '\n');
  logger.info({ configPath: CONFIG_PATH }, 'Config written');

  // ── 5. Initialize memory database ────────────────────────────────────────
  try {
    execSync(
      `${pythonCmd} "${MEMORY_INDEXER}" --rebuild`,
      {
        encoding: 'utf-8',
        stdio: ['pipe', 'pipe', 'pipe'],
        timeout: 30000,
        env: { ...process.env, DEUS_VAULT_PATH: vaultPath },
      },
    );
    logger.info('Memory database initialized');
  } catch (err) {
    const message = err instanceof Error ? (err as { stderr?: string }).stderr || err.message : String(err);
    logger.warn({ err: message }, 'Memory database init failed (may need Gemini API key)');
  }

  // ── 6. Check Gemini API key ──────────────────────────────────────────────
  let hasGeminiKey = false;
  try {
    const envContent = fs.readFileSync(DEUS_ENV_PATH, 'utf-8');
    hasGeminiKey = envContent.includes('GEMINI_API_KEY=');
  } catch {
    // File doesn't exist
  }

  emitStatus('MEMORY', {
    STATUS: 'success',
    PYTHON_VERSION: pythonVersion,
    VAULT_PATH: vaultPath,
    CONFIG_PATH: CONFIG_PATH,
    HAS_GEMINI_KEY: hasGeminiKey,
    GEMINI_KEY_HINT: hasGeminiKey
      ? 'configured'
      : 'Not set — memory search disabled. Get a free key at https://aistudio.google.com/apikey',
    STEP: 'complete',
  });
}
