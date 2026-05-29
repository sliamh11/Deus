/**
 * IPC Protocol — single source of truth for host-side IPC schemas.
 *
 * Zod schemas for ContainerOutput and ContainerInput are defined here so that
 * all JSON.parse() calls on container output can be validated at runtime.
 * TypeScript types are derived from the schemas to ensure the type system and
 * runtime validation stay in sync.
 *
 * NOTE: The container-side mirror in container/agent-runner/src/index.ts
 * cannot import from here (cross-container boundary). Keep that file's
 * interfaces manually synced and update its SYNC-REQUIRED comment to reference
 * this file.
 */

import { z } from 'zod';

// ── IPC marker constants ────────────────────────────────────────────────────
// Previously defined in container-runner.ts with a SYNC-REQUIRED comment.
// Canonical definition lives here; container/agent-runner/src/index.ts
// still holds its own copy (cross-boundary, cannot import from host).
export const OUTPUT_START_MARKER = '---DEUS_OUTPUT_START---';
export const OUTPUT_END_MARKER = '---DEUS_OUTPUT_END---';

// ── Sub-schemas ─────────────────────────────────────────────────────────────

export const RuntimeSessionSchema = z.object({
  backend: z.enum(['claude', 'openai', 'llama-cpp']),
  session_id: z.string(),
  resume_cursor: z.string().optional(),
  metadata_json: z.string().optional(),
});

export const ContextStatsSchema = z.object({
  tokens: z.number(),
  limit: z.number(),
  pct: z.number(),
  warn: z.boolean().optional(),
  autoCompact: z.boolean().optional(),
});

export const CompactionEventSchema = z.object({
  trigger: z.enum(['manual', 'auto']),
  preTokens: z.number().optional(),
  summary: z.string().optional(),
});

// ── ContainerOutput ─────────────────────────────────────────────────────────

export const ContainerOutputSchema = z.object({
  status: z.enum(['success', 'error']),
  result: z.string().nullable(),
  newSessionRef: RuntimeSessionSchema.optional(),
  newSessionId: z.string().optional(),
  error: z.string().optional(),
  prUrl: z.string().optional(),
  contextStats: ContextStatsSchema.optional(),
  compactionEvent: CompactionEventSchema.optional(),
});

/** Derived TypeScript type — single source of truth. */
export type ContainerOutput = z.infer<typeof ContainerOutputSchema>;
export type ContextStats = z.infer<typeof ContextStatsSchema>;
export type CompactionEvent = z.infer<typeof CompactionEventSchema>;

// ── ContainerInput ──────────────────────────────────────────────────────────

export const ContainerInputSchema = z.object({
  prompt: z.string(),
  backend: z.enum(['claude', 'openai', 'llama-cpp']).optional(),
  sessionId: z.string().optional(),
  sessionRef: RuntimeSessionSchema.optional(),
  groupFolder: z.string(),
  chatJid: z.string(),
  isControlGroup: z.boolean(),
  isScheduledTask: z.boolean().optional(),
  assistantName: z.string().optional(),
  imageAttachments: z
    .array(z.object({ relativePath: z.string(), mediaType: z.string() }))
    .optional(),
  projectHint: z.string().optional(),
  effort: z.enum(['low', 'medium', 'high', 'max']).optional(),
  worktreePath: z.string().optional(),
});

/** Derived TypeScript type — single source of truth. */
export type ContainerInput = z.infer<typeof ContainerInputSchema>;

// ── IPC message file schema ─────────────────────────────────────────────────
// Containers write JSON files to /data/ipc/<group>/messages/.
// Each file contains one of these objects.

export const IpcMessageFileSchema = z.object({
  type: z.string(),
  chatJid: z.string().optional(),
  text: z.string().optional(),
});

export type IpcMessageFile = z.infer<typeof IpcMessageFileSchema>;
