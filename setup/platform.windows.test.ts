/**
 * Windows-specific platform branch tests.
 *
 * Uses vi.mock('child_process') so execSync is fully replaceable under ESM.
 * Runs in an isolated worker — the real child_process in platform.test.ts
 * is unaffected.
 */
import { vi, describe, it, expect, afterEach } from 'vitest';
import os from 'os';

vi.mock('child_process');

import { execSync } from 'child_process';
import {
  getPlatform,
  getServiceManager,
  commandExists,
  getNodePath,
  openBrowser,
} from './platform.js';

describe('Windows platform (mocked)', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.resetAllMocks();
  });

  it('getPlatform returns windows on win32', () => {
    vi.spyOn(os, 'platform').mockReturnValue('win32');
    expect(getPlatform()).toBe('windows');
  });

  it('getServiceManager returns servy when servy-cli is available', () => {
    vi.spyOn(os, 'platform').mockReturnValue('win32');
    vi.mocked(execSync).mockReturnValue('' as any);
    expect(getServiceManager()).toBe('servy');
  });

  it('getServiceManager returns nssm when only nssm is available', () => {
    vi.spyOn(os, 'platform').mockReturnValue('win32');
    vi.mocked(execSync).mockImplementation((cmd) => {
      if (String(cmd).includes('servy-cli')) throw new Error('not found');
      return '' as any;
    });
    expect(getServiceManager()).toBe('nssm');
  });

  it('getServiceManager returns none when no service manager is available', () => {
    vi.spyOn(os, 'platform').mockReturnValue('win32');
    vi.mocked(execSync).mockImplementation(() => {
      throw new Error('not found');
    });
    expect(getServiceManager()).toBe('none');
  });

  it('getNodePath uses where on win32 and returns first line', () => {
    vi.spyOn(os, 'platform').mockReturnValue('win32');
    vi.mocked(execSync).mockReturnValue(
      'C:\\Program Files\\nodejs\\node.exe\r\nC:\\other\\node.exe\r\n' as any,
    );
    expect(getNodePath()).toBe('C:\\Program Files\\nodejs\\node.exe');
  });

  it('commandExists uses where on win32', () => {
    vi.spyOn(os, 'platform').mockReturnValue('win32');
    vi.mocked(execSync).mockReturnValue('' as any);
    commandExists('someprogram');
    expect(vi.mocked(execSync)).toHaveBeenCalledWith('where someprogram', {
      stdio: 'ignore',
    });
  });

  it('openBrowser uses start on win32 and returns true', () => {
    vi.spyOn(os, 'platform').mockReturnValue('win32');
    vi.mocked(execSync).mockReturnValue('' as any);
    const result = openBrowser('https://example.com');
    expect(result).toBe(true);
    expect(vi.mocked(execSync)).toHaveBeenCalledWith(
      expect.stringContaining('start'),
      { stdio: 'ignore' },
    );
  });
});
