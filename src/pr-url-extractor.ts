/**
 * Extract a GitHub PR URL from text, scoped to a specific repository.
 *
 * SYNC-REQUIRED: The regex pattern is duplicated in
 * container/agent-runner/src/index.ts (container can't import from src/).
 */

const PR_URL_RE = /https:\/\/github\.com\/([^/]+\/[^/]+)\/pull\/(\d+)/g;

export function extractPrUrl(text: string, repoSlug?: string): string | null {
  for (const match of text.matchAll(PR_URL_RE)) {
    if (!repoSlug || match[1] === repoSlug) {
      return match[0];
    }
  }
  return null;
}
