/**
 * Evolution loop client for Deus host.
 *
 * Bridges the Node.js host to the Python evolution package via child_process.
 * Two roles:
 *   1. Pre-dispatch: fetch relevant reflections to prepend to the agent prompt.
 *   2. Post-dispatch: log interaction + trigger async judge eval (fire-and-forget).
 *
 * Falls back silently if the evolution package is not installed or the API key
 * is missing — the agent continues to work normally without reflections.
 */
import { execFile, spawn } from 'child_process';
import path from 'path';

import { logger } from './logger.js';

const EVOLUTION_CLI = path.join(process.cwd(), 'evolution', 'cli.py');
const PYTHON_BIN = process.env.EVOLUTION_PYTHON ?? 'python3';
const EVOLUTION_ENABLED = process.env.EVOLUTION_ENABLED !== '0';

export interface LogInteractionParams {
  id: string;
  prompt: string;
  response: string | null;
  groupFolder: string;
  latencyMs?: number;
  toolsUsed?: string[];
  sessionId?: string;
}

/**
 * Retrieve relevant reflections for the given query.
 * Returns a formatted block string (empty if none found or evolution disabled).
 * Blocks for up to 3 seconds — designed for pre-dispatch injection.
 */
export async function getReflections(
  query: string,
  groupFolder: string,
  toolsPlanned?: string[],
): Promise<string> {
  if (!EVOLUTION_ENABLED) return '';
  try {
    const payload = JSON.stringify({
      query,
      group_folder: groupFolder,
      tools_planned: toolsPlanned ?? [],
      top_k: 3,
    });
    const result = await _runPython(['get_reflections', payload], 3000);
    if (!result) return '';
    const parsed = JSON.parse(result);
    return parsed.reflections_block ?? '';
  } catch (err) {
    logger.debug({ err }, 'evolution: get_reflections failed (non-fatal)');
    return '';
  }
}

/**
 * Log an interaction and trigger async judge evaluation.
 * Fire-and-forget — does not block the response pipeline.
 */
export function logInteraction(params: LogInteractionParams): void {
  if (!EVOLUTION_ENABLED) return;
  const payload = JSON.stringify({
    id: params.id,
    prompt: params.prompt,
    response: params.response ?? '',
    group_folder: params.groupFolder,
    latency_ms: params.latencyMs,
    tools_used: params.toolsUsed ?? [],
    session_id: params.sessionId,
  });

  // Spawn detached so it survives even if the host process exits quickly
  const child = spawn(PYTHON_BIN, [EVOLUTION_CLI, 'log_interaction', payload], {
    detached: false,
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  child.stderr?.on('data', (d: Buffer) => {
    logger.debug(
      { data: d.toString().trim() },
      'evolution: log_interaction stderr',
    );
  });
  child.on('error', (err) => {
    logger.debug({ err }, 'evolution: log_interaction spawn error (non-fatal)');
  });
  // Do not await — fire and forget
}

function _runPython(args: string[], timeoutMs: number): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      PYTHON_BIN,
      [EVOLUTION_CLI, ...args],
      { timeout: timeoutMs, maxBuffer: 64 * 1024 },
      (err, stdout) => {
        if (err) return reject(err);
        resolve(stdout.trim());
      },
    );
  });
}
