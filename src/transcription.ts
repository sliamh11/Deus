/**
 * Shared transcription module — whisper.cpp wrapper.
 *
 * Used by `deus listen` and available to voice-note skills
 * (add-voice-transcription, use-local-whisper).
 *
 * All config comes from env vars; callers can override via TranscribeOptions.
 */

import { execFile } from 'child_process';
import { createWriteStream } from 'fs';
import fs from 'fs';
import path from 'path';
import { pipeline } from 'stream/promises';
import { promisify } from 'util';
import { fileURLToPath } from 'url';

import { IS_MACOS, IS_LINUX } from './platform.js';

const execFileAsync = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PROJECT_ROOT = path.resolve(__dirname, '..');

const MODEL_URL =
  'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin';

// ── Types ──────────────────────────────────────────────────────────────────

export interface TranscribeOptions {
  /** BCP-47 language code, e.g. 'en', 'he'. Default: WHISPER_LANG env || 'en'. */
  language?: string;
  /** Absolute path to ggml model file. Default: WHISPER_MODEL env || project default. */
  model?: string;
  /** whisper-cli binary name. Default: WHISPER_BIN env || 'whisper-cli'. */
  bin?: string;
}

export class TranscriptionError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'TranscriptionError';
  }
}

// ── Path resolution ────────────────────────────────────────────────────────

export function resolveDefaultModelPath(): string {
  return (
    process.env.WHISPER_MODEL ||
    path.join(PROJECT_ROOT, 'data', 'models', 'ggml-base.bin')
  );
}

// ── Model bootstrap ────────────────────────────────────────────────────────

/** Download ggml-base.bin on first use. No-op if the file already exists. */
export async function ensureWhisperModel(modelPath: string): Promise<void> {
  if (fs.existsSync(modelPath)) return;
  const dir = path.dirname(modelPath);
  fs.mkdirSync(dir, { recursive: true });
  console.log(
    '  Whisper model not found. Downloading ggml-base.bin (148 MB)...',
  );
  const res = await fetch(MODEL_URL);
  if (!res.ok || !res.body) {
    throw new TranscriptionError(
      `Failed to download model: HTTP ${res.status}`,
    );
  }
  await pipeline(
    res.body as unknown as NodeJS.ReadableStream,
    createWriteStream(modelPath),
  );
  console.log('  Download complete.\n');
}

// ── Transcription ──────────────────────────────────────────────────────────

/**
 * Transcribe a WAV file using whisper-cli.
 * Returns trimmed transcript text, or empty string if nothing was heard.
 */
export async function transcribeFile(
  wavPath: string,
  opts?: TranscribeOptions,
): Promise<string> {
  const bin = opts?.bin ?? process.env.WHISPER_BIN ?? 'whisper-cli';
  const model = opts?.model ?? resolveDefaultModelPath();
  const lang = opts?.language ?? process.env.WHISPER_LANG ?? 'en';

  const { stdout } = await execFileAsync(bin, [
    '-m',
    model,
    '-f',
    wavPath,
    '--no-timestamps',
    '-nt',
    '-l',
    lang,
  ]).catch((err: Error & { stderr?: string }) => {
    throw new TranscriptionError(
      `Transcription failed: ${err.message}${err.stderr ? '\n' + err.stderr : ''}`,
    );
  });

  return stdout
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .join(' ');
}

// ── Dep hints ─────────────────────────────────────────────────────────────

/** Returns a user-facing install hint for a missing dependency. */
export function depInstallHint(dep: string): string {
  const isSox = dep === 'sox';
  const isWhisper = dep === 'whisper-cli';
  if (IS_MACOS) {
    if (isWhisper) return 'brew install whisper-cpp';
    if (isSox) return 'brew install sox';
    return `brew install ${dep}`;
  }
  if (IS_LINUX) {
    if (isWhisper)
      return '# Build whisper-cpp from source: https://github.com/ggerganov/whisper.cpp';
    return `sudo apt install ${isSox ? 'sox libsox-fmt-all' : dep}`;
  }
  // Windows
  if (isWhisper)
    return '# Download whisper-cpp: https://github.com/ggerganov/whisper.cpp/releases';
  return `choco install ${isSox ? 'sox.portable' : dep}`;
}
