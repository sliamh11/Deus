/**
 * Oracle tests for Ingress Gateway Phase 2 — credential isolation primitives.
 * Authored from the spec BEFORE implementation exists (oracle-author warden).
 * These tests are RED against origin/main and must go GREEN once the
 * implementer adds:
 *   - getOrCreateScopedToken
 *   - isToolAllowedForToken
 *   - publicIngressFolders guard on getOrCreateGroupToken
 *   - _clearTokens extension clearing tokenScopes + publicIngressFolders
 *
 * Every test is tagged @oracle so the oracle-integrity gate can protect it.
 *
 * TEST-SEAM REQUIREMENTS imposed on the implementer:
 *   (none for this file — all new exports are public API per the spec)
 */

import { describe, it, expect, beforeEach } from 'vitest';
import {
  getOrCreateGroupToken,
  getOrCreateScopedToken,
  isToolAllowedForToken,
  validateGroupToken,
  _clearTokens,
} from './group-tokens.js';

describe('group-tokens Phase 2 — oracle (c): scoped token enforcement', () => {
  // @oracle: isToolAllowedForToken must use Set membership for scoped tokens
  beforeEach(() => _clearTokens());

  it('isToolAllowedForToken returns true for a tool IN the scope set', () => {
    // @oracle: scoped token enforcement — tool in curated set is allowed
    const curatedSet = new Set(['WebSearch', 'Read', 'Glob']);
    const token = getOrCreateScopedToken('folder-curated', curatedSet);
    expect(isToolAllowedForToken(token, 'WebSearch')).toBe(true);
    expect(isToolAllowedForToken(token, 'Read')).toBe(true);
    expect(isToolAllowedForToken(token, 'Glob')).toBe(true);
  });

  it('isToolAllowedForToken returns false for a tool NOT in the scope set', () => {
    // @oracle: scoped token enforcement — tool outside curated set is rejected (R2)
    const curatedSet = new Set(['WebSearch', 'Read', 'Glob']);
    const token = getOrCreateScopedToken('folder-curated-b', curatedSet);
    // Bash is in the global allowlist but NOT in the curated set
    expect(isToolAllowedForToken(token, 'Bash')).toBe(false);
    // mcp__deus__memory is a deus tool, NOT in the curated set
    expect(isToolAllowedForToken(token, 'mcp__deus__memory')).toBe(false);
  });

  it('isToolAllowedForToken returns true for ALL tools on an unscoped (normal-group) token', () => {
    // @oracle: unscoped tokens are unrestricted — normal groups unaffected
    const token = getOrCreateGroupToken('normal-folder');
    expect(isToolAllowedForToken(token, 'Bash')).toBe(true);
    expect(isToolAllowedForToken(token, 'Write')).toBe(true);
    expect(isToolAllowedForToken(token, 'mcp__deus__memory')).toBe(true);
  });

  it('per-source isolation: token A is rejected for token B curated tool (different folders)', () => {
    // @oracle: per-source isolation — token A cannot access token B scope
    // Two distinct folders; each minted with disjoint scopes.
    const scopeA = new Set(['WebSearch']);
    const scopeB = new Set(['Glob']);
    const tokenA = getOrCreateScopedToken('source-folder-A', scopeA);
    const tokenB = getOrCreateScopedToken('source-folder-B', scopeB);

    // Token A: allowed for 'WebSearch', rejected for 'Glob' (token B's tool)
    expect(isToolAllowedForToken(tokenA, 'WebSearch')).toBe(true);
    expect(isToolAllowedForToken(tokenA, 'Glob')).toBe(false);

    // Token B: allowed for 'Glob', rejected for 'WebSearch' (token A's tool)
    expect(isToolAllowedForToken(tokenB, 'Glob')).toBe(true);
    expect(isToolAllowedForToken(tokenB, 'WebSearch')).toBe(false);
  });
});

describe('group-tokens Phase 2 — oracle (d): race and conflict guards', () => {
  // @oracle: fail-closed mint namespace — race and conflict guards
  beforeEach(() => _clearTokens());

  it('getOrCreateGroupToken THROWS for a folder already registered as publicIngress', () => {
    // @oracle: publicIngress folder must go through scoped path only — race guard
    const scope = new Set(['Read']);
    getOrCreateScopedToken('ingress-folder', scope);
    // The folder is now registered in publicIngressFolders; the unscoped path must throw
    expect(() => getOrCreateGroupToken('ingress-folder')).toThrow();
  });

  it('getOrCreateScopedToken THROWS when called twice with DIFFERENT scopes (scope conflict)', () => {
    // @oracle: scope conflict — a folder maps to exactly one source/scope
    const scopeX = new Set(['WebSearch']);
    const scopeY = new Set(['Glob']); // different scope
    getOrCreateScopedToken('conflict-folder', scopeX);
    expect(() => getOrCreateScopedToken('conflict-folder', scopeY)).toThrow();
  });

  it('getOrCreateScopedToken is IDEMPOTENT — same scope returns the SAME token', () => {
    // @oracle: same scope re-registers safely and returns the identical token
    const scope = new Set(['WebSearch', 'Read']);
    const t1 = getOrCreateScopedToken('idempotent-folder', scope);
    const t2 = getOrCreateScopedToken(
      'idempotent-folder',
      new Set(['WebSearch', 'Read']),
    );
    expect(t1).toBe(t2);
  });

  it('_clearTokens also clears publicIngress folder registry and token scopes', () => {
    // @oracle: _clearTokens must clear the new state for proper test isolation
    const scope = new Set(['Read']);
    const token = getOrCreateScopedToken('cleanup-folder', scope);
    // Confirm the state was set
    expect(isToolAllowedForToken(token, 'Read')).toBe(true);
    expect(isToolAllowedForToken(token, 'Bash')).toBe(false);

    _clearTokens();

    // After clear, getOrCreateGroupToken on the same folder must NOT throw
    // (folder is no longer registered as publicIngress)
    expect(() => getOrCreateGroupToken('cleanup-folder')).not.toThrow();

    // After clear, the old token is no longer in foldersByToken, but the
    // critical property is that the scope entry is also gone — a fresh
    // scoped token for the same folder should work, and the OLD token
    // should now be treated as unscoped (no scope entry → returns true).
    expect(isToolAllowedForToken(token, 'Bash')).toBe(true);
  });

  it('getOrCreateScopedToken mints a valid 64-char hex token', () => {
    // @oracle: scoped tokens follow the same format as group tokens
    const scope = new Set(['Read']);
    const token = getOrCreateScopedToken('hex-folder', scope);
    expect(token).toMatch(/^[0-9a-f]{64}$/);
  });

  it('validateGroupToken can resolve a scoped token back to its folder', () => {
    // @oracle: scoped tokens are still registered in the reverse-lookup map
    const scope = new Set(['Read']);
    const token = getOrCreateScopedToken('resolve-folder', scope);
    expect(validateGroupToken(token)).toBe('resolve-folder');
  });
});
