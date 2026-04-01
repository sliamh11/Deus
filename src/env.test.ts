import { describe, it, expect, vi, beforeEach } from 'vitest';

// Mock fs before importing the module under test
vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      readFileSync: vi.fn(),
    },
  };
});

// Mock logger to suppress output
vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import fs from 'fs';
import { readEnvFile } from './env.js';

const mockReadFileSync = vi.mocked(fs.readFileSync);

beforeEach(() => {
  vi.resetAllMocks();
});

describe('readEnvFile', () => {
  it('returns only the requested keys from a .env file', () => {
    mockReadFileSync.mockReturnValue(
      'ANTHROPIC_API_KEY=sk-test\nGEMINI_API_KEY=gemini-key\nOTHER=value\n',
    );

    const result = readEnvFile(['ANTHROPIC_API_KEY', 'GEMINI_API_KEY']);
    expect(result).toEqual({
      ANTHROPIC_API_KEY: 'sk-test',
      GEMINI_API_KEY: 'gemini-key',
    });
  });

  it('excludes keys not in the requested list', () => {
    mockReadFileSync.mockReturnValue('FOO=bar\nBAZ=qux\n');

    const result = readEnvFile(['FOO']);
    expect(result).toEqual({ FOO: 'bar' });
    expect(result).not.toHaveProperty('BAZ');
  });

  it('strips double-quoted values', () => {
    mockReadFileSync.mockReturnValue('KEY="quoted value"\n');

    const result = readEnvFile(['KEY']);
    expect(result['KEY']).toBe('quoted value');
  });

  it('strips single-quoted values', () => {
    mockReadFileSync.mockReturnValue("KEY='single quoted'\n");

    const result = readEnvFile(['KEY']);
    expect(result['KEY']).toBe('single quoted');
  });

  it('ignores comment lines', () => {
    mockReadFileSync.mockReturnValue(
      '# This is a comment\nKEY=value\n# Another comment\n',
    );

    const result = readEnvFile(['KEY']);
    expect(result['KEY']).toBe('value');
  });

  it('ignores blank lines', () => {
    mockReadFileSync.mockReturnValue('\n\nKEY=value\n\n');

    const result = readEnvFile(['KEY']);
    expect(result['KEY']).toBe('value');
  });

  it('returns empty object when .env file does not exist', () => {
    mockReadFileSync.mockImplementation(() => {
      throw new Error('ENOENT: no such file');
    });

    const result = readEnvFile(['ANY_KEY']);
    expect(result).toEqual({});
  });

  it('returns empty object when keys list is empty', () => {
    mockReadFileSync.mockReturnValue('FOO=bar\nBAZ=qux\n');

    const result = readEnvFile([]);
    expect(result).toEqual({});
  });

  it('returns empty object when requested key is not in .env', () => {
    mockReadFileSync.mockReturnValue('OTHER=value\n');

    const result = readEnvFile(['MISSING_KEY']);
    expect(result).toEqual({});
  });

  it('handles KEY=value with equals sign in value', () => {
    mockReadFileSync.mockReturnValue('JWT=abc=def=ghi\n');

    const result = readEnvFile(['JWT']);
    expect(result['JWT']).toBe('abc=def=ghi');
  });

  it('skips lines without equals sign', () => {
    mockReadFileSync.mockReturnValue('NOEQUALS\nKEY=value\n');

    const result = readEnvFile(['NOEQUALS', 'KEY']);
    expect(result).not.toHaveProperty('NOEQUALS');
    expect(result['KEY']).toBe('value');
  });

  it('skips keys with empty values after stripping quotes', () => {
    mockReadFileSync.mockReturnValue('EMPTY=\n');

    const result = readEnvFile(['EMPTY']);
    // Empty string values are skipped per the source: if (value) result[key] = value
    expect(result).toEqual({});
  });
});
