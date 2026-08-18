import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { logger } from './logger.js';
import {
  isSenderAllowed,
  isTriggerAllowed,
  loadSenderAllowlist,
  SenderAllowlistConfig,
  shouldDropMessage,
} from './sender-allowlist.js';

let tmpDir: string;

function cfgPath(name = 'sender-allowlist.json'): string {
  return path.join(tmpDir, name);
}

function writeConfig(config: unknown, name?: string): string {
  const p = cfgPath(name);
  fs.writeFileSync(p, JSON.stringify(config));
  return p;
}

beforeEach(() => {
  tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'allowlist-test-'));
});

afterEach(() => {
  // Total spy isolation: without this, an assertion throwing before a manual
  // mockRestore() would leak the spy into later spy-based tests and inflate
  // their call counts, turning one failure into a confusing cascade.
  vi.restoreAllMocks();
  fs.rmSync(tmpDir, { recursive: true, force: true });
});

describe('loadSenderAllowlist', () => {
  it('returns allow-all defaults when file is missing', () => {
    const cfg = loadSenderAllowlist(cfgPath());
    expect(cfg.default.allow).toBe('*');
    expect(cfg.default.mode).toBe('trigger');
    expect(cfg.logDenied).toBe(true);
  });

  it('loads allow=* config', () => {
    const p = writeConfig({
      default: { allow: '*', mode: 'trigger' },
      chats: {},
      logDenied: false,
    });
    const cfg = loadSenderAllowlist(p);
    expect(cfg.default.allow).toBe('*');
    expect(cfg.logDenied).toBe(false);
  });

  it('loads allow=[] (deny all)', () => {
    const p = writeConfig({
      default: { allow: [], mode: 'trigger' },
      chats: {},
    });
    const cfg = loadSenderAllowlist(p);
    expect(cfg.default.allow).toEqual([]);
  });

  it('loads allow=[list]', () => {
    const p = writeConfig({
      default: { allow: ['alice', 'bob'], mode: 'drop' },
      chats: {},
    });
    const cfg = loadSenderAllowlist(p);
    expect(cfg.default.allow).toEqual(['alice', 'bob']);
    expect(cfg.default.mode).toBe('drop');
  });

  it('per-chat override beats default', () => {
    const p = writeConfig({
      default: { allow: '*', mode: 'trigger' },
      chats: { 'group-a': { allow: ['alice'], mode: 'drop' } },
    });
    const cfg = loadSenderAllowlist(p);
    expect(cfg.chats['group-a'].allow).toEqual(['alice']);
    expect(cfg.chats['group-a'].mode).toBe('drop');
  });

  // A config file that EXISTS but cannot be trusted must deny, not widen to
  // allow-all. These four cases previously asserted the opposite (fail-open).
  it('denies all senders on invalid JSON', () => {
    const p = cfgPath();
    fs.writeFileSync(p, '{ not valid json }}}');
    const cfg = loadSenderAllowlist(p);
    expect(cfg.default.allow).toEqual([]);
    expect(isSenderAllowed('any-chat', 'alice', cfg)).toBe(false);
  });

  it('denies all senders on invalid schema', () => {
    const p = writeConfig({ default: { oops: true } });
    const cfg = loadSenderAllowlist(p);
    expect(cfg.default.allow).toEqual([]);
    expect(isSenderAllowed('any-chat', 'alice', cfg)).toBe(false);
  });

  it('denies all senders when allow array items are not strings', () => {
    const p = writeConfig({
      default: { allow: [123, null, true], mode: 'trigger' },
      chats: {},
    });
    const cfg = loadSenderAllowlist(p);
    expect(cfg.default.allow).toEqual([]);
    expect(isSenderAllowed('any-chat', 'alice', cfg)).toBe(false);
  });

  // `null` parses successfully, so without a root guard the loader would read
  // `.default` off null and THROW on the per-message path — a crash, not just a
  // fail-open.
  it.each(['null', '[]', '42', '"a string"'])(
    'denies (and does not throw) when the config root is %s',
    (raw) => {
      const p = cfgPath();
      fs.writeFileSync(p, raw);
      const cfg = loadSenderAllowlist(p);
      expect(cfg.default.allow).toEqual([]);
      expect(isSenderAllowed('any-chat', 'alice', cfg)).toBe(false);
    },
  );

  // A malformed chats container must not silently collapse to an empty map —
  // that would delete the operator's per-chat restrictions and fall through to
  // the permissive default.
  it('denies when chats is present but not an object', () => {
    const p = writeConfig({
      default: { allow: '*', mode: 'trigger' },
      chats: 'oops',
    });
    const cfg = loadSenderAllowlist(p);
    expect(cfg.default.allow).toEqual([]);
    expect(isSenderAllowed('some-chat', 'alice', cfg)).toBe(false);
  });

  // Guards the "log suppression, NOT a config cache" property. Without this,
  // adding a mount-security-style process-lifetime cache — exactly what the
  // comment in sender-allowlist.ts warns against — passes every other test.
  // The loader must stay uncached so editing the file takes effect on the very
  // next message, with no restart.
  it('re-reads the file on every call rather than caching a good config', () => {
    const p = writeConfig({ default: { allow: ['alice'], mode: 'trigger' } });
    expect(isSenderAllowed('c', 'alice', loadSenderAllowlist(p))).toBe(true);
    expect(isSenderAllowed('c', 'bob', loadSenderAllowlist(p))).toBe(false);

    // Operator edits the file: the change must be picked up immediately.
    writeConfig({ default: { allow: ['bob'], mode: 'trigger' } });
    expect(isSenderAllowed('c', 'bob', loadSenderAllowlist(p))).toBe(true);
    expect(isSenderAllowed('c', 'alice', loadSenderAllowlist(p))).toBe(false);
  });

  // The other direction of the same property: a corrupt file self-heals once
  // fixed, without a restart.
  it('recovers from a corrupt config as soon as it is repaired', () => {
    const p = cfgPath();
    fs.writeFileSync(p, '{ broken beyond repair');
    expect(loadSenderAllowlist(p).default.allow).toEqual([]);

    writeConfig({ default: { allow: '*', mode: 'trigger' } });
    expect(loadSenderAllowlist(p).default.allow).toBe('*');
  });

  it('accepts a config with no chats key at all', () => {
    const p = writeConfig({ default: { allow: ['alice'], mode: 'trigger' } });
    const cfg = loadSenderAllowlist(p);
    expect(cfg.chats).toEqual({});
    expect(isSenderAllowed('some-chat', 'alice', cfg)).toBe(true);
    expect(isSenderAllowed('some-chat', 'mallory', cfg)).toBe(false);
  });

  it('denies an unreadable (non-ENOENT) config instead of allowing all', () => {
    // A directory where a file is expected reproduces a non-ENOENT read error
    // (EISDIR) without depending on chmod semantics, which vary by platform and
    // do not apply when running as root.
    const p = cfgPath();
    fs.mkdirSync(p);
    const cfg = loadSenderAllowlist(p);
    expect(cfg.default.allow).toEqual([]);
    expect(isSenderAllowed('any-chat', 'alice', cfg)).toBe(false);
  });

  // The deny-all fallback must never use 'drop' mode: shouldDropMessage() true
  // routes to a branch in src/index.ts that returns BEFORE storing the message,
  // so a deny-all in drop mode would silently destroy inbound messages.
  it('deny-all fallback uses trigger mode so no message is ever dropped', () => {
    const p = cfgPath();
    fs.writeFileSync(p, 'not json at all');
    const cfg = loadSenderAllowlist(p);
    expect(cfg.default.mode).toBe('trigger');
    expect(shouldDropMessage('any-chat', cfg)).toBe(false);
  });

  it('denies an invalid per-chat entry rather than dropping it', () => {
    const p = writeConfig({
      default: { allow: '*', mode: 'trigger' },
      chats: {
        good: { allow: ['alice'], mode: 'trigger' },
        bad: { allow: 123 },
      },
    });
    const cfg = loadSenderAllowlist(p);
    expect(cfg.chats['good']).toBeDefined();
    // Previously deleted, which let getEntry() fall through to the permissive
    // default and WIDEN access past what the operator wrote for this chat.
    expect(cfg.chats['bad']).toEqual({ allow: [], mode: 'trigger' });
    expect(isSenderAllowed('bad', 'alice', cfg)).toBe(false);
    expect(isSenderAllowed('good', 'alice', cfg)).toBe(true);
  });

  it('returns a copy so callers cannot mutate shared module state', () => {
    const p = cfgPath();
    fs.writeFileSync(p, 'still not json');
    const first = loadSenderAllowlist(p);
    (first.default.allow as string[]).push('injected');
    const second = loadSenderAllowlist(p);
    expect(second.default.allow).toEqual([]);
  });

  it('reports a broken config once per distinct file state, not per read', () => {
    const p = cfgPath();
    fs.writeFileSync(p, '{ broken');
    const errSpy = vi.spyOn(logger, 'error').mockImplementation(() => logger);

    loadSenderAllowlist(p);
    loadSenderAllowlist(p);
    loadSenderAllowlist(p);
    expect(errSpy).toHaveBeenCalledTimes(1);

    // A genuinely different broken state must be reported again.
    fs.writeFileSync(p, '{ broken differently');
    loadSenderAllowlist(p);
    expect(errSpy).toHaveBeenCalledTimes(2);

    errSpy.mockRestore();
  });

  it('dedupes an unreadable config on its error code', () => {
    const p = cfgPath();
    fs.mkdirSync(p); // EISDIR on every read
    const errSpy = vi.spyOn(logger, 'error').mockImplementation(() => logger);

    loadSenderAllowlist(p);
    loadSenderAllowlist(p);
    expect(errSpy).toHaveBeenCalledTimes(1);

    errSpy.mockRestore();
  });

  // A file that goes missing resets the suppression, so the SAME broken content
  // reappearing later is reported again rather than silently swallowed.
  it('re-reports identical broken content after the file disappears', () => {
    const p = cfgPath();
    const broken = '{ vanishing act';
    fs.writeFileSync(p, broken);
    const errSpy = vi.spyOn(logger, 'error').mockImplementation(() => logger);

    loadSenderAllowlist(p);
    expect(errSpy).toHaveBeenCalledTimes(1);

    fs.rmSync(p);
    const gone = loadSenderAllowlist(p);
    expect(gone.default.allow).toBe('*'); // absent == feature off

    fs.writeFileSync(p, broken);
    loadSenderAllowlist(p);
    expect(errSpy).toHaveBeenCalledTimes(2);

    errSpy.mockRestore();
  });
});

describe('isSenderAllowed', () => {
  it('allow=* allows any sender', () => {
    const cfg: SenderAllowlistConfig = {
      default: { allow: '*', mode: 'trigger' },
      chats: {},
      logDenied: true,
    };
    expect(isSenderAllowed('g1', 'anyone', cfg)).toBe(true);
  });

  it('allow=[] denies any sender', () => {
    const cfg: SenderAllowlistConfig = {
      default: { allow: [], mode: 'trigger' },
      chats: {},
      logDenied: true,
    };
    expect(isSenderAllowed('g1', 'anyone', cfg)).toBe(false);
  });

  it('allow=[list] allows exact match only', () => {
    const cfg: SenderAllowlistConfig = {
      default: { allow: ['alice', 'bob'], mode: 'trigger' },
      chats: {},
      logDenied: true,
    };
    expect(isSenderAllowed('g1', 'alice', cfg)).toBe(true);
    expect(isSenderAllowed('g1', 'eve', cfg)).toBe(false);
  });

  it('uses per-chat entry over default', () => {
    const cfg: SenderAllowlistConfig = {
      default: { allow: '*', mode: 'trigger' },
      chats: { g1: { allow: ['alice'], mode: 'trigger' } },
      logDenied: true,
    };
    expect(isSenderAllowed('g1', 'bob', cfg)).toBe(false);
    expect(isSenderAllowed('g2', 'bob', cfg)).toBe(true);
  });
});

describe('shouldDropMessage', () => {
  it('returns false for trigger mode', () => {
    const cfg: SenderAllowlistConfig = {
      default: { allow: '*', mode: 'trigger' },
      chats: {},
      logDenied: true,
    };
    expect(shouldDropMessage('g1', cfg)).toBe(false);
  });

  it('returns true for drop mode', () => {
    const cfg: SenderAllowlistConfig = {
      default: { allow: '*', mode: 'drop' },
      chats: {},
      logDenied: true,
    };
    expect(shouldDropMessage('g1', cfg)).toBe(true);
  });

  it('per-chat mode override', () => {
    const cfg: SenderAllowlistConfig = {
      default: { allow: '*', mode: 'trigger' },
      chats: { g1: { allow: '*', mode: 'drop' } },
      logDenied: true,
    };
    expect(shouldDropMessage('g1', cfg)).toBe(true);
    expect(shouldDropMessage('g2', cfg)).toBe(false);
  });
});

describe('isTriggerAllowed', () => {
  it('allows trigger for allowed sender', () => {
    const cfg: SenderAllowlistConfig = {
      default: { allow: ['alice'], mode: 'trigger' },
      chats: {},
      logDenied: false,
    };
    expect(isTriggerAllowed('g1', 'alice', cfg)).toBe(true);
  });

  it('denies trigger for disallowed sender', () => {
    const cfg: SenderAllowlistConfig = {
      default: { allow: ['alice'], mode: 'trigger' },
      chats: {},
      logDenied: false,
    };
    expect(isTriggerAllowed('g1', 'eve', cfg)).toBe(false);
  });

  it('logs when logDenied is true', () => {
    const cfg: SenderAllowlistConfig = {
      default: { allow: ['alice'], mode: 'trigger' },
      chats: {},
      logDenied: true,
    };
    isTriggerAllowed('g1', 'eve', cfg);
    // Logger.debug is called — we just verify no crash; logger is a real pino instance
  });
});
