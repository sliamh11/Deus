/**
 * Model-facing size measurement for the Headroom POC tool-size log (LIA-347).
 *
 * The PostToolUse hook only receives the SDK's structured `tool_response`, which
 * for file-mutation tools embeds full-file snapshots the model NEVER receives
 * (Edit: `originalFile`; Write: `originalFile` + `content`; NotebookEdit:
 * `original_file` + `updated_file`). Naively `JSON.stringify`-ing the whole
 * object over-counts those tools massively (Edit measured ~11.8k tok/call, ~32%
 * of all reported container tool tokens were phantom). This module strips those
 * non-model-facing fields, per tool, before measuring — so `tool-sizes.jsonl`
 * tracks roughly what the model actually consumes. Read/Bash/Grep etc. carry
 * genuinely model-facing content and are left byte-identical to before.
 *
 * Measurement only: this never alters what the model or the SDK sees.
 */

/**
 * Fields present on a tool's `tool_response` that the model does NOT receive,
 * keyed by tool name. Verified against @anthropic-ai/claude-agent-sdk
 * sdk-tools.d.ts (FileEditOutput / FileWriteOutput / NotebookEditOutput) and
 * scoped to the file-mutation tools actually allowed (allowed-tools.ts). Tools
 * absent here are measured unchanged. MultiEdit is intentionally omitted: it has
 * no output type in this SDK and is not an allowed tool.
 */
export const NON_MODEL_FACING_FIELDS: Record<string, readonly string[]> = {
  Edit: ['originalFile'],
  Write: ['originalFile', 'content'],
  NotebookEdit: ['original_file', 'updated_file'],
};

export interface MeasuredResponse {
  /** UTF-8 byte length of the model-facing portion of the tool response. */
  bytes: number;
  /** True when at least one non-model-facing field was excluded. */
  stripped: boolean;
}

/**
 * Measure the model-facing byte size of a tool response. String responses and
 * tools with no denylist entry are measured as-is; file-mutation tools have
 * their full-file fields removed first.
 */
export function measureToolResponse(
  toolName: string,
  toolResponse: unknown,
): MeasuredResponse {
  if (typeof toolResponse === 'string') {
    return { bytes: Buffer.byteLength(toolResponse, 'utf8'), stripped: false };
  }

  const fields = NON_MODEL_FACING_FIELDS[toolName];
  let value: unknown = toolResponse ?? '';
  let stripped = false;

  if (
    fields &&
    value !== null &&
    typeof value === 'object' &&
    !Array.isArray(value)
  ) {
    const copy = { ...(value as Record<string, unknown>) };
    for (const field of fields) {
      if (field in copy) {
        delete copy[field];
        stripped = true;
      }
    }
    value = copy;
  }

  return { bytes: Buffer.byteLength(JSON.stringify(value), 'utf8'), stripped };
}
