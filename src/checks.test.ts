import { describe, it, expect, beforeEach, vi } from 'vitest';

vi.mock('./config.js', () => ({
  HOME_DIR: '/home/testuser',
  CONFIG_DIR: '/home/testuser/.config/deus',
}));

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

vi.mock('./env.js', () => ({
  readEnvFile: vi.fn(() => ({})),
}));

vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      existsSync: vi.fn(() => false),
      readFileSync: vi.fn(() => '{}'),
    },
  };
});

vi.mock('child_process', async () => {
  const actual =
    await vi.importActual<typeof import('child_process')>('child_process');
  return {
    ...actual,
    execSync: vi.fn(() => '0\n'),
  };
});

import fs from 'fs';
import { execSync } from 'child_process';
import { readEnvFile } from './env.js';
import {
  hasApiCredentials,
  hasGeminiApiKey,
  hasMemoryVault,
  hasPythonDeps,
  hasMemoryDb,
  hasAnyChannelAuth,
  countRegisteredGroups,
  readDeusConfig,
} from './checks.js';

const mockReadEnvFile = vi.mocked(readEnvFile);
const mockExistsSync = vi.mocked(fs.existsSync);
const mockReadFileSync = vi.mocked(fs.readFileSync);
const mockExecSync = vi.mocked(execSync);

beforeEach(() => {
  vi.resetAllMocks();
  mockExistsSync.mockReturnValue(false);
  mockReadFileSync.mockReturnValue('{}');
  mockExecSync.mockReturnValue(Buffer.from('0\n'));
  mockReadEnvFile.mockReturnValue({});
  // Clear process.env of credential vars
  delete process.env.ANTHROPIC_API_KEY;
  delete process.env.CLAUDE_CODE_OAUTH_TOKEN;
  delete process.env.ANTHROPIC_AUTH_TOKEN;
  delete process.env.GEMINI_API_KEY;
  delete process.env.DEUS_VAULT_PATH;
});

// ── hasApiCredentials ─────────────────────────────────────────────────────

describe('hasApiCredentials', () => {
  it('returns true when ANTHROPIC_API_KEY is in .env', () => {
    mockReadEnvFile.mockReturnValue({ ANTHROPIC_API_KEY: 'sk-test' });
    expect(hasApiCredentials()).toBe(true);
  });

  it('returns true when CLAUDE_CODE_OAUTH_TOKEN is in .env', () => {
    mockReadEnvFile.mockReturnValue({ CLAUDE_CODE_OAUTH_TOKEN: 'token-value' });
    expect(hasApiCredentials()).toBe(true);
  });

  it('returns true when ANTHROPIC_API_KEY is in process.env', () => {
    process.env.ANTHROPIC_API_KEY = 'sk-from-env';
    expect(hasApiCredentials()).toBe(true);
  });

  it('returns true when ~/.claude/.credentials.json has a valid OAuth token', () => {
    mockReadFileSync.mockReturnValue(
      JSON.stringify({ claudeAiOauth: { accessToken: 'oauth-from-file' } }),
    );
    expect(hasApiCredentials()).toBe(true);
  });

  it('returns false when no credentials are configured', () => {
    expect(hasApiCredentials()).toBe(false);
  });
});

// ── hasGeminiApiKey ───────────────────────────────────────────────────────

describe('hasGeminiApiKey', () => {
  it('returns true when GEMINI_API_KEY is in .env', () => {
    mockReadEnvFile.mockReturnValue({ GEMINI_API_KEY: 'gemini-key' });
    expect(hasGeminiApiKey()).toBe(true);
  });

  it('returns true when GEMINI_API_KEY is in process.env', () => {
    process.env.GEMINI_API_KEY = 'gemini-from-env';
    expect(hasGeminiApiKey()).toBe(true);
  });

  it('returns false when not configured', () => {
    expect(hasGeminiApiKey()).toBe(false);
  });
});

// ── readDeusConfig ────────────────────────────────────────────────────────

describe('readDeusConfig', () => {
  it('returns parsed config object when file exists', () => {
    mockReadFileSync.mockReturnValue('{"vault_path": "/tmp/vault"}');
    const config = readDeusConfig();
    expect(config.vault_path).toBe('/tmp/vault');
  });

  it('returns empty object when config file does not exist (readFileSync throws)', () => {
    mockReadFileSync.mockImplementation(() => {
      throw new Error('ENOENT');
    });
    const config = readDeusConfig();
    expect(config).toEqual({});
  });

  it('returns empty object when config is invalid JSON', () => {
    mockReadFileSync.mockReturnValue('not json');
    const config = readDeusConfig();
    expect(config).toEqual({});
  });
});

// ── hasMemoryVault ────────────────────────────────────────────────────────

describe('hasMemoryVault', () => {
  it('returns ok=false when no vault path is configured', () => {
    mockReadFileSync.mockReturnValue('{}');
    const result = hasMemoryVault();
    expect(result.ok).toBe(false);
    expect(result.path).toBeNull();
  });

  it('returns ok=false when vault path is configured but does not exist', () => {
    mockReadFileSync.mockReturnValue('{"vault_path": "/tmp/nonexistent"}');
    mockExistsSync.mockReturnValue(false);
    const result = hasMemoryVault();
    expect(result.ok).toBe(false);
    expect(result.path).toBe('/tmp/nonexistent');
  });

  it('returns ok=true when vault path exists', () => {
    mockReadFileSync.mockReturnValue('{"vault_path": "/tmp/vault"}');
    mockExistsSync.mockReturnValue(true);
    const result = hasMemoryVault();
    expect(result.ok).toBe(true);
    expect(result.path).toBe('/tmp/vault');
  });

  it('respects DEUS_VAULT_PATH environment variable', () => {
    process.env.DEUS_VAULT_PATH = '/env/vault';
    mockExistsSync.mockReturnValue(true);
    const result = hasMemoryVault();
    expect(result.ok).toBe(true);
    expect(result.path).toBe('/env/vault');
    delete process.env.DEUS_VAULT_PATH;
  });
});

// ── hasPythonDeps ─────────────────────────────────────────────────────────

describe('hasPythonDeps', () => {
  it('returns ok=true when all deps are present', () => {
    mockExecSync.mockReturnValue(Buffer.from(''));
    const result = hasPythonDeps();
    expect(result.ok).toBe(true);
    expect(result.missing).toHaveLength(0);
  });

  it('returns ok=false with python3 missing when python3 check fails', () => {
    mockExecSync.mockImplementation((cmd: string) => {
      if (String(cmd).includes('python3 --version')) {
        throw new Error('not found');
      }
      return Buffer.from('');
    });
    const result = hasPythonDeps();
    expect(result.ok).toBe(false);
    expect(result.missing).toContain('python3');
  });

  it('returns ok=false with sqlite-vec missing', () => {
    mockExecSync.mockImplementation((cmd: string) => {
      if (String(cmd).includes('sqlite_vec')) throw new Error('not found');
      return Buffer.from('');
    });
    const result = hasPythonDeps();
    expect(result.ok).toBe(false);
    expect(result.missing).toContain('sqlite-vec');
  });

  it('returns ok=false with google-genai missing', () => {
    mockExecSync.mockImplementation((cmd: string) => {
      if (String(cmd).includes('google')) throw new Error('not found');
      return Buffer.from('');
    });
    const result = hasPythonDeps();
    expect(result.ok).toBe(false);
    expect(result.missing).toContain('google-genai');
  });
});

// ── hasMemoryDb ───────────────────────────────────────────────────────────

describe('hasMemoryDb', () => {
  it('returns true when memory.db exists', () => {
    mockExistsSync.mockReturnValue(true);
    expect(hasMemoryDb()).toBe(true);
  });

  it('returns false when memory.db does not exist', () => {
    mockExistsSync.mockReturnValue(false);
    expect(hasMemoryDb()).toBe(false);
  });
});

// ── hasAnyChannelAuth ─────────────────────────────────────────────────────

describe('hasAnyChannelAuth', () => {
  it('returns true when WhatsApp creds.json exists', () => {
    mockExistsSync.mockImplementation((p: fs.PathLike) =>
      String(p).includes('creds.json'),
    );
    expect(hasAnyChannelAuth()).toBe(true);
  });

  it('returns true when TELEGRAM_BOT_TOKEN is in .env', () => {
    mockReadEnvFile.mockReturnValue({ TELEGRAM_BOT_TOKEN: 'bot-token' });
    expect(hasAnyChannelAuth()).toBe(true);
  });

  it('returns true when SLACK_BOT_TOKEN is in .env', () => {
    mockReadEnvFile.mockReturnValue({ SLACK_BOT_TOKEN: 'slack-token' });
    expect(hasAnyChannelAuth()).toBe(true);
  });

  it('returns false when no channel is configured', () => {
    mockExistsSync.mockReturnValue(false);
    expect(hasAnyChannelAuth()).toBe(false);
  });
});

// ── countRegisteredGroups ─────────────────────────────────────────────────

describe('countRegisteredGroups', () => {
  it('returns 0 when DB file does not exist', () => {
    mockExistsSync.mockReturnValue(false);
    expect(countRegisteredGroups()).toBe(0);
  });

  it('returns 0 when execSync fails', () => {
    mockExistsSync.mockReturnValue(true);
    mockExecSync.mockImplementation(() => {
      throw new Error('sqlite3 not found');
    });
    expect(countRegisteredGroups()).toBe(0);
  });

  it('parses count from sqlite3 output', () => {
    mockExistsSync.mockReturnValue(true);
    // The source uses { encoding: 'utf-8' } so execSync returns a string
    mockExecSync.mockReturnValue('3\n' as unknown as Buffer);
    expect(countRegisteredGroups()).toBe(3);
  });
});
