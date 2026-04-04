import { describe, it, expect } from 'vitest';
import path from 'path';

import { STORE_DIR, GROUPS_DIR, DATA_DIR } from './config.js';

describe('config paths', () => {
  it('STORE_DIR is an absolute path ending with store', () => {
    expect(path.isAbsolute(STORE_DIR)).toBe(true);
    expect(STORE_DIR).toMatch(/store$/);
  });

  it('GROUPS_DIR is an absolute path ending with groups', () => {
    expect(path.isAbsolute(GROUPS_DIR)).toBe(true);
    expect(GROUPS_DIR).toMatch(/groups$/);
  });

  it('DATA_DIR is an absolute path ending with data', () => {
    expect(path.isAbsolute(DATA_DIR)).toBe(true);
    expect(DATA_DIR).toMatch(/data$/);
  });

  it('PROJECT_ROOT uses path.resolve so paths are normalized', () => {
    // STORE_DIR = path.resolve(PROJECT_ROOT, 'store')
    // If PROJECT_ROOT were not normalized, paths could contain duplicated segments.
    // Verify no segment duplication (e.g. "DeusDeusstore" pattern).
    const segments = STORE_DIR.split(path.sep);
    // Check that 'store' appears only as the last segment, not fused into another
    expect(segments[segments.length - 1]).toBe('store');
  });
});
