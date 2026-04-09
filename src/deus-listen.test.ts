import { describe, it, expect, vi } from 'vitest';

vi.mock('child_process', async () => {
  const actual =
    await vi.importActual<typeof import('child_process')>('child_process');
  return { ...actual, execFile: vi.fn(), spawn: vi.fn() };
});

vi.mock('fs', async () => {
  const actual = await vi.importActual<typeof import('fs')>('fs');
  return {
    ...actual,
    default: {
      ...actual,
      existsSync: vi.fn(() => true),
      writeFileSync: vi.fn(),
      rmSync: vi.fn(),
    },
  };
});

vi.mock('./platform.js', () => ({
  IS_MACOS: true,
  IS_LINUX: false,
  IS_WINDOWS: false,
  killProcess: vi.fn(),
}));

vi.mock('./transcription.js', () => ({
  transcribeFile: vi.fn(async () => 'hello world'),
  ensureWhisperModel: vi.fn(async () => {}),
  resolveDefaultModelPath: vi.fn(() => '/models/ggml-base.bin'),
  depInstallHint: vi.fn((dep: string) => `brew install ${dep}`),
  TranscriptionError: class TranscriptionError extends Error {},
}));

import { execFile } from 'child_process';
import {
  computeRms,
  renderBar,
  buildWavHeader,
  parseArgs,
  copyToClipboard,
  readClipboard,
  appendClipboard,
} from './deus-listen.js';

const mockExecFile = vi.mocked(execFile);

function makeExecFileCallback(stdout = '', error: Error | null = null) {
  // execFileAsync may be called with or without an options object, so always
  // take the last argument as the callback regardless of arity.
  return (...args: unknown[]) => {
    const callback = args[args.length - 1] as (...a: unknown[]) => void;
    callback(error, { stdout, stderr: '' });
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

// ── Pure function tests (no mocks needed) ──────────────────────────────────

describe('computeRms', () => {
  it('returns 0 for empty or short buffer', () => {
    expect(computeRms(Buffer.alloc(0))).toBe(0);
    expect(computeRms(Buffer.alloc(1))).toBe(0);
  });

  it('returns 0 for silence (all zeros)', () => {
    expect(computeRms(Buffer.alloc(64))).toBe(0);
  });

  it('returns ~1 for full-scale signal (all int16 max)', () => {
    const buf = Buffer.alloc(64);
    for (let i = 0; i < 64; i += 2) buf.writeInt16LE(32767, i);
    expect(computeRms(buf)).toBeCloseTo(1, 2);
  });

  it('returns a value between 0 and 1 for typical audio', () => {
    const buf = Buffer.alloc(64);
    for (let i = 0; i < 64; i += 2)
      buf.writeInt16LE(i % 2 === 0 ? 8000 : -8000, i);
    const rms = computeRms(buf);
    expect(rms).toBeGreaterThan(0);
    expect(rms).toBeLessThanOrEqual(1);
  });
});

describe('renderBar', () => {
  it('returns lowest bar for silence', () => {
    expect(renderBar(0)).toBe('▁');
  });

  it('returns highest bar for full-scale signal', () => {
    expect(renderBar(1)).toBe('█');
  });

  it('returns a single Unicode block character', () => {
    const bars = '▁▂▃▄▅▆▇█';
    expect(bars).toContain(renderBar(0.1));
    expect(bars).toContain(renderBar(0.5));
  });
});

describe('buildWavHeader', () => {
  it('returns exactly 44 bytes', () => {
    expect(buildWavHeader(1000).length).toBe(44);
  });

  it('starts with RIFF and contains WAVE and data markers', () => {
    const h = buildWavHeader(1000);
    expect(h.toString('ascii', 0, 4)).toBe('RIFF');
    expect(h.toString('ascii', 8, 12)).toBe('WAVE');
    expect(h.toString('ascii', 36, 40)).toBe('data');
  });

  it('writes the correct data length at offset 40', () => {
    const h = buildWavHeader(12345);
    expect(h.readUInt32LE(40)).toBe(12345);
  });

  it('writes file size = dataLen + 36 at offset 4', () => {
    const h = buildWavHeader(1000);
    expect(h.readUInt32LE(4)).toBe(1036);
  });
});

// ── parseArgs ──────────────────────────────────────────────────────────────

describe('parseArgs', () => {
  it('returns defaults for empty argv', () => {
    const args = parseArgs([]);
    expect(args.stream).toBe(false);
    expect(args.noClipboard).toBe(false);
    expect(args.maxSilence).toBe(1.5);
    expect(args.threshold).toBe(3);
  });

  it('sets stream flag', () => {
    expect(parseArgs(['--stream']).stream).toBe(true);
  });

  it('parses --lang', () => {
    expect(parseArgs(['--lang', 'he']).lang).toBe('he');
  });

  it('parses --max-silence', () => {
    expect(parseArgs(['--max-silence', '0.5']).maxSilence).toBe(0.5);
  });

  it('parses --threshold', () => {
    expect(parseArgs(['--threshold', '5']).threshold).toBe(5);
  });

  it('parses --no-clipboard', () => {
    expect(parseArgs(['--no-clipboard']).noClipboard).toBe(true);
  });
});

// ── Clipboard ──────────────────────────────────────────────────────────────

describe('copyToClipboard', () => {
  it('calls pbcopy on macOS and returns true', async () => {
    mockExecFile.mockImplementationOnce(makeExecFileCallback('') as any);
    const result = await copyToClipboard('hello');
    expect(result).toBe(true);
    expect(mockExecFile.mock.calls[0][0]).toBe('pbcopy');
  });

  it('returns false when pbcopy throws', async () => {
    mockExecFile.mockImplementationOnce(
      makeExecFileCallback('', new Error('not found')) as any,
    );
    const result = await copyToClipboard('hello');
    expect(result).toBe(false);
  });
});

describe('readClipboard', () => {
  it('calls pbpaste on macOS and returns output', async () => {
    mockExecFile.mockImplementationOnce(
      makeExecFileCallback('existing text') as any,
    );
    const result = await readClipboard();
    expect(result).toBe('existing text');
    expect(mockExecFile.mock.calls[0][0]).toBe('pbpaste');
  });

  it('returns empty string on error', async () => {
    mockExecFile.mockImplementationOnce(
      makeExecFileCallback('', new Error('fail')) as any,
    );
    const result = await readClipboard();
    expect(result).toBe('');
  });
});

describe('appendClipboard', () => {
  it('appends with space when existing content has no trailing space', async () => {
    // read returns existing, write returns success
    mockExecFile
      .mockImplementationOnce(makeExecFileCallback('existing') as any)
      .mockImplementationOnce(makeExecFileCallback('') as any);
    await appendClipboard('new');
    const writeCall = mockExecFile.mock.calls[1];
    expect((writeCall[2] as any).input).toBe('existing new');
  });

  it('appends directly when existing ends with space', async () => {
    mockExecFile
      .mockImplementationOnce(makeExecFileCallback('existing ') as any)
      .mockImplementationOnce(makeExecFileCallback('') as any);
    await appendClipboard('new');
    const writeCall = mockExecFile.mock.calls[1];
    expect((writeCall[2] as any).input).toBe('existing new');
  });

  it('writes text alone when clipboard is empty', async () => {
    mockExecFile
      .mockImplementationOnce(makeExecFileCallback('') as any)
      .mockImplementationOnce(makeExecFileCallback('') as any);
    await appendClipboard('first');
    const writeCall = mockExecFile.mock.calls[1];
    expect((writeCall[2] as any).input).toBe('first');
  });
});
