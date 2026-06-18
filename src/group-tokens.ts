import crypto from 'crypto';

/**
 * Registry pattern: per-group proxy tokens for container authentication.
 * Tokens are process-lifetime (regenerated on restart — same scope as
 * the previous single shared token). O(n) reverse-lookup on validation
 * is acceptable given expected group count (<20).
 */

const tokensByFolder = new Map<string, string>();
const foldersByToken = new Map<string, string>();

// Phase 2 (LIA-315): per-token tool-scope for reduced-privilege webhook runs.
// A publicIngress (webhook) folder's token is restricted to a curated tool set;
// the tool-proxy rejects any tool outside that set. Normal-group tokens are
// never scoped and stay unrestricted.
const tokenScopes = new Map<string, Set<string>>(); // token -> allowed curated tools
const publicIngressFolders = new Set<string>(); // folders that MUST mint via the scoped path

const ANONYMOUS_KEY = '_anonymous';

export function getOrCreateGroupToken(folder?: string): string {
  const key = folder || ANONYMOUS_KEY;
  // Race guard (R2): a publicIngress folder may only ever be minted via the
  // scoped path, so its token always carries a scope. Reject the unscoped path
  // BEFORE the idempotency return — otherwise an unscoped first-mint would
  // silently produce a token that isToolAllowedForToken treats as unrestricted.
  if (publicIngressFolders.has(key)) {
    throw new Error(
      `getOrCreateGroupToken: folder "${key}" is a publicIngress folder — use getOrCreateScopedToken`,
    );
  }
  const existing = tokensByFolder.get(key);
  if (existing) return existing;

  const token = crypto.randomBytes(32).toString('hex');
  tokensByFolder.set(key, token);
  foldersByToken.set(token, key);
  return token;
}

function scopesEqual(a: Set<string>, b: Set<string>): boolean {
  if (a.size !== b.size) return false;
  for (const v of a) if (!b.has(v)) return false;
  return true;
}

/**
 * Mint (or return) a SCOPED token for a publicIngress (webhook) folder.
 * Phase 2 (LIA-315), R2 linchpin. Fail-closed:
 *   - new folder            -> mint, record scope.
 *   - same folder + scope   -> idempotent, return existing token.
 *   - same folder, DIFFERENT scope -> throw (a folder maps to exactly one
 *     source/scope; never silently merge or override its authority).
 *   - existing token with NO scope -> throw (folder was minted unscoped first).
 */
export function getOrCreateScopedToken(
  folder: string,
  scope: Set<string>,
): string {
  publicIngressFolders.add(folder);
  const existing = tokensByFolder.get(folder);
  if (existing) {
    const existingScope = tokenScopes.get(existing);
    if (!existingScope) {
      throw new Error(
        `getOrCreateScopedToken: folder "${folder}" already has an unscoped token`,
      );
    }
    if (!scopesEqual(existingScope, scope)) {
      throw new Error(
        `getOrCreateScopedToken: folder "${folder}" already minted with a different scope`,
      );
    }
    return existing;
  }
  const token = crypto.randomBytes(32).toString('hex');
  tokensByFolder.set(folder, token);
  foldersByToken.set(token, folder);
  tokenScopes.set(token, new Set(scope));
  return token;
}

export function validateGroupToken(token: string): string | null {
  return foldersByToken.get(token) ?? null;
}

/**
 * Whether a token may invoke a given tool. Scoped (publicIngress) tokens are
 * restricted to their curated set; unscoped (normal-group) tokens are
 * unrestricted. Enforced in the tool-proxy (Phase 2, LIA-315). A scoped token
 * can never lack an entry here — publicIngress folders mint only via the
 * scoped path — so "no entry" unambiguously means a normal, unscoped group.
 */
export function isToolAllowedForToken(token: string, rawName: string): boolean {
  const scope = tokenScopes.get(token);
  if (!scope) return true; // unscoped — normal group, unrestricted
  return scope.has(rawName);
}

/** @internal — for testing only */
export function _clearTokens(): void {
  tokensByFolder.clear();
  foldersByToken.clear();
  tokenScopes.clear();
  publicIngressFolders.clear();
}
