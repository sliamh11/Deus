import { describe, expect, it } from 'vitest';

import { safeInteractionId } from './safe-interaction-id.js';

describe('safeInteractionId', () => {
  it('passes through filename-safe characters unchanged', () => {
    expect(safeInteractionId('whatsapp_main-1780863677424')).toBe(
      'whatsapp_main-1780863677424',
    );
    expect(safeInteractionId('a.b_c-1')).toBe('a.b_c-1');
  });

  it('replaces every non-[A-Za-z0-9._-] char with underscore', () => {
    expect(safeInteractionId('group/folder@jid:1')).toBe('group_folder_jid_1');
    expect(safeInteractionId('a b\tc')).toBe('a_b_c');
  });

  it('stays byte-identical to the host transform in container-runner.ts', () => {
    // The host (readToolCalls / readAvailableTools) inlines this exact regex.
    // If this assertion ever fails the container and host resolve DIFFERENT
    // files and capture silently drops — keep them in lockstep.
    const hostTransform = (id: string): string =>
      id.replace(/[^A-Za-z0-9._-]/g, '_');
    for (const sample of [
      'whatsapp_main-1780863677424',
      'group/folder@jid:1',
      'telegram:123/abc',
      'a b\tc',
      '',
    ]) {
      expect(safeInteractionId(sample)).toBe(hostTransform(sample));
    }
  });
});
