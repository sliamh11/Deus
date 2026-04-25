/**
 * Pure predicate functions for checking system prerequisites.
 *
 * Single source of truth for "is X configured?" — used by the startup gate
 * and reusable by setup/verify.ts or other subsystems.
 *
 * All functions are synchronous, side-effect-free, and return structured results.
 */

import { execSync } from 'child_process';
import fs from 'fs';
import path from 'path';

import { HOME_DIR, CONFIG_DIR, STORE_DIR } from './config.js';
import { readEnvFile } from './env.js';

const DEUS_CONFIG_PATH = path.join(CONFIG_DIR, 'config.json');
const MEMORY_DB_PATH = path.join(HOME_DIR, '.deus', 'memory.db');
const CLAUDE_CREDENTIALS_PATH = path.join(
  HOME_DIR,
  '.claude',
  '.credentials.json',
);

/** Check if ~/.claude/.credentials.json has a valid OAuth access token. */
function hasClaudeCredentialsFile(): boolean {
  try {
    const raw = fs.readFileSync(CLAUDE_CREDENTIALS_PATH, 'utf-8');
    const parsed = JSON.parse(raw) as {
      claudeAiOauth?: { accessToken?: string };
    };
    return !!parsed?.claudeAiOauth?.accessToken;
  } catch {
    return false;
  }
}

/** Check if credentials for the selected default agent backend are configured. */
export function hasApiCredentials(): boolean {
  const env = readEnvFile([
    'DEUS_AGENT_BACKEND',
    'ANTHROPIC_API_KEY',
    'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_AUTH_TOKEN',
    'OPENAI_API_KEY',
  ]);
  const backend = (
    process.env.DEUS_AGENT_BACKEND ||
    env.DEUS_AGENT_BACKEND ||
    'claude'
  ).toLowerCase();
  if (backend === 'openai') {
    return !!(env.OPENAI_API_KEY || process.env.OPENAI_API_KEY);
  }

  return !!(
    env.ANTHROPIC_API_KEY ||
    env.CLAUDE_CODE_OAUTH_TOKEN ||
    env.ANTHROPIC_AUTH_TOKEN ||
    process.env.ANTHROPIC_API_KEY ||
    process.env.CLAUDE_CODE_OAUTH_TOKEN ||
    process.env.ANTHROPIC_AUTH_TOKEN ||
    hasClaudeCredentialsFile()
  );
}

/** Check if a Gemini API key is configured for memory embeddings. */
export function hasGeminiApiKey(): boolean {
  const env = readEnvFile(['GEMINI_API_KEY']);
  return !!(env.GEMINI_API_KEY || process.env.GEMINI_API_KEY);
}

/** Read the Deus config file (~/.config/deus/config.json). */
export function readDeusConfig(): Record<string, unknown> {
  try {
    return JSON.parse(fs.readFileSync(DEUS_CONFIG_PATH, 'utf-8'));
  } catch {
    return {};
  }
}

/** Check if the memory vault directory is configured and exists. */
export function hasMemoryVault(): { ok: boolean; path: string | null } {
  const vaultPath =
    process.env.DEUS_VAULT_PATH ||
    (readDeusConfig().vault_path as string | undefined);

  if (!vaultPath) {
    return { ok: false, path: null };
  }

  const resolved = vaultPath.startsWith('~')
    ? path.join(HOME_DIR, vaultPath.slice(1))
    : vaultPath;

  if (!fs.existsSync(resolved)) {
    return { ok: false, path: resolved };
  }

  return { ok: true, path: resolved };
}

/**
 * Resolve the Python executable name.
 * Tries `python3` first (Unix standard), then `python` (Windows / some envs).
 * Returns the working command, or null if Python is not available.
 */
export function resolvePython(): string | null {
  for (const cmd of ['python3', 'python']) {
    try {
      execSync(`${cmd} --version`, { stdio: 'pipe', timeout: 5000 });
      return cmd;
    } catch {
      // try next
    }
  }
  return null;
}

/** Check if Python 3 and required packages (sqlite-vec, google-genai) are available. */
export function hasPythonDeps(): { ok: boolean; missing: string[] } {
  const missing: string[] = [];

  const python = resolvePython();
  if (!python) {
    return { ok: false, missing: ['python3'] };
  }

  // Check sqlite-vec
  try {
    execSync(`${python} -c "import sqlite_vec"`, {
      stdio: 'pipe',
      timeout: 5000,
    });
  } catch {
    missing.push('sqlite-vec');
  }

  // Check google-genai
  try {
    execSync(`${python} -c "from google import genai"`, {
      stdio: 'pipe',
      timeout: 5000,
    });
  } catch {
    missing.push('google-genai');
  }

  return { ok: missing.length === 0, missing };
}

/** Check if the memory database exists. */
export function hasMemoryDb(): boolean {
  return fs.existsSync(MEMORY_DB_PATH);
}

/** Check if any messaging channel has credentials configured. */
export function hasAnyChannelAuth(): boolean {
  // Channels are installed via skills (/add-whatsapp, /add-telegram, etc.).
  // Each skill leaves credentials on disk. Check for known credential patterns.
  const checks: Array<() => boolean> = [
    // WhatsApp: store/auth/creds.json
    () => fs.existsSync(path.join(STORE_DIR, 'auth', 'creds.json')),
    // Token-based channels (Telegram, Slack, Discord, etc.)
    () => {
      const env = readEnvFile([
        'TELEGRAM_BOT_TOKEN',
        'SLACK_BOT_TOKEN',
        'DISCORD_BOT_TOKEN',
      ]);
      return !!(
        env.TELEGRAM_BOT_TOKEN ||
        env.SLACK_BOT_TOKEN ||
        env.DISCORD_BOT_TOKEN
      );
    },
  ];
  return checks.some((check) => check());
}

/** Check if the agent container image has been built. */
export function hasContainerImage(): boolean {
  const runtime = process.env.CONTAINER_RUNTIME || 'docker';
  const bin = runtime === 'container' ? 'container' : 'docker';
  try {
    execSync(`${bin} image inspect deus-agent`, {
      stdio: 'pipe',
      timeout: 5000,
    });
    return true;
  } catch {
    return false;
  }
}

/** Count registered groups in the database (opens readonly, safe before initDatabase). */
export function countRegisteredGroups(): number {
  const dbPath = path.join(STORE_DIR, 'messages.db');
  if (!fs.existsSync(dbPath)) return 0;

  try {
    // Use node -e with better-sqlite3 instead of sqlite3 CLI — sqlite3 is not
    // available on Windows. This spawns a short-lived node process that loads
    // better-sqlite3 (already an npm dependency) and prints the count.
    const script = `const D=require('better-sqlite3');const db=new D(${JSON.stringify(dbPath)},{readonly:true});try{const r=db.prepare('SELECT COUNT(*) as cnt FROM registered_groups').get();console.log(r?r.cnt:0)}finally{db.close()}`;
    const result = execSync(`node -e "${script.replace(/"/g, '\\"')}"`, {
      encoding: 'utf-8',
      stdio: ['pipe', 'pipe', 'pipe'],
      timeout: 5000,
    });
    return parseInt(result.trim(), 10) || 0;
  } catch {
    return 0;
  }
}
