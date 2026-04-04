import { vi, describe, it, expect, afterEach } from 'vitest';
import os from 'os';
import fs from 'fs';

import { resolveBash } from './container.js';

describe('resolveBash', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('returns "bash" on non-Windows platforms', () => {
    vi.spyOn(process, 'platform', 'get').mockReturnValue('darwin');
    expect(resolveBash()).toBe('bash');
  });

  it('returns "bash" on linux', () => {
    vi.spyOn(process, 'platform', 'get').mockReturnValue('linux');
    expect(resolveBash()).toBe('bash');
  });

  it('returns Git Bash path on Windows when found', () => {
    vi.spyOn(process, 'platform', 'get').mockReturnValue('win32');
    vi.spyOn(fs, 'existsSync').mockImplementation((p) => {
      return p === 'C:\\Program Files\\Git\\bin\\bash.exe';
    });
    expect(resolveBash()).toBe('C:\\Program Files\\Git\\bin\\bash.exe');
  });

  it('returns Git Bash x86 path on Windows when 64-bit not found', () => {
    vi.spyOn(process, 'platform', 'get').mockReturnValue('win32');
    vi.spyOn(fs, 'existsSync').mockImplementation((p) => {
      return p === 'C:\\Program Files (x86)\\Git\\bin\\bash.exe';
    });
    expect(resolveBash()).toBe('C:\\Program Files (x86)\\Git\\bin\\bash.exe');
  });

  it('falls back to "bash" on Windows when Git Bash not found', () => {
    vi.spyOn(process, 'platform', 'get').mockReturnValue('win32');
    vi.spyOn(fs, 'existsSync').mockReturnValue(false);
    expect(resolveBash()).toBe('bash');
  });
});
