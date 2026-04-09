import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('child_process', async () => {
  const actual =
    await vi.importActual<typeof import('child_process')>('child_process');
  return { ...actual, execFile: vi.fn() };
});

vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      existsSync: vi.fn(() => true),
      mkdirSync: vi.fn(),
    },
  };
});

vi.mock('./platform.js', () => ({
  IS_MACOS: true,
  IS_LINUX: false,
  IS_WINDOWS: false,
}));

import { execFile } from 'child_process';
import fs from 'fs';
import {
  transcribeFile,
  resolveDefaultModelPath,
  depInstallHint,
  TranscriptionError,
} from './transcription.js';

const mockExecFile = vi.mocked(execFile);

function makeExecFileCallback(
  stdout: string,
  stderr = '',
  error: Error | null = null,
) {
  return (
    _bin: string,
    _args: string[],
    callback: (...args: unknown[]) => void,
  ) => {
    callback(error, { stdout, stderr });
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('resolveDefaultModelPath', () => {
  it('returns WHISPER_MODEL env var when set', () => {
    const orig = process.env.WHISPER_MODEL;
    process.env.WHISPER_MODEL = '/custom/model.bin';
    expect(resolveDefaultModelPath()).toBe('/custom/model.bin');
    process.env.WHISPER_MODEL = orig;
  });

  it('returns a path ending with ggml-base.bin by default', () => {
    const orig = process.env.WHISPER_MODEL;
    delete process.env.WHISPER_MODEL;
    expect(resolveDefaultModelPath()).toMatch(/ggml-base\.bin$/);
    process.env.WHISPER_MODEL = orig;
  });
});

describe('transcribeFile', () => {
  it('returns trimmed transcript from whisper stdout', async () => {
    mockExecFile.mockImplementationOnce(
      makeExecFileCallback('  Hello, world.\n\n') as any,
    );
    const result = await transcribeFile('/tmp/test.wav');
    expect(result).toBe('Hello, world.');
  });

  it('joins multiple non-empty lines with a space', async () => {
    mockExecFile.mockImplementationOnce(
      makeExecFileCallback('  line one\n  line two\n') as any,
    );
    const result = await transcribeFile('/tmp/test.wav');
    expect(result).toBe('line one line two');
  });

  it('returns empty string when stdout is blank', async () => {
    mockExecFile.mockImplementationOnce(makeExecFileCallback('   \n\n') as any);
    const result = await transcribeFile('/tmp/test.wav');
    expect(result).toBe('');
  });

  it('throws TranscriptionError on whisper failure', async () => {
    mockExecFile.mockImplementationOnce(
      makeExecFileCallback('', '', new Error('spawn error')) as any,
    );
    await expect(transcribeFile('/tmp/test.wav')).rejects.toBeInstanceOf(
      TranscriptionError,
    );
  });

  it('uses opts.language and opts.bin when provided', async () => {
    mockExecFile.mockImplementationOnce(
      makeExecFileCallback('שלום עולם') as any,
    );
    await transcribeFile('/tmp/test.wav', {
      language: 'he',
      bin: 'my-whisper',
      model: '/m.bin',
    });
    const call = mockExecFile.mock.calls[0];
    expect(call[0]).toBe('my-whisper');
    expect(call[1]).toContain('-l');
    expect(call[1]).toContain('he');
    expect(call[1]).toContain('/m.bin');
  });
});

describe('depInstallHint', () => {
  it('returns brew command on macOS', () => {
    expect(depInstallHint('sox')).toContain('brew');
    expect(depInstallHint('whisper-cli')).toContain('brew');
  });
});
