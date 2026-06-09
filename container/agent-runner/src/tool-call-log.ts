/**
 * Structured per-tool-call capture for evolution tool observability (LIA-154).
 *
 * Mirrors createToolSizeLogHook / tool-audit.ts: a fire-and-forget PostToolUse
 * hook that appends one structured record per tool call to a PER-INTERACTION,
 * host-mounted log `/workspace/group/logs/tool-calls/<interaction_id>.jsonl`.
 * The host reads that one file back at logInteraction time (readToolCalls) and
 * stores it in evolution.db's `tool_calls` column. Per-interaction (not one
 * shared append-only log) bounds each host read to the dispatch and avoids
 * unbounded single-file growth. OBSERVABILITY ONLY — nothing in the live
 * scoring path reads the column yet (activation deferred; see LIA-154).
 *
 * Measurement only: never modifies tool output the model sees, never throws.
 * Opt-out via DEUS_TOOL_CALL_LOG=0.
 */

import fs from 'fs';
import path from 'path';
import type {
  HookCallback,
  PostToolUseHookInput,
} from '@anthropic-ai/claude-agent-sdk';

import { safeInteractionId } from './safe-interaction-id.js';

const LOG_DIR = '/workspace/group/logs/tool-calls';

// Cap free-text args so each JSONL line stays well under PIPE_BUF (4096B) —
// keeps concurrent appendFileSync writes atomic (no torn/interleaved lines) and
// bounds secret/PII exposure from e.g. a Bash command carrying a token.
const MAX_FIELD = 1024;

export interface ToolCallFields {
  name: string;
  file_path?: string;
  command?: string;
  subagent_type?: string;
  is_error: boolean;
}

function cap(value: unknown): string | undefined {
  if (typeof value !== 'string' || value.length === 0) return undefined;
  return value.length > MAX_FIELD ? value.slice(0, MAX_FIELD) : value;
}

function isErrorResponse(toolResponse: unknown): boolean {
  if (toolResponse && typeof toolResponse === 'object') {
    const r = toolResponse as Record<string, unknown>;
    if (typeof r.is_error === 'boolean') return r.is_error;
    if ('error' in r && r.error) return true;
  }
  return false;
}

/**
 * Project a tool call down to the scoring-relevant fields the offline
 * mechanical scorers consume (evolution/judge/mechanical.py): name, plus
 * file_path (Read/Edit/Write), command (Bash), or subagent_type (Agent/Task).
 * Pure + capped — unit-tested independently of the hook/fs.
 */
export function extractToolCallFields(
  name: string,
  toolInput: unknown,
  toolResponse: unknown,
): ToolCallFields {
  const input =
    toolInput && typeof toolInput === 'object'
      ? (toolInput as Record<string, unknown>)
      : {};
  const fields: ToolCallFields = {
    name,
    is_error: isErrorResponse(toolResponse),
  };

  if (
    name === 'Read' ||
    name === 'Edit' ||
    name === 'Write' ||
    name === 'NotebookEdit'
  ) {
    const fp = cap(input.file_path ?? input.notebook_path);
    if (fp !== undefined) fields.file_path = fp;
  } else if (name === 'Bash') {
    const cmd = cap(input.command);
    if (cmd !== undefined) fields.command = cmd;
  } else if (name === 'Agent' || name === 'Task') {
    const st = cap(input.subagent_type);
    if (st !== undefined) fields.subagent_type = st;
  }

  return fields;
}

export function createToolCallLogHook(): HookCallback {
  return async (input): Promise<Record<string, unknown>> => {
    try {
      // Per-dispatch join key, threaded in via the container env (fresh
      // `docker run` per dispatch, LIA-154). The filename encodes it, so no
      // join key is stored in the records themselves.
      const interactionId = process.env.DEUS_INTERACTION_ID;
      if (!interactionId) return {}; // no join key → host can't read it back
      const hookInput = input as PostToolUseHookInput;
      const fields = extractToolCallFields(
        hookInput.tool_name,
        hookInput.tool_input,
        hookInput.tool_response,
      );
      const entry = {
        ts: new Date().toISOString(),
        session_id: hookInput.session_id ?? null,
        tool_use_id: hookInput.tool_use_id,
        ...fields,
      };
      const logPath = path.join(
        LOG_DIR,
        `${safeInteractionId(interactionId)}.jsonl`,
      );
      fs.mkdirSync(LOG_DIR, { recursive: true });
      fs.appendFileSync(logPath, JSON.stringify(entry) + '\n');
    } catch {
      // Capture must never crash a tool call.
    }
    return {};
  };
}
