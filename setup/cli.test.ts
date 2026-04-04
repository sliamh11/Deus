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
import { run } from './cli.js';

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

  it('replaces existing symlink', async () => {
    vi.mocked(getPlatform).mockReturnValue('macos');

    // Create an existing symlink pointing elsewhere
    const binDir = path.join(os.homedir(), '.local', 'bin');
    const linkPath = path.join(binDir, 'deus');
    fs.mkdirSync(binDir, { recursive: true });
    try {
      fs.unlinkSync(linkPath);
    } catch {
      // doesn't exist
    }
    fs.symlinkSync('/tmp/old-deus', linkPath);

    await run([]);

    expect(emitStatusCalls[0].data.STATUS).toBe('success');
    expect(fs.realpathSync(fs.readlinkSync(linkPath))).toBe(
      fs.realpathSync(path.join(tmpDir, 'deus-cmd.sh')),
    );

    // Clean up
    fs.unlinkSync(linkPath);
  });
});
