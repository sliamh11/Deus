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
import { run, cleanStaleLegacySymlink, checkExistingCli } from './cli.js';

describe('setup/cli', () => {
  const originalCwd = process.cwd();
  let tmpDir: string;

  beforeEach(() => {
    emitStatusCalls.length = 0;
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-cli-test-'));
    // Create fake deus-cmd.sh
    fs.writeFileSync(path.join(tmpDir, 'deus-cmd.sh'), '#!/bin/zsh\necho hi');
    // Create fake deus-cmd.ps1
    fs.writeFileSync(path.join(tmpDir, 'deus-cmd.ps1'), 'param() {}');
    process.chdir(tmpDir);
  });

  afterEach(() => {
    process.chdir(originalCwd);
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('creates symlink on unix platforms', async () => {
    vi.mocked(getPlatform).mockReturnValue('macos');

    await run([]);

    expect(emitStatusCalls).toHaveLength(1);
    expect(emitStatusCalls[0].event).toBe('SETUP_CLI');
    expect(emitStatusCalls[0].data.STATUS).toBe('success');

    const linkPath = emitStatusCalls[0].data.LINK_PATH as string;
    expect(fs.existsSync(linkPath)).toBe(true);
    expect(fs.lstatSync(linkPath).isSymbolicLink()).toBe(true);
    expect(fs.realpathSync(fs.readlinkSync(linkPath))).toBe(
      fs.realpathSync(path.join(tmpDir, 'deus-cmd.sh')),
    );

    // Clean up
    fs.unlinkSync(linkPath);
  });

  it('fails if deus-cmd.sh is missing on unix', async () => {
    vi.mocked(getPlatform).mockReturnValue('linux');
    fs.unlinkSync(path.join(tmpDir, 'deus-cmd.sh'));

    await run([]);

    expect(emitStatusCalls).toHaveLength(1);
    expect(emitStatusCalls[0].data.STATUS).toBe('failed');
    expect(emitStatusCalls[0].data.ERROR).toBe('deus-cmd.sh not found');
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
});
