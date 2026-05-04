import crypto from 'crypto';

/**
 * Registry pattern: per-group proxy tokens for container authentication.
 * Tokens are process-lifetime (regenerated on restart — same scope as
 * the previous single shared token). O(n) reverse-lookup on validation
 * is acceptable given expected group count (<20).
 */

const tokensByFolder = new Map<string, string>();
const foldersByToken = new Map<string, string>();

const ANONYMOUS_KEY = '_anonymous';

export function getOrCreateGroupToken(folder?: string): string {
  const key = folder || ANONYMOUS_KEY;
  const existing = tokensByFolder.get(key);
  if (existing) return existing;

  const token = crypto.randomBytes(32).toString('hex');
  tokensByFolder.set(key, token);
  foldersByToken.set(token, key);
  return token;
}

export function validateGroupToken(token: string): string | null {
  return foldersByToken.get(token) ?? null;
}

/** @internal — for testing only */
export function _clearTokens(): void {
  tokensByFolder.clear();
  foldersByToken.clear();
}
