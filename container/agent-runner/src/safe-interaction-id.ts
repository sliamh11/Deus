/**
 * Filename-safe interaction id, shared by every in-container capture log
 * (tool-call-log.ts, available-tools-log.ts) and MUST stay byte-identical to
 * the host's transform in `container-runner.ts` (readToolCalls /
 * readAvailableTools) so both sides resolve the same per-interaction file
 * (LIA-154). Single source of truth — do not re-implement.
 */
export function safeInteractionId(id: string): string {
  return id.replace(/[^A-Za-z0-9._-]/g, '_');
}
