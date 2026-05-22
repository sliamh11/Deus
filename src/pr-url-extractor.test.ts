import { describe, expect, it } from 'vitest';
import { extractPrUrl } from './pr-url-extractor.js';

describe('extractPrUrl', () => {
  it('extracts PR URL from middle of text', () => {
    const text =
      'Created PR at https://github.com/owner/repo/pull/42 for the feature.';
    expect(extractPrUrl(text)).toBe('https://github.com/owner/repo/pull/42');
  });

  it('extracts first matching URL when multiple present', () => {
    const text = [
      'See https://github.com/owner/repo/pull/1',
      'and https://github.com/owner/repo/pull/2',
    ].join('\n');
    expect(extractPrUrl(text)).toBe('https://github.com/owner/repo/pull/1');
  });

  it('returns null when no PR URL present', () => {
    expect(extractPrUrl('No PR here.')).toBeNull();
    expect(extractPrUrl('')).toBeNull();
  });

  it('returns null for malformed URL', () => {
    expect(extractPrUrl('https://github.com/owner/repo/issues/5')).toBeNull();
  });

  it('scopes to repoSlug when provided', () => {
    const text =
      'See https://github.com/other/lib/pull/99 and https://github.com/owner/repo/pull/7';
    expect(extractPrUrl(text, 'owner/repo')).toBe(
      'https://github.com/owner/repo/pull/7',
    );
  });

  it('returns null when repoSlug does not match', () => {
    const text = 'PR: https://github.com/other/lib/pull/99';
    expect(extractPrUrl(text, 'owner/repo')).toBeNull();
  });
});
