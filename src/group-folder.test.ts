import path from 'path';

import { describe, expect, it } from 'vitest';

import {
  isValidGroupFolder,
  resolveGroupFolderPath,
  resolveGroupIpcPath,
} from './group-folder.js';

describe('group folder validation', () => {
  it('accepts normal group folder names', () => {
    expect(isValidGroupFolder('main')).toBe(true);
    expect(isValidGroupFolder('family-chat')).toBe(true);
    expect(isValidGroupFolder('Team_42')).toBe(true);
  });

  it('rejects traversal and reserved names', () => {
    expect(isValidGroupFolder('../../etc')).toBe(false);
    expect(isValidGroupFolder('/tmp')).toBe(false);
    expect(isValidGroupFolder('global')).toBe(false);
    expect(isValidGroupFolder('')).toBe(false);
  });

  it('resolves safe paths under groups directory', () => {
    const resolved = resolveGroupFolderPath('family-chat');
    expect(resolved.endsWith(`${path.sep}groups${path.sep}family-chat`)).toBe(
      true,
    );
  });

  it('resolves safe paths under data ipc directory', () => {
    const resolved = resolveGroupIpcPath('family-chat');
    expect(
      resolved.endsWith(`${path.sep}data${path.sep}ipc${path.sep}family-chat`),
    ).toBe(true);
  });

  it('throws for unsafe folder names', () => {
    expect(() => resolveGroupFolderPath('../../etc')).toThrow();
    expect(() => resolveGroupIpcPath('/tmp')).toThrow();
  });
});

// Per-run IPC namespace key (LIA-211): concurrent Linear dispatches share one
// groupFolder but each has a unique chatJid; without a per-run subdir their
// `_close` sentinels collide. These pin the path-derivation that both the
// writer (executeAgentRun eventSink) and the mounter (buildVolumeMounts) share.
describe('resolveGroupIpcPath per-run key (LIA-211)', () => {
  const ipcSuffix = (folder: string, key?: string) =>
    [`data`, `ipc`, folder, ...(key ? [key] : [])].join(path.sep);

  it('appends a per-run subdir when runKey is provided', () => {
    const resolved = resolveGroupIpcPath(
      'linear-dispatch',
      'linear-dispatch-abc12345',
    );
    expect(
      resolved.endsWith(
        `${path.sep}${ipcSuffix('linear-dispatch', 'linear-dispatch-abc12345')}`,
      ),
    ).toBe(true);
  });

  it('two different runKeys resolve to two different dirs (the core regression)', () => {
    const a = resolveGroupIpcPath(
      'linear-dispatch',
      'linear-dispatch-aaaa1111',
    );
    const b = resolveGroupIpcPath(
      'linear-dispatch',
      'linear-dispatch-bbbb2222',
    );
    expect(a).not.toBe(b);
    // ...and both still live under the same shared folder.
    expect(path.dirname(a)).toBe(path.dirname(b));
  });

  it('no runKey is byte-identical to the legacy folder path (opt-in, non-breaking)', () => {
    const withUndefined = resolveGroupIpcPath('family-chat', undefined);
    const legacy = resolveGroupIpcPath('family-chat');
    expect(withUndefined).toBe(legacy);
    expect(
      withUndefined.endsWith(`${path.sep}${ipcSuffix('family-chat')}`),
    ).toBe(true);
  });

  it('sanitizes separators and dots so traversal cannot survive the key', () => {
    // '/', '\\' and '.' are all outside the [A-Za-z0-9_-] allowlist → '_'.
    for (const [raw, safe] of [
      ['..', '__'],
      ['../x', '___x'],
      ['a/b', 'a_b'],
      ['a\\b', 'a_b'],
    ] as const) {
      const resolved = resolveGroupIpcPath('main', raw);
      expect(resolved.endsWith(`${path.sep}${ipcSuffix('main', safe)}`)).toBe(
        true,
      );
      // The sanitized key has no separator, so the path stays within base.
      expect(() => resolveGroupIpcPath('main', raw)).not.toThrow();
    }
  });
});
