import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import path from 'path';

import {
  STORE_DIR,
  GROUPS_DIR,
  DATA_DIR,
  deusInstanceId,
  resetDeusInstanceIdCache,
} from './config.js';

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

describe('deusInstanceId (LIA-491)', () => {
  const original = process.env.DEUS_INSTANCE_ID;

  beforeEach(() => {
    resetDeusInstanceIdCache();
    delete process.env.DEUS_INSTANCE_ID;
  });

  afterEach(() => {
    resetDeusInstanceIdCache();
    if (original === undefined) delete process.env.DEUS_INSTANCE_ID;
    else process.env.DEUS_INSTANCE_ID = original;
  });

  it('defaults to a stable 8-hex digest when no override is set', () => {
    const first = deusInstanceId();
    expect(first).toMatch(/^[0-9a-f]{8}$/);
    // Memoised: repeated calls agree.
    expect(deusInstanceId()).toBe(first);
    // And stable across a cache reset, since it derives from PROJECT_ROOT.
    resetDeusInstanceIdCache();
    expect(deusInstanceId()).toBe(first);
  });

  it('lets DEUS_INSTANCE_ID override the default', () => {
    const fallback = deusInstanceId();
    resetDeusInstanceIdCache();
    process.env.DEUS_INSTANCE_ID = 'sandbox';
    const overridden = deusInstanceId();
    expect(overridden).toMatch(/^[0-9a-f]{8}$/);
    expect(overridden).not.toBe(fallback);
  });

  it('hashes the override rather than using it verbatim', () => {
    // A verbatim value would be illegal in a container name and could inject
    // regex metacharacters into the ownership matcher.
    process.env.DEUS_INSTANCE_ID = 'Not/A*Valid.Name$^[]';
    expect(deusInstanceId()).toMatch(/^[0-9a-f]{8}$/);
  });

  it('is deterministic for the same override', () => {
    process.env.DEUS_INSTANCE_ID = 'sandbox';
    const a = deusInstanceId();
    resetDeusInstanceIdCache();
    const b = deusInstanceId();
    expect(a).toBe(b);
  });

  it('distinguishes two different overrides', () => {
    process.env.DEUS_INSTANCE_ID = 'sandbox-a';
    const a = deusInstanceId();
    resetDeusInstanceIdCache();
    process.env.DEUS_INSTANCE_ID = 'sandbox-b';
    expect(deusInstanceId()).not.toBe(a);
  });
});
