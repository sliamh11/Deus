import { describe, it, expect } from 'vitest';
import { DisconnectReason } from '@whiskeysockets/baileys';

import { classifyDisconnect } from './whatsapp.js';

/**
 * classifyDisconnect decides whether a connection-close should reconnect, and
 * distinguishes the two non-reconnect cases so an intentional teardown is not
 * mislabeled as a real logout (the bug: every graceful restart logged
 * "Logged out. Re-authenticate to continue.").
 */
describe('classifyDisconnect', () => {
  it('treats an intentional teardown as intentional-shutdown (never reconnect)', () => {
    expect(classifyDisconnect(undefined, true)).toBe('intentional-shutdown');
    expect(classifyDisconnect(408, true)).toBe('intentional-shutdown');
    // Intentional takes precedence even if the close also carried loggedOut.
    expect(classifyDisconnect(DisconnectReason.loggedOut, true)).toBe(
      'intentional-shutdown',
    );
  });

  it('treats a real, non-intentional loggedOut as logged-out (needs re-auth)', () => {
    expect(classifyDisconnect(DisconnectReason.loggedOut, false)).toBe(
      'logged-out',
    );
  });

  it('reconnects on any other non-intentional close', () => {
    expect(classifyDisconnect(undefined, false)).toBe('reconnect');
    expect(classifyDisconnect(408, false)).toBe('reconnect'); // timed out
    expect(classifyDisconnect(440, false)).toBe('reconnect'); // connection replaced
    expect(classifyDisconnect(DisconnectReason.restartRequired, false)).toBe(
      'reconnect',
    );
  });
});
