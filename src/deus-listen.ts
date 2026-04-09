/**
 * deus listen — Record from mic, transcribe with whisper.cpp, copy to clipboard.
 *
 * Phase 2: Live Unicode VU meter, Node.js implementation.
 * Phase 3: --stream flag for VAD-segmented continuous dictation via sox silence effect.
 *
 * Invoked by deus-cmd.sh: `node dist/deus-listen.js [--stream] [--lang <code>]
 *                           [--max-silence <sec>] [--threshold <pct>] [--no-clipboard]`
 */

import { execFile, spawn } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { promisify } from 'util';

import { IS_MACOS, IS_LINUX, IS_WINDOWS, killProcess } from './platform.js';
import {
  transcribeFile,
  ensureWhisperModel,
  resolveDefaultModelPath,
  depInstallHint,
  TranscriptionError,
} from './transcription.js';

const execFileAsync = promisify(execFile);

// ── VU meter helpers (exported for unit tests) ────────────────────────────

const BAR_CHARS = '▁▂▃▄▅▆▇█';
const METER_WIDTH = 8; // number of history slots shown

/**
 * Compute RMS amplitude from a raw int16 LE PCM buffer.
 * Returns a value in [0, 1].
 */
export function computeRms(pcm: Buffer): number {
  const samples = Math.floor(pcm.length / 2);
  if (samples === 0) return 0;
  let sumSq = 0;
  for (let i = 0; i < samples; i++) {
    const s = pcm.readInt16LE(i * 2) / 32768;
    sumSq += s * s;
  }
  return Math.sqrt(sumSq / samples);
}

/**
 * Map an RMS value (0–1) to a single Unicode bar character.
 * Uses a log scale so quiet audio is visible.
 */
export function renderBar(rms: number): string {
  if (rms <= 0) return BAR_CHARS[0];
  // Map to [0,1] via log: typical speech ~0.02–0.3 RMS maps nicely with this curve
  const norm = Math.max(0, Math.min(1, (Math.log10(rms + 0.001) + 3) / 3));
  const idx = Math.min(
    BAR_CHARS.length - 1,
    Math.floor(norm * BAR_CHARS.length),
  );
  return BAR_CHARS[idx];
}

/**
 * Build a canonical 44-byte PCM WAV header.
 */
export function buildWavHeader(
  dataLen: number,
  sampleRate = 16000,
  channels = 1,
  bitsPerSample = 16,
): Buffer {
  const header = Buffer.alloc(44);
  const byteRate = (sampleRate * channels * bitsPerSample) / 8;
  const blockAlign = (channels * bitsPerSample) / 8;
  header.write('RIFF', 0, 'ascii');
  header.writeUInt32LE(dataLen + 36, 4);
  header.write('WAVE', 8, 'ascii');
  header.write('fmt ', 12, 'ascii');
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(channels, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(byteRate, 28);
  header.writeUInt16LE(blockAlign, 32);
  header.writeUInt16LE(bitsPerSample, 34);
  header.write('data', 36, 'ascii');
  header.writeUInt32LE(dataLen, 40);
  return header;
}

// ── Clipboard (exported for unit tests) ──────────────────────────────────

/** Copy text to the OS clipboard. Returns false if no clipboard tool is available. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (IS_MACOS) {
      await execFileAsync('pbcopy', [], { input: text } as any);
      return true;
    }
    if (IS_WINDOWS) {
      await execFileAsync('clip.exe', [], { input: text } as any);
      return true;
    }
    // Linux — try xclip, xsel, wl-copy in order
    for (const [bin, args] of [
      ['xclip', ['-selection', 'clipboard']],
      ['xsel', ['--clipboard', '--input']],
      ['wl-copy', []],
    ] as [string, string[]][]) {
      try {
        await execFileAsync(bin, args, { input: text } as any);
        return true;
      } catch {
        // try next
      }
    }
    return false;
  } catch {
    return false;
  }
}

/** Read current clipboard content. Returns empty string on failure. */
export async function readClipboard(): Promise<string> {
  try {
    if (IS_MACOS) {
      const { stdout } = await execFileAsync('pbpaste', []);
      return stdout;
    }
    if (IS_WINDOWS) {
      const { stdout } = await execFileAsync('powershell', [
        '-NoProfile',
        '-Command',
        'Get-Clipboard',
      ]);
      return stdout.replace(/\r\n/g, '\n').trimEnd();
    }
    // Linux
    for (const [bin, args] of [
      ['xclip', ['-selection', 'clipboard', '-o']],
      ['xsel', ['--clipboard', '--output']],
      ['wl-paste', []],
    ] as [string, string[]][]) {
      try {
        const { stdout } = await execFileAsync(bin, args);
        return stdout;
      } catch {
        // try next
      }
    }
    return '';
  } catch {
    return '';
  }
}

/** Append text to clipboard (read existing → join → write). */
export async function appendClipboard(text: string): Promise<void> {
  const existing = await readClipboard();
  const joined =
    existing && !existing.endsWith(' ') && !existing.endsWith('\n')
      ? `${existing} ${text}`
      : `${existing}${text}`;
  await copyToClipboard(joined);
}

// ── Dependency check ──────────────────────────────────────────────────────

async function checkDeps(deps: string[]): Promise<void> {
  const missing: string[] = [];
  for (const dep of deps) {
    try {
      await execFileAsync(dep, ['--version'], { timeout: 3000 });
    } catch {
      missing.push(dep);
    }
  }
  if (missing.length === 0) return;
  console.error(`\n  Missing dependencies: ${missing.join(', ')}\n`);
  for (const dep of missing) {
    console.error(`  ${depInstallHint(dep)}`);
  }
  console.error('');
  process.exit(1);
}

// ── Recording ─────────────────────────────────────────────────────────────

/** Args to spawn sox for raw PCM capture from the default input device. */
function recArgs(extra: string[] = []): { bin: string; args: string[] } {
  const base = [
    '-q',
    '-r',
    '16000',
    '-c',
    '1',
    '-b',
    '16',
    '-t',
    'raw',
    '-e',
    'signed-integer',
    '-L',
    '-',
  ];
  if (IS_WINDOWS) {
    return { bin: 'sox', args: ['-d', ...base, ...extra] };
  }
  return { bin: 'rec', args: [...base, ...extra] };
}

/** Format elapsed seconds as m:ss. */
function mmss(seconds: number): string {
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return `${m}:${s.toString().padStart(2, '0')}`;
}

/**
 * Record from the microphone, streaming raw PCM through the VU meter.
 * Stops on SIGINT or Enter keypress (TTY only).
 * Writes a valid WAV file to `wavPath` on completion.
 *
 * In `--stream` mode, sox silence args cause sox to exit automatically —
 * no Enter/SIGINT needed per segment.
 */
export function recordWithMeter(
  wavPath: string,
  opts: {
    silenceArgs?: string[]; // if set, appended to rec args for VAD
    label?: string; // e.g. "Recording..." or "Listening..."
    noClipboard?: boolean;
  } = {},
): Promise<void> {
  return new Promise((resolve, reject) => {
    const { bin, args } = recArgs(opts.silenceArgs ?? []);
    const rec = spawn(bin, args, { stdio: ['ignore', 'pipe', 'ignore'] });

    const chunks: Buffer[] = [];
    const history: string[] = Array(METER_WIDTH).fill(BAR_CHARS[0]);
    const isTTY = process.stdout.isTTY;
    const startTime = Date.now();
    let stopped = false;
    let meterInterval: ReturnType<typeof setInterval> | null = null;

    const label = opts.label ?? 'Recording… (Enter to stop)';

    const stop = () => {
      if (stopped) return;
      stopped = true;
      if (meterInterval) clearInterval(meterInterval);
      if (isTTY) {
        process.stdout.write('\r' + ' '.repeat(60) + '\r');
        try {
          process.stdin.setRawMode(false);
          process.stdin.pause();
        } catch {
          /* non-TTY or already paused */
        }
      }
      if (rec.pid) killProcess(rec.pid);
    };

    // Stdin keypress (TTY only)
    if (isTTY && !opts.silenceArgs) {
      try {
        process.stdin.setRawMode(true);
        process.stdin.resume();
        process.stdin.setEncoding('utf8');
        process.stdin.once('data', (key: string) => {
          // Ctrl+C (0x03) or Enter (\r / \n)
          if (key === '\u0003') {
            stop();
            process.exit(0);
          }
          stop();
        });
      } catch {
        /* stdin not controllable */
      }
    }

    // SIGINT handler for this recording session
    const onSigint = () => {
      stop();
      // In stream mode, reject so the outer loop can exit
      reject(new Error('SIGINT'));
    };
    process.once('SIGINT', onSigint);

    rec.stdout.on('data', (chunk: Buffer) => {
      chunks.push(chunk);
      const rms = computeRms(chunk);
      history.shift();
      history.push(renderBar(rms));

      if (isTTY) {
        const elapsed = Math.floor((Date.now() - startTime) / 1000);
        process.stdout.write(
          `\r  ${history.join('')}  ${mmss(elapsed)}  ${label}  `,
        );
      }
    });

    // Periodic elapsed update even when silent
    if (isTTY && !opts.silenceArgs) {
      meterInterval = setInterval(() => {
        const elapsed = Math.floor((Date.now() - startTime) / 1000);
        process.stdout.write(
          `\r  ${history.join('')}  ${mmss(elapsed)}  ${label}  `,
        );
      }, 200);
    }

    rec.once('close', () => {
      process.removeListener('SIGINT', onSigint);
      if (!stopped) stop();

      const pcm = Buffer.concat(chunks);
      const minBytes = 16000; // ~0.5 s at 16kHz s16 mono

      if (pcm.length < minBytes) {
        reject(new Error('Recording too short (< 0.5 s). Try again.'));
        return;
      }

      const header = buildWavHeader(pcm.length);
      fs.writeFileSync(wavPath, Buffer.concat([header, pcm]));
      resolve();
    });

    rec.once('error', (err) => {
      process.removeListener('SIGINT', onSigint);
      reject(new Error(`sox failed to start: ${err.message}`));
    });
  });
}

// ── Single-shot mode (Phase 2) ────────────────────────────────────────────

async function runSingleShot(opts: {
  lang: string;
  noClipboard: boolean;
  modelPath: string;
}): Promise<void> {
  const wavPath = path.join(os.tmpdir(), `deus-voice-${Date.now()}.wav`);
  console.log('\n  Recording… (press Enter or Ctrl+C to stop)\n');

  try {
    await recordWithMeter(wavPath);
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`\n  ${msg}`);
    process.exit(1);
  }

  console.log('\n  Transcribing…\n');

  let text: string;
  try {
    text = await transcribeFile(wavPath, {
      language: opts.lang,
      model: opts.modelPath,
    });
  } catch (err: unknown) {
    if (err instanceof TranscriptionError) {
      console.error(`  ${err.message}`);
    } else {
      console.error('  Transcription failed unexpectedly.');
    }
    process.exit(1);
  } finally {
    fs.rmSync(wavPath, { force: true });
  }

  if (!text) {
    console.error(
      '  Could not transcribe audio. Try speaking louder or longer.',
    );
    process.exit(1);
  }

  console.log(`  ${text}\n`);

  if (!opts.noClipboard) {
    const ok = await copyToClipboard(text);
    console.log(
      ok
        ? '  Copied to clipboard. Paste with Cmd+V / Ctrl+V.\n'
        : '  (clipboard not available)\n',
    );
  }
}

// ── Stream mode (Phase 3) ─────────────────────────────────────────────────

async function runStream(opts: {
  lang: string;
  noClipboard: boolean;
  modelPath: string;
  maxSilence: number;
  threshold: number;
}): Promise<void> {
  const { maxSilence, threshold } = opts;

  // sox silence effect args: start on first speech, stop after maxSilence of quiet
  const silenceArgs = [
    'silence',
    '1',
    '0.1',
    `${threshold}%`, // leading: start after 0.1s above threshold%
    '1',
    String(maxSilence),
    `${threshold}%`, // trailing: stop after maxSilence below threshold%
  ];

  console.log('\n  Streaming… (speak naturally, Ctrl+C to exit)\n');

  let segmentIndex = 0;

  while (true) {
    const wavPath = path.join(
      os.tmpdir(),
      `deus-voice-${Date.now()}-${segmentIndex}.wav`,
    );

    process.stdout.write(
      `  ${Array(METER_WIDTH).fill(BAR_CHARS[0]).join('')}  Listening…  `,
    );

    try {
      await recordWithMeter(wavPath, {
        silenceArgs,
        label: 'Recording…',
      });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      if (msg === 'SIGINT') break;
      // Too short / no speech — loop
      fs.rmSync(wavPath, { force: true });
      continue;
    }

    process.stdout.write('\r  Transcribing…' + ' '.repeat(40) + '\r');

    let text = '';
    try {
      text = await transcribeFile(wavPath, {
        language: opts.lang,
        model: opts.modelPath,
      });
    } catch {
      process.stdout.write('\r  (transcription failed, continuing…)\n');
    } finally {
      fs.rmSync(wavPath, { force: true });
    }

    if (text) {
      process.stdout.write(`\r  ${text}\n`);
      if (!opts.noClipboard) {
        await appendClipboard(text).catch(() => {});
      }
    }

    segmentIndex++;
  }

  console.log('\n  Stopped.\n');
}

// ── CLI entrypoint ────────────────────────────────────────────────────────

interface CliArgs {
  stream: boolean;
  lang: string;
  noClipboard: boolean;
  maxSilence: number;
  threshold: number;
}

export function parseArgs(argv: string[]): CliArgs {
  const args: CliArgs = {
    stream: false,
    lang: process.env.WHISPER_LANG ?? 'en',
    noClipboard: process.env.DEUS_LISTEN_NO_CLIPBOARD === '1',
    maxSilence: 1.5,
    threshold: 3,
  };
  for (let i = 0; i < argv.length; i++) {
    switch (argv[i]) {
      case '--stream':
        args.stream = true;
        break;
      case '--no-clipboard':
        args.noClipboard = true;
        break;
      case '--lang':
        args.lang = argv[++i] ?? args.lang;
        break;
      case '--max-silence':
        args.maxSilence = parseFloat(argv[++i] ?? '1.5');
        break;
      case '--threshold':
        args.threshold = parseFloat(argv[++i] ?? '3');
        break;
    }
  }
  return args;
}

async function main(): Promise<void> {
  const args = parseArgs(process.argv.slice(2));
  const deps = IS_WINDOWS ? ['sox'] : ['sox', 'rec'];
  await checkDeps(deps);

  const modelPath = resolveDefaultModelPath();
  await ensureWhisperModel(modelPath);

  if (args.stream) {
    await runStream({
      lang: args.lang,
      noClipboard: args.noClipboard,
      modelPath,
      maxSilence: args.maxSilence,
      threshold: args.threshold,
    });
  } else {
    await runSingleShot({
      lang: args.lang,
      noClipboard: args.noClipboard,
      modelPath,
    });
  }
}

main().catch((err: unknown) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
