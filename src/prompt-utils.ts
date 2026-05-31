/**
 * Escapes a string for safe interpolation inside XML/HTML tags in prompts.
 * Prevents prompt injection via Linear-sourced fields such as comment bodies,
 * author names, and issue descriptions.
 *
 * Escape order matters: `&` must be escaped first to avoid double-escaping.
 */
export function escapeXmlForPrompt(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}
