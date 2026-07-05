/**
 * Reactive oversize-Read nudge (LIA-379).
 *
 * When a Read response crosses a byte threshold, appends a one-line advisory
 * via `additionalContext` steering FOLLOW-UP reads toward offset/limit or
 * Grep. It never rewrites or truncates the Read result itself —
 * `additionalContext` is append-only, and it is the only channel the SDK
 * honors for built-in tools (`updatedMCPToolOutput` applies to mcp__* tools
 * exclusively, verified against sdk.d.ts PostToolUseHookSpecificOutput).
 *
 * Rate-limited so it advises rather than nags: once per distinct file per
 * turn, capped at a per-turn total. The tracker is constructed once per
 * runQuery() call (same lifecycle as DoomLoopDetector), so limits reset each
 * turn. The per-path dedup deliberately does NOT re-fire when the model
 * paginates the same file with offset/limit — that is the compliant behavior
 * being encouraged.
 */

import type {
  HookCallback,
  PostToolUseHookInput,
} from '@anthropic-ai/claude-agent-sdk';
import { measureToolResponse } from './tool-size-measure.js';

const DEFAULT_THRESHOLD_BYTES = 20_000;
const DEFAULT_MAX_NUDGES_PER_TURN = 3;

/** Byte threshold above which a Read result triggers the advisory. */
export function readOversizeThresholdBytes(): number {
  const parsed = Number.parseInt(
    process.env.DEUS_READ_OVERSIZE_THRESHOLD_BYTES || '', // LIA-379
    10,
  );
  return Number.isFinite(parsed) && parsed > 0
    ? parsed
    : DEFAULT_THRESHOLD_BYTES;
}

/** Per-turn cap on how many oversize advisories may fire. */
export function readOversizeMaxNudges(): number {
  const parsed = Number.parseInt(
    process.env.DEUS_READ_OVERSIZE_NUDGE_MAX || '', // LIA-379
    10,
  );
  return Number.isFinite(parsed) && parsed > 0
    ? parsed
    : DEFAULT_MAX_NUDGES_PER_TURN;
}

/**
 * The one-line advisory appended after an oversize Read. Own cost: ~40 tokens
 * per fire, ≤3 fires/turn by default (~120 tokens worst case) — vs the ≥20KB
 * (~5k+ tokens) reads it targets, each re-billed every later turn.
 */
export const READ_OVERSIZE_NUDGE =
  'This Read returned a large chunk of the file. If a follow-up read only ' +
  'needs part of it, prefer Read with offset/limit or Grep — a full re-read ' +
  're-bills the whole content again.';

/**
 * Per-turn rate-limit state: at most one nudge per distinct key (file path),
 * capped at `maxNudges` total. `Set` over a count-map because dedup is a pure
 * membership check — one bit of state per key is all the rule needs.
 */
export class ReadOversizeNudgeTracker {
  private readonly nudgedKeys = new Set<string>();
  private nudgeCount = 0;

  constructor(
    private readonly maxNudges: number = DEFAULT_MAX_NUDGES_PER_TURN,
  ) {}

  /** True exactly once per key, and never after the per-turn cap is spent. */
  shouldNudge(key: string): boolean {
    if (this.nudgeCount >= this.maxNudges) return false;
    if (this.nudgedKeys.has(key)) return false;
    this.nudgedKeys.add(key);
    this.nudgeCount += 1;
    return true;
  }
}

function extractFilePath(toolInput: unknown): string | undefined {
  const input = toolInput as Record<string, unknown> | undefined;
  return typeof input?.file_path === 'string' ? input.file_path : undefined;
}

/**
 * PostToolUse hook: fires only for Read results at/above the threshold, at
 * most once per file per turn. Returns `{}` in every other case.
 */
export function createReadOversizeNudgeHook(
  tracker: ReadOversizeNudgeTracker,
  thresholdBytes: number = readOversizeThresholdBytes(),
): HookCallback {
  return async (input) => {
    const hookInput = input as PostToolUseHookInput;
    if (hookInput.tool_name !== 'Read') return {};

    const { bytes } = measureToolResponse('Read', hookInput.tool_response);
    if (bytes < thresholdBytes) return {};

    const key =
      extractFilePath(hookInput.tool_input) ?? hookInput.tool_use_id ?? '';
    if (!tracker.shouldNudge(key)) return {};

    return {
      hookSpecificOutput: {
        hookEventName: 'PostToolUse' as const,
        additionalContext: READ_OVERSIZE_NUDGE,
      },
    };
  };
}
