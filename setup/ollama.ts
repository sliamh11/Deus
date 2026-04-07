/**
 * Step: ollama — Detect hardware, recommend a judge model, pull if approved.
 *
 * This step is optional — Ollama is not required for Deus to function.
 * If Ollama is not installed the step exits gracefully without failing setup.
 *
 * Flow:
 *   1. Check if `ollama` CLI exists — skip entirely if not found.
 *   2. Call `python3 -m evolution.hardware` for hardware info + recommendation.
 *   3. Check if the recommended model is already pulled (skip pull if so).
 *   4. Pull the model and write OLLAMA_MODEL to ~/.config/deus/.env.
 */
import { execSync, spawnSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';

import { CONFIG_DIR } from '../src/config.js';
import { logger } from '../src/logger.js';
import { commandExists } from './platform.js';
import { emitStatus } from './status.js';

interface HardwareInfo {
  os: string;
  arch: string;
  ram_gb: number;
  cores: number;
  gpu: string;
}

interface HardwareReport {
  hardware: HardwareInfo;
  recommendation: {
    model: string;
    size_gb: number;
    fits: boolean;
  };
}

/** Read a single KEY=value from a .env file, return undefined if not present. */
function readEnvKey(envPath: string, key: string): string | undefined {
  if (!fs.existsSync(envPath)) return undefined;
  const lines = fs.readFileSync(envPath, 'utf-8').split('\n');
  for (const line of lines) {
    const trimmed = line.trim();
    if (trimmed.startsWith(`${key}=`)) {
      return trimmed.slice(key.length + 1).trim();
    }
  }
  return undefined;
}

/** Write or update a single KEY=value in a .env file, preserving other lines. */
function writeEnvKey(envPath: string, key: string, value: string): void {
  const dir = path.dirname(envPath);
  if (!fs.existsSync(dir)) {
    fs.mkdirSync(dir, { recursive: true });
  }

  let content = '';
  if (fs.existsSync(envPath)) {
    const lines = fs.readFileSync(envPath, 'utf-8').split('\n');
    const filtered = lines.filter((l) => !l.trim().startsWith(`${key}=`));
    content = filtered.join('\n');
    if (content.length > 0 && !content.endsWith('\n')) {
      content += '\n';
    }
  }

  content += `${key}=${value}\n`;
  fs.writeFileSync(envPath, content, 'utf-8');
}

/** Check if a model is already pulled by scanning `ollama list` output. */
function isModelPulled(modelName: string): boolean {
  try {
    const result = execSync('ollama list', { encoding: 'utf-8' });
    // ollama list output: "NAME  ID  SIZE  MODIFIED"
    // Model name is in the first column; strip the tag suffix for partial match
    const baseName = modelName.split(':')[0];
    const tag = modelName.includes(':') ? modelName.split(':')[1] : '';
    return result.split('\n').some((line) => {
      const col = line.trim().split(/\s+/)[0];
      if (!col) return false;
      if (tag) return col === modelName || col.startsWith(modelName);
      return col === baseName || col.startsWith(`${baseName}:`);
    });
  } catch {
    return false;
  }
}

/** Invoke `python3 -m evolution.hardware` and parse the JSON output. */
function getHardwareReport(): HardwareReport | null {
  try {
    const result = spawnSync('python3', ['-m', 'evolution.hardware'], {
      encoding: 'utf-8',
      cwd: process.cwd(),
      env: process.env,
    });

    if (result.status !== 0 || !result.stdout) {
      logger.warn(
        { stderr: result.stderr },
        'evolution.hardware module not available — skipping hardware detection',
      );
      return null;
    }

    return JSON.parse(result.stdout.trim()) as HardwareReport;
  } catch {
    return null;
  }
}

export async function run(_args: string[]): Promise<void> {
  // ── 1. Check if Ollama is installed ────────────────────────────────────────
  if (!commandExists('ollama')) {
    logger.info('Ollama not found — skipping model advisor step');
    emitStatus('OLLAMA', {
      STATUS: 'skipped',
      REASON: 'ollama_not_installed',
      NOTE: 'Install Ollama from https://ollama.ai to enable local judge models',
    });
    return;
  }

  logger.info('Ollama detected — running hardware advisor');

  // ── 2. Get hardware report ──────────────────────────────────────────────────
  const report = getHardwareReport();

  if (!report) {
    logger.warn('Hardware detection unavailable — skipping model pull');
    emitStatus('OLLAMA', {
      STATUS: 'skipped',
      REASON: 'hardware_detection_unavailable',
      NOTE: 'Run: python3 -m evolution.hardware to diagnose',
    });
    return;
  }

  const { hardware: hw, recommendation: rec } = report;

  logger.info({ hw, recommendation: rec }, 'Hardware detection complete');

  const ramDisplay = hw.ram_gb > 0 ? `${Math.round(hw.ram_gb)} GB` : 'unknown';
  const gpuDisplay = hw.gpu !== 'unknown' ? hw.gpu : 'unknown GPU';

  // ── 3. Check if model is already pulled ────────────────────────────────────
  const envPath = path.join(CONFIG_DIR, '.env');
  const existingModel = readEnvKey(envPath, 'OLLAMA_MODEL');

  if (isModelPulled(rec.model)) {
    logger.info({ model: rec.model }, 'Recommended model already pulled');

    // Ensure the .env reflects the current recommendation
    if (existingModel !== rec.model) {
      writeEnvKey(envPath, 'OLLAMA_MODEL', rec.model);
      logger.info({ model: rec.model, envPath }, 'Updated OLLAMA_MODEL in env');
    }

    emitStatus('OLLAMA', {
      STATUS: 'success',
      MODEL: rec.model,
      MODEL_SIZE_GB: rec.size_gb,
      RAM_GB: hw.ram_gb,
      GPU: hw.gpu,
      PULLED: false,
      ALREADY_PRESENT: true,
      ENV_UPDATED: existingModel !== rec.model,
    });
    return;
  }

  // ── 4. Pull the model ───────────────────────────────────────────────────────
  console.log(
    `\nDetected ${ramDisplay} RAM, ${gpuDisplay}. ` +
      `Recommended model: ${rec.model} (${rec.size_gb} GB)\n`,
  );

  if (!rec.fits) {
    console.warn(
      `Warning: ${rec.model} may not fit in available RAM (${ramDisplay}). ` +
        'Proceeding anyway — Ollama will page if needed.\n',
    );
  }

  logger.info({ model: rec.model }, 'Pulling Ollama model');
  console.log(`Pulling ${rec.model}...`);

  const pull = spawnSync('ollama', ['pull', rec.model], {
    stdio: 'inherit',
    encoding: 'utf-8',
  });

  if (pull.status !== 0) {
    logger.error(
      { model: rec.model, status: pull.status },
      'ollama pull failed',
    );
    emitStatus('OLLAMA', {
      STATUS: 'failed',
      MODEL: rec.model,
      ERROR: `ollama pull exited with code ${pull.status ?? 'unknown'}`,
    });
    // Non-fatal: don't exit(1) — Ollama is optional
    return;
  }

  // ── 5. Write OLLAMA_MODEL to ~/.config/deus/.env ───────────────────────────
  writeEnvKey(envPath, 'OLLAMA_MODEL', rec.model);
  logger.info({ model: rec.model, envPath }, 'Wrote OLLAMA_MODEL to env');

  emitStatus('OLLAMA', {
    STATUS: 'success',
    MODEL: rec.model,
    MODEL_SIZE_GB: rec.size_gb,
    RAM_GB: hw.ram_gb,
    GPU: hw.gpu,
    PULLED: true,
    ALREADY_PRESENT: false,
    ENV_UPDATED: true,
  });
}
