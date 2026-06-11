import { describe, it, expect, vi, beforeEach } from 'vitest';
import fs from 'fs';
import path from 'path';

import { loadSkillIpcHandlers } from './index.js';
import { logger } from '../logger.js';

// Mock fs and logger
vi.mock('fs');
vi.mock('../logger.js', () => ({
  logger: {
    info: vi.fn(),
    warn: vi.fn(),
    error: vi.fn(),
  },
}));

describe('loadSkillIpcHandlers', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('does nothing when skills directory does not exist', async () => {
    vi.mocked(fs.existsSync).mockReturnValue(false);
    await loadSkillIpcHandlers();
    // Should not throw or call readdirSync
    expect(fs.readdirSync).not.toHaveBeenCalled();
  });

  it('skips skill directories without host.js or host.ts', async () => {
    vi.mocked(fs.existsSync).mockImplementation((p) => {
      const s = String(p);
      if (s.endsWith('skills')) return true; // skills dir exists
      return false; // no host.js or host.ts
    });
    vi.mocked(fs.readdirSync).mockReturnValue([
      { name: 'some-skill', isDirectory: () => true },
    ] as never);

    await loadSkillIpcHandlers();
    // No crash, no import attempted
  });

  it('scans skills directory for skill folders', async () => {
    const skillsDir = path.join(process.cwd(), '.claude', 'skills');
    vi.mocked(fs.existsSync).mockImplementation((p) => {
      return String(p) === skillsDir;
    });
    vi.mocked(fs.readdirSync).mockReturnValue([
      { name: 'x-integration', isDirectory: () => true },
      { name: 'README.md', isDirectory: () => false },
    ] as never);

    await loadSkillIpcHandlers();
    expect(fs.readdirSync).toHaveBeenCalledWith(skillsDir, {
      withFileTypes: true,
    });
  });

  it('warns loudly when a skill has host.ts but no compiled host.js', async () => {
    const skillsDir = path.join(process.cwd(), '.claude', 'skills');
    vi.mocked(fs.existsSync).mockImplementation((p) => {
      const s = String(p);
      if (s === skillsDir) return true; // skills dir exists
      if (s.endsWith('host.js')) return false; // not compiled
      if (s.endsWith('host.ts')) return true; // source present
      return false;
    });
    vi.mocked(fs.readdirSync).mockReturnValue([
      { name: 'x-integration', isDirectory: () => true },
    ] as never);

    await loadSkillIpcHandlers();

    expect(logger.warn).toHaveBeenCalledWith(
      expect.objectContaining({ skill: 'x-integration' }),
      expect.stringContaining('no compiled host.js'),
    );
  });

  it('does not warn when a compiled host.js is present', async () => {
    const skillsDir = path.join(process.cwd(), '.claude', 'skills');
    vi.mocked(fs.existsSync).mockImplementation((p) => {
      const s = String(p);
      if (s === skillsDir) return true;
      if (s.endsWith('host.js')) return true; // compiled present
      return false;
    });
    vi.mocked(fs.readdirSync).mockReturnValue([
      { name: 'x-integration', isDirectory: () => true },
    ] as never);

    await loadSkillIpcHandlers();

    expect(logger.warn).not.toHaveBeenCalled();
  });
});
