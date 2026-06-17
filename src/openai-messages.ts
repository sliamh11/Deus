/**
 * Helpers for the OpenAI chat-completions `messages` shape that the Web UI
 * (Odysseus /v1) replays on every request. Shared by odysseus-server.ts (prompt
 * building) and webui-consolidation.ts (LIA-295) so neither duplicates the
 * multi-part content walker — and so they don't import each other (cycle-free).
 */

/** Flatten an OpenAI message `content` (string or multi-part array) to text. */
export function messageText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    // OpenAI multi-part content: concatenate text parts.
    return content
      .map((p: unknown) =>
        typeof (p as { text?: unknown })?.text === 'string'
          ? (p as { text: string }).text
          : '',
      )
      .join('');
  }
  return '';
}
