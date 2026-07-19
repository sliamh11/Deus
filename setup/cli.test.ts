import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import os from 'os';
import path from 'path';

// Mock platform module before importing cli
vi.mock('./platform.js', () => ({
  getPlatform: vi.fn(() => 'macos'),
}));

// Capture emitStatus calls
const emitStatusCalls: Array<{ event: string; data: Record<string, unknown> }> =
  [];
vi.mock('./status.js', () => ({
  emitStatus: vi.fn((event: string, data: Record<string, unknown>) => {
    emitStatusCalls.push({ event, data });
  }),
}));

// Mock logger
vi.mock('../src/logger.js', () => ({
  logger: {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

import { getPlatform } from './platform.js';
import {
  run,
  cleanStaleLegacySymlink,
  checkExistingCli,
  checkExistingCmdShim,
} from './cli.js';

// These tests exercise run() against the REAL ~/.local/bin (there's no way
// to redirect setupUnixCli/setupUnixCliV2's target without changing
// production code), so a developer running this suite could plausibly
// already have a real `deus`/`deus-v2` command installed there. A prior
// version of this file unconditionally unlinked ~/.local/bin/deus-v2 in its
// outer afterEach — a code review correctly caught that this would delete a
// developer's real, pre-existing launcher. backupEntry/restoreEntry snapshot
// whatever was at each path before the suite runs and put it back
// afterward, regardless of what any individual test did in between.
type EntryBackup =
  | { existed: false }
  | { existed: true; isSymlink: true; target: string }
  | { existed: true; isSymlink: false; content: Buffer; mode: number };

function backupEntry(p: string): EntryBackup {
  let stat;
  try {
    stat = fs.lstatSync(p);
  } catch {
    return { existed: false };
  }
  if (stat.isSymbolicLink()) {
    return { existed: true, isSymlink: true, target: fs.readlinkSync(p) };
  }
  // Capture the mode (permission bits, e.g. the executable bit) too — a code
  // review caught that restoring only file content via writeFileSync would
  // silently drop a pre-existing executable command's +x bit, breaking it.
  return {
    existed: true,
    isSymlink: false,
    content: fs.readFileSync(p),
    mode: stat.mode,
  };
}

function restoreEntry(p: string, backup: EntryBackup): void {
  try {
    fs.unlinkSync(p);
  } catch {
    // nothing there — fine
  }
  if (!backup.existed) return;
  fs.mkdirSync(path.dirname(p), { recursive: true });
  if (backup.isSymlink) {
    fs.symlinkSync(backup.target, p);
  } else {
    fs.writeFileSync(p, backup.content);
    fs.chmodSync(p, backup.mode);
  }
}

describe('setup/cli', () => {
  const originalCwd = process.cwd();
  const v1LinkPath = path.join(os.homedir(), '.local', 'bin', 'deus');
  const v2LinkPath = path.join(os.homedir(), '.local', 'bin', 'deus-v2');
  let tmpDir: string;
  let v1Backup: EntryBackup;
  let v2Backup: EntryBackup;

  beforeEach(() => {
    emitStatusCalls.length = 0;
    v1Backup = backupEntry(v1LinkPath);
    v2Backup = backupEntry(v2LinkPath);
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-cli-test-'));
    // Create fake deus-cmd.sh
    fs.writeFileSync(path.join(tmpDir, 'deus-cmd.sh'), '#!/bin/zsh\necho hi');
    // Create fake deus-cmd.ps1
    fs.writeFileSync(path.join(tmpDir, 'deus-cmd.ps1'), 'param() {}');
    // Create fake deus-v2-cmd.mjs (LIA-434) — run() now always attempts the
    // v2 launcher install alongside v1's, so v1-focused tests below need
    // this present to keep the v2 leg a quiet 'success' rather than noise.
    fs.writeFileSync(
      path.join(tmpDir, 'deus-v2-cmd.mjs'),
      '#!/usr/bin/env node\n',
    );
    process.chdir(tmpDir);
  });

  afterEach(() => {
    process.chdir(originalCwd);
    fs.rmSync(tmpDir, { recursive: true, force: true });
    restoreEntry(v1LinkPath, v1Backup);
    restoreEntry(v2LinkPath, v2Backup);
  });

  describe('backupEntry / restoreEntry (developer-data protection, LIA-434)', () => {
    // Direct unit tests for the save/restore helpers themselves, isolated
    // from run()'s real ~/.local/bin usage — regression coverage for the
    // exact bug a code review caught (an earlier version's outer afterEach
    // unconditionally deleted ~/.local/bin/deus-v2, which would destroy a
    // developer's real, pre-existing launcher).
    let checkDir: string;

    beforeEach(() => {
      checkDir = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-backup-check-'));
    });

    afterEach(() => {
      fs.rmSync(checkDir, { recursive: true, force: true });
    });

    it('restores a pre-existing regular file after it is deleted and replaced', () => {
      const p = path.join(checkDir, 'deus-v2');
      fs.writeFileSync(p, 'a real developer launcher, not a test fixture');

      const backup = backupEntry(p);
      fs.unlinkSync(p); // simulate a test deleting it
      fs.writeFileSync(p, 'test fixture content'); // simulate a test replacing it
      restoreEntry(p, backup);

      expect(fs.readFileSync(p, 'utf-8')).toBe(
        'a real developer launcher, not a test fixture',
      );
    });

    it('preserves the executable bit of a pre-existing regular file (code-review-caught bug)', () => {
      // A prior version only restored content via writeFileSync, which
      // creates the new file with default (non-executable) permissions — a
      // code review caught that this would silently strip the +x bit off a
      // developer's real, executable ~/.local/bin/deus-v2 after the test
      // suite ran, breaking the installed command.
      const p = path.join(checkDir, 'deus-v2');
      fs.writeFileSync(p, '#!/usr/bin/env node\n// real launcher', {
        mode: 0o755,
      });
      expect(fs.statSync(p).mode & 0o777).toBe(0o755);

      const backup = backupEntry(p);
      fs.unlinkSync(p);
      fs.writeFileSync(p, 'test fixture content'); // test fixtures are non-executable
      restoreEntry(p, backup);

      expect(fs.statSync(p).mode & 0o777).toBe(0o755);
    });

    it('restores a pre-existing symlink after it is deleted and replaced', () => {
      const target = path.join(checkDir, 'real-target');
      fs.writeFileSync(target, 'real target');
      const p = path.join(checkDir, 'deus-v2');
      fs.symlinkSync(target, p);

      const backup = backupEntry(p);
      fs.unlinkSync(p);
      fs.writeFileSync(p, 'test fixture content');
      restoreEntry(p, backup);

      expect(fs.lstatSync(p).isSymbolicLink()).toBe(true);
      expect(fs.readlinkSync(p)).toBe(target);
    });

    it('leaves nothing behind when nothing existed before', () => {
      const p = path.join(checkDir, 'deus-v2');
      const backup = backupEntry(p);
      fs.writeFileSync(p, 'test fixture content'); // simulate a test creating it
      restoreEntry(p, backup);

      expect(fs.existsSync(p)).toBe(false);
    });
  });

  it('creates symlink on unix platforms', async () => {
    vi.mocked(getPlatform).mockReturnValue('macos');

    await run([]);

    // run() now always attempts the v2 launcher install too (LIA-434) —
    // SETUP_CLI (v1) first, then SETUP_CLI_V2, independent of each other.
    expect(emitStatusCalls).toHaveLength(2);
    expect(emitStatusCalls[0].event).toBe('SETUP_CLI');
    expect(emitStatusCalls[0].data.STATUS).toBe('success');
    expect(emitStatusCalls[1].event).toBe('SETUP_CLI_V2');
    expect(emitStatusCalls[1].data.STATUS).toBe('success');

    const linkPath = emitStatusCalls[0].data.LINK_PATH as string;
    expect(fs.existsSync(linkPath)).toBe(true);
    expect(fs.lstatSync(linkPath).isSymbolicLink()).toBe(true);
    expect(fs.realpathSync(fs.readlinkSync(linkPath))).toBe(
      fs.realpathSync(path.join(tmpDir, 'deus-cmd.sh')),
    );

    // Clean up
    fs.unlinkSync(linkPath);
    fs.unlinkSync(emitStatusCalls[1].data.LINK_PATH as string);
  });

  it('fails if deus-cmd.sh is missing on unix', async () => {
    vi.mocked(getPlatform).mockReturnValue('linux');
    fs.unlinkSync(path.join(tmpDir, 'deus-cmd.sh'));

    await run([]);

    expect(emitStatusCalls).toHaveLength(2);
    expect(emitStatusCalls[0].event).toBe('SETUP_CLI');
    expect(emitStatusCalls[0].data.STATUS).toBe('failed');
    expect(emitStatusCalls[0].data.ERROR).toBe('deus-cmd.sh not found');
    // v1 failing must not affect the independent v2 leg.
    expect(emitStatusCalls[1].event).toBe('SETUP_CLI_V2');
    expect(emitStatusCalls[1].data.STATUS).toBe('success');

    // Clean up
    fs.unlinkSync(emitStatusCalls[1].data.LINK_PATH as string);
  });

  it('replaces existing dead symlink', async () => {
    vi.mocked(getPlatform).mockReturnValue('macos');

    // Create an existing dead symlink
    const binDir = path.join(os.homedir(), '.local', 'bin');
    const linkPath = path.join(binDir, 'deus');
    fs.mkdirSync(binDir, { recursive: true });
    try {
      fs.unlinkSync(linkPath);
    } catch {
      // doesn't exist
    }
    fs.symlinkSync('/tmp/old-deus-nonexistent', linkPath);

    await run([]);

    expect(emitStatusCalls[0].data.STATUS).toBe('success');
    expect(fs.realpathSync(fs.readlinkSync(linkPath))).toBe(
      fs.realpathSync(path.join(tmpDir, 'deus-cmd.sh')),
    );

    // Clean up
    fs.unlinkSync(linkPath);
  });

  it('replaces existing Deus symlink from different install path', async () => {
    vi.mocked(getPlatform).mockReturnValue('macos');

    const binDir = path.join(os.homedir(), '.local', 'bin');
    const linkPath = path.join(binDir, 'deus');
    fs.mkdirSync(binDir, { recursive: true });
    try {
      fs.unlinkSync(linkPath);
    } catch {
      // doesn't exist
    }
    // Symlink pointing to our own deus-cmd.sh (current dir)
    fs.symlinkSync(path.join(tmpDir, 'deus-cmd.sh'), linkPath);

    await run([]);

    expect(emitStatusCalls[0].data.STATUS).toBe('success');

    // Clean up
    fs.unlinkSync(linkPath);
  });

  it('skips symlink creation when foreign binary exists', async () => {
    vi.mocked(getPlatform).mockReturnValue('macos');

    const binDir = path.join(os.homedir(), '.local', 'bin');
    const linkPath = path.join(binDir, 'deus');
    fs.mkdirSync(binDir, { recursive: true });
    try {
      fs.unlinkSync(linkPath);
    } catch {
      // doesn't exist
    }
    // Create a regular file (foreign binary)
    fs.writeFileSync(linkPath, '#!/bin/sh\necho "different deus tool"');

    await run([]);

    expect(emitStatusCalls[0].data.STATUS).toBe('conflict');
    expect(emitStatusCalls[0].data.EXISTING).toBe('foreign');
    // Foreign file should still be intact
    expect(fs.readFileSync(linkPath, 'utf-8')).toContain('different deus tool');

    // Clean up
    fs.unlinkSync(linkPath);
  });

  describe('checkExistingCli', () => {
    let checkDir: string;

    beforeEach(() => {
      checkDir = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-check-'));
    });

    afterEach(() => {
      fs.rmSync(checkDir, { recursive: true, force: true });
    });

    it('returns none when path does not exist', () => {
      expect(checkExistingCli(path.join(checkDir, 'deus'))).toBe('none');
    });

    it('returns ours when symlink points to deus-cmd.sh', () => {
      const target = path.join(checkDir, 'deus-cmd.sh');
      fs.writeFileSync(target, '#!/bin/sh');
      fs.symlinkSync(target, path.join(checkDir, 'deus'));
      expect(checkExistingCli(path.join(checkDir, 'deus'))).toBe('ours');
    });

    it('returns dead when symlink target does not exist', () => {
      fs.symlinkSync('/tmp/nonexistent-xyz', path.join(checkDir, 'deus'));
      expect(checkExistingCli(path.join(checkDir, 'deus'))).toBe('dead');
    });

    it('returns foreign for symlink to non-deus target', () => {
      const target = path.join(checkDir, 'other-tool');
      fs.writeFileSync(target, '#!/bin/sh');
      fs.symlinkSync(target, path.join(checkDir, 'deus'));
      expect(checkExistingCli(path.join(checkDir, 'deus'))).toBe('foreign');
    });

    it('returns foreign for regular file', () => {
      fs.writeFileSync(path.join(checkDir, 'deus'), '#!/bin/sh\necho hi');
      expect(checkExistingCli(path.join(checkDir, 'deus'))).toBe('foreign');
    });
  });

  describe('checkExistingCmdShim (Windows .cmd shim conflict protection, LIA-434)', () => {
    let checkDir: string;

    beforeEach(() => {
      checkDir = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-cmdshim-check-'));
    });

    afterEach(() => {
      fs.rmSync(checkDir, { recursive: true, force: true });
    });

    it('returns none when path does not exist', () => {
      expect(checkExistingCmdShim(path.join(checkDir, 'deus.cmd'), 'v1')).toBe(
        'none',
      );
    });

    it('returns ours for a v1 shim generated for the expected script', () => {
      const cmdPath = path.join(checkDir, 'deus.cmd');
      fs.writeFileSync(
        cmdPath,
        '@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "C:\\deus\\deus-cmd.ps1" %*\r\n',
      );
      expect(checkExistingCmdShim(cmdPath, 'v1')).toBe('ours');
    });

    it('returns ours for a v2 shim generated for the expected script', () => {
      const cmdPath = path.join(checkDir, 'deus-v2.cmd');
      fs.writeFileSync(
        cmdPath,
        '@echo off\r\nnode "C:\\deus\\deus-v2-cmd.mjs" %*\r\n',
      );
      expect(checkExistingCmdShim(cmdPath, 'v2')).toBe('ours');
    });

    it('returns foreign for a regular file not generated by this step', () => {
      const cmdPath = path.join(checkDir, 'deus.cmd');
      fs.writeFileSync(
        cmdPath,
        '@echo off\r\necho some other unrelated tool\r\n',
      );
      expect(checkExistingCmdShim(cmdPath, 'v1')).toBe('foreign');
    });

    it('returns foreign for a user-authored wrapper that merely mentions the script name (code-review-caught bug)', () => {
      // Regression test for a real bug a GPT-backend code review caught:
      // the original check was a loose `content.includes(basename)`
      // substring test, which would have classified ANY file merely
      // mentioning "deus-cmd.ps1" as "ours" — including a user's own,
      // unrelated wrapper script — and silently overwritten it. Only an
      // exact structural match to this step's own generated format now
      // counts as "ours".
      const cmdPath = path.join(checkDir, 'deus.cmd');
      fs.writeFileSync(
        cmdPath,
        'REM my custom wrapper around deus-cmd.ps1, do not touch\r\npowershell -File deus-cmd.ps1\r\n',
      );
      expect(checkExistingCmdShim(cmdPath, 'v1')).toBe('foreign');
    });

    it('returns foreign when the target basename merely ends with the expected name, no separator boundary (code-review-caught bypass)', () => {
      // Regression test for a real bypass a GPT-backend code review caught:
      // an earlier version of the pattern required only that
      // "deus-v2-cmd.mjs" appear as a SUFFIX of whatever preceded it inside
      // the quotes, with no path-separator boundary — so a foreign shim
      // targeting a DIFFERENT file whose name merely ends with the expected
      // basename (e.g. "custom-deus-v2-cmd.mjs") would also match and be
      // wrongly classified 'ours', overwriting the user's file.
      const cmdPath = path.join(checkDir, 'deus-v2.cmd');
      fs.writeFileSync(
        cmdPath,
        '@echo off\r\nnode "C:\\tools\\custom-deus-v2-cmd.mjs" %*\r\n',
      );
      expect(checkExistingCmdShim(cmdPath, 'v2')).toBe('foreign');
    });

    it('returns foreign for a symlink at the shim path, never following it', () => {
      const target = path.join(checkDir, 'something-else');
      fs.writeFileSync(target, 'arbitrary content');
      const cmdPath = path.join(checkDir, 'deus.cmd');
      fs.symlinkSync(target, cmdPath);
      expect(checkExistingCmdShim(cmdPath, 'v1')).toBe('foreign');
    });

    it('v1 and v2 markers are distinct — a v1 shim is not mistaken for a v2 one', () => {
      const cmdPath = path.join(checkDir, 'deus.cmd');
      fs.writeFileSync(
        cmdPath,
        '@echo off\r\npowershell -NoProfile -ExecutionPolicy Bypass -File "C:\\deus\\deus-cmd.ps1" %*\r\n',
      );
      expect(checkExistingCmdShim(cmdPath, 'v2')).toBe('foreign');
    });
  });

  describe('cleanStaleLegacySymlink', () => {
    const legacyDir = path.join(os.tmpdir(), 'deus-legacy-test');
    const legacyPath = path.join(legacyDir, 'deus');
    let mockLog: {
      info: ReturnType<typeof vi.fn>;
      warn: ReturnType<typeof vi.fn>;
    };

    // We can't write to /usr/local/bin in tests, so we test the function
    // directly with a monkey-patched path via fs mocking.
    // Instead, test the logic by calling the exported function with a mock
    // that simulates stale symlinks in a temp dir.

    beforeEach(() => {
      fs.mkdirSync(legacyDir, { recursive: true });
      mockLog = { info: vi.fn(), warn: vi.fn() };
    });

    afterEach(() => {
      fs.rmSync(legacyDir, { recursive: true, force: true });
    });

    it('removes a dead symlink at the legacy path', () => {
      const deadLink = path.join(legacyDir, 'deus');
      fs.symlinkSync('/tmp/nonexistent-deus-target-xyz', deadLink);

      cleanStaleLegacySymlink(mockLog, deadLink);

      // Symlink should be removed
      expect(() => fs.lstatSync(deadLink)).toThrow();
      expect(mockLog.info).toHaveBeenCalledTimes(1);
    });

    it('leaves alive symlinks untouched', () => {
      const target = path.join(legacyDir, 'real-target');
      fs.writeFileSync(target, 'exists');
      const aliveLink = path.join(legacyDir, 'deus');
      fs.symlinkSync(target, aliveLink);

      cleanStaleLegacySymlink(mockLog, aliveLink);

      // Symlink should still exist
      expect(fs.existsSync(aliveLink)).toBe(true);
      expect(fs.lstatSync(aliveLink).isSymbolicLink()).toBe(true);
      expect(mockLog.info).not.toHaveBeenCalled();
      expect(mockLog.warn).not.toHaveBeenCalled();
    });

    it('leaves regular files untouched', () => {
      const regularFile = path.join(legacyDir, 'deus');
      fs.writeFileSync(regularFile, '#!/bin/sh\necho deus');

      cleanStaleLegacySymlink(mockLog, regularFile);

      // File should still exist
      expect(fs.existsSync(regularFile)).toBe(true);
      expect(mockLog.info).not.toHaveBeenCalled();
      expect(mockLog.warn).not.toHaveBeenCalled();
    });

    it('does nothing when path does not exist', () => {
      const missingPath = path.join(legacyDir, 'nonexistent');
      cleanStaleLegacySymlink(mockLog, missingPath);

      expect(mockLog.info).not.toHaveBeenCalled();
      expect(mockLog.warn).not.toHaveBeenCalled();
    });
  });

  describe('deus-v2 CLI (LIA-434)', () => {
    it('creates the deus-v2 symlink independently of deus', async () => {
      vi.mocked(getPlatform).mockReturnValue('macos');

      await run([]);

      const v2Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI_V2')!;
      expect(v2Call.data.STATUS).toBe('success');

      const linkPath = v2Call.data.LINK_PATH as string;
      expect(fs.existsSync(linkPath)).toBe(true);
      expect(fs.lstatSync(linkPath).isSymbolicLink()).toBe(true);
      expect(fs.realpathSync(fs.readlinkSync(linkPath))).toBe(
        fs.realpathSync(path.join(tmpDir, 'deus-v2-cmd.mjs')),
      );

      fs.unlinkSync(linkPath);
    });

    it('fails only the v2 leg if deus-v2-cmd.mjs is missing — v1 unaffected', async () => {
      vi.mocked(getPlatform).mockReturnValue('macos');
      fs.unlinkSync(path.join(tmpDir, 'deus-v2-cmd.mjs'));

      await run([]);

      const v1Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI')!;
      const v2Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI_V2')!;
      expect(v1Call.data.STATUS).toBe('success');
      expect(v2Call.data.STATUS).toBe('failed');
      expect(v2Call.data.ERROR).toBe('deus-v2-cmd.mjs not found');

      fs.unlinkSync(v1Call.data.LINK_PATH as string);
    });

    it('replaces an existing dead deus-v2 symlink', async () => {
      vi.mocked(getPlatform).mockReturnValue('macos');

      const binDir = path.join(os.homedir(), '.local', 'bin');
      const linkPath = path.join(binDir, 'deus-v2');
      fs.mkdirSync(binDir, { recursive: true });
      try {
        fs.unlinkSync(linkPath);
      } catch {
        // doesn't exist
      }
      fs.symlinkSync('/tmp/old-deus-v2-nonexistent', linkPath);

      await run([]);

      const v2Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI_V2')!;
      expect(v2Call.data.STATUS).toBe('success');
      expect(fs.realpathSync(fs.readlinkSync(linkPath))).toBe(
        fs.realpathSync(path.join(tmpDir, 'deus-v2-cmd.mjs')),
      );

      const v1Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI')!;
      fs.unlinkSync(v1Call.data.LINK_PATH as string);
      fs.unlinkSync(linkPath);
    });

    it('replaces an existing deus-v2 symlink from a different install path', async () => {
      vi.mocked(getPlatform).mockReturnValue('macos');

      const binDir = path.join(os.homedir(), '.local', 'bin');
      const linkPath = path.join(binDir, 'deus-v2');
      fs.mkdirSync(binDir, { recursive: true });
      try {
        fs.unlinkSync(linkPath);
      } catch {
        // doesn't exist
      }
      fs.symlinkSync(path.join(tmpDir, 'deus-v2-cmd.mjs'), linkPath);

      await run([]);

      const v2Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI_V2')!;
      expect(v2Call.data.STATUS).toBe('success');

      const v1Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI')!;
      fs.unlinkSync(v1Call.data.LINK_PATH as string);
      fs.unlinkSync(linkPath);
    });

    it('skips deus-v2 symlink creation when a foreign binary exists at that path', async () => {
      vi.mocked(getPlatform).mockReturnValue('macos');

      const binDir = path.join(os.homedir(), '.local', 'bin');
      const linkPath = path.join(binDir, 'deus-v2');
      fs.mkdirSync(binDir, { recursive: true });
      try {
        fs.unlinkSync(linkPath);
      } catch {
        // doesn't exist
      }
      fs.writeFileSync(linkPath, '#!/bin/sh\necho "different tool"');

      await run([]);

      const v2Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI_V2')!;
      expect(v2Call.data.STATUS).toBe('conflict');
      expect(v2Call.data.EXISTING).toBe('foreign');
      expect(fs.readFileSync(linkPath, 'utf-8')).toContain('different tool');

      const v1Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI')!;
      fs.unlinkSync(v1Call.data.LINK_PATH as string);
      fs.unlinkSync(linkPath);
    });

    it('a v2 setup failure never affects the v1 SETUP_CLI status (failure isolation)', async () => {
      vi.mocked(getPlatform).mockReturnValue('macos');

      const binDir = path.join(os.homedir(), '.local', 'bin');
      const v2LinkPath = path.join(binDir, 'deus-v2');
      fs.mkdirSync(binDir, { recursive: true });
      try {
        fs.unlinkSync(v2LinkPath);
      } catch {
        // doesn't exist
      }

      const realSymlinkSync = fs.symlinkSync;
      const symlinkSpy = vi
        .spyOn(fs, 'symlinkSync')
        .mockImplementation((target, linkPath, ...rest) => {
          if (linkPath === v2LinkPath) {
            throw new Error('simulated v2 symlink failure');
          }
          return realSymlinkSync(target, linkPath, ...(rest as []));
        });

      await run([]);
      symlinkSpy.mockRestore();

      const v1Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI')!;
      const v2Call = emitStatusCalls.find((c) => c.event === 'SETUP_CLI_V2')!;
      expect(v1Call.data.STATUS).toBe('success');
      expect(v2Call.data.STATUS).toBe('failed');
      expect(v2Call.data.ERROR).toContain('simulated v2 symlink failure');

      fs.unlinkSync(v1Call.data.LINK_PATH as string);
    });
  });
});
