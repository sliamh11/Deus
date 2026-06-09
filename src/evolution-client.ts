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
import { randomUUID } from 'crypto';
import path from 'path';

import { logger } from './logger.js';
import { emojiToSignal } from './reaction-signal.js';

const EVOLUTION_CLI = path.join(process.cwd(), 'evolution', 'cli.py');
const PYTHON_BIN = process.env.EVOLUTION_PYTHON ?? 'python3';
const EVOLUTION_ENABLED = process.env.EVOLUTION_ENABLED !== '0';
// Kill switch for the DSPy optimizer arm's prompt injection (LIA-131 Phase 2).
// Default OFF — the arm ships dark until shadow deltas justify flipping it per
// module. Checked before any subprocess spawn so default-off adds zero latency.
// Read once at module load (like EVOLUTION_ENABLED): flipping it needs a process
// restart, which matches how the long-running service picks up env changes.
const OPTIMIZED_PROMPTS_ENABLED =
  process.env.EVOLUTION_OPTIMIZED_PROMPTS === '1';

/**
 * One structured tool call captured in-container (LIA-154). Mirrors the fields
 * the offline mechanical scorers consume (evolution/judge/mechanical.py).
 * Observability only — not read by the live scoring path yet.
 */
export interface ToolCall {
  name: string;
  file_path?: string;
  command?: string;
  subagent_type?: string;
  is_error?: boolean;
  tool_use_id?: string;
  session_id?: string | null;
  ts?: string;
}

export interface LogInteractionParams {
  id: string;
  prompt: string;
  response: string | null;
  groupFolder: string;
  latencyMs?: number;
  toolsUsed?: string[];
  /** Structured per-call records (LIA-154 observability; not yet scored). */
  toolCalls?: ToolCall[];
  /** Offered tool manifest for this dispatch (LIA-154; unblocks LIA-151). */
  availableTools?: string[];
  sessionId?: string;
  domainPresets?: string[];
  userSignal?: string;
  retrievedReflectionIds?: string[];
  contextTokens?: number;
  hasCode?: boolean;
}

export interface ReflectionsResult {
  block: string;
  reflectionIds: string[];
}

export interface ActivePromptResult {
  /** Sanitized, boundary-tagged optimized-prompt block, or '' when none. */
  block: string;
  artifactId?: string;
  baselineScore?: number;
  optimizedScore?: number;
  sampleCount?: number;
}

const EMPTY_ACTIVE_PROMPT: ActivePromptResult = { block: '' };

/**
 * Retrieve the active DSPy-optimized prompt block for a module (LIA-131 Phase 2).
 *
 * Returns an empty block unless BOTH EVOLUTION_ENABLED and
 * EVOLUTION_OPTIMIZED_PROMPTS are on — the gate is checked BEFORE the Python
 * subprocess, so the default-off path costs nothing on the dispatch hot path.
 * The Python helper sanitizes (boundary tags + length cap) and rejects the
 * trivial default, so this returns only content that is safe to inject as-is.
 */
export async function getActivePrompt(
  module: string,
): Promise<ActivePromptResult> {
  if (!EVOLUTION_ENABLED || !OPTIMIZED_PROMPTS_ENABLED) {
    return EMPTY_ACTIVE_PROMPT;
  }
  try {
    const result = await _runPython(['get_active_prompt', module], 3000);
    if (!result) return EMPTY_ACTIVE_PROMPT;
    const parsed = JSON.parse(result);
    if (!parsed.block) return EMPTY_ACTIVE_PROMPT;
    return {
      block: parsed.block,
      artifactId: parsed.artifact_id ?? undefined,
      baselineScore: parsed.baseline_score ?? undefined,
      optimizedScore: parsed.optimized_score ?? undefined,
      sampleCount: parsed.sample_count ?? undefined,
    };
  } catch (err) {
    logger.debug({ err }, 'evolution: get_active_prompt failed (non-fatal)');
    return EMPTY_ACTIVE_PROMPT;
  }
}

/**
 * Retrieve relevant reflections for the given query.
 * Returns a formatted block string and the IDs of retrieved reflections.
 * Blocks for up to 3 seconds — designed for pre-dispatch injection.
 */
export async function getReflections(
  query: string,
  groupFolder: string,
  toolsPlanned?: string[],
): Promise<ReflectionsResult> {
  if (!EVOLUTION_ENABLED) return { block: '', reflectionIds: [] };
  try {
    const payload = JSON.stringify({
      query,
      group_folder: groupFolder,
      tools_planned: toolsPlanned ?? [],
      top_k: 3,
    });
    const result = await _runPython(['get_reflections', payload], 3000);
    if (!result) return { block: '', reflectionIds: [] };
    const parsed = JSON.parse(result);
    return {
      block: parsed.reflections_block ?? '',
      reflectionIds: parsed.reflection_ids ?? [],
    };
  } catch (err) {
    logger.debug({ err }, 'evolution: get_reflections failed (non-fatal)');
    return { block: '', reflectionIds: [] };
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
    tool_calls: params.toolCalls ?? [],
    available_tools: params.availableTools ?? [],
    session_id: params.sessionId,
    domain_presets: params.domainPresets ?? [],
    user_signal: params.userSignal ?? null,
    retrieved_reflection_ids: params.retrievedReflectionIds ?? [],
    context_tokens: params.contextTokens ?? null,
    has_code: params.hasCode ? 1 : 0,
  });

  // Spawn detached so it survives even if the host process exits quickly
  const child = spawn(PYTHON_BIN, [EVOLUTION_CLI, 'log_interaction', payload], {
    detached: false,
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  child.stderr?.on('data', (d: Buffer) => {
    const text = d.toString().trim();
    if (text) logger.warn({ data: text }, 'evolution: log_interaction stderr');
  });
  child.on('error', (err) => {
    logger.error(
      { err },
      'evolution: log_interaction spawn error — interaction not logged',
    );
  });
  // Do not await — fire and forget
}

export interface ReactionSignalParams {
  emoji: string;
  groupFolder: string;
  sessionId?: string;
  reactedToMessageId?: string;
}

/**
 * Convert a channel-received emoji reaction into a userSignal log entry.
 *
 * No-op when the emoji doesn't map to a positive/negative signal. Session
 * lookup happens on the Python side via get_previous_in_session; this call
 * only needs groupFolder + sessionId to attach the signal to the right
 * previous interaction.
 */
export function logReactionSignal(params: ReactionSignalParams): void {
  const signal = emojiToSignal(params.emoji);
  if (signal === null) return;
  logInteraction({
    id: randomUUID(),
    prompt: '[reaction]',
    response: null,
    groupFolder: params.groupFolder,
    sessionId: params.sessionId,
    userSignal: signal,
  });
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
