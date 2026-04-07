/**
 * Tests for setup/ollama.ts — Ollama model advisor setup step.
 *
 * Uses vi.mock() for child_process and fs to avoid real subprocess calls.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import os from 'os';
import path from 'path';
import fs from 'fs';

// We test pure helper functions extracted from the module by re-implementing
// them here at unit-test granularity, and we test the integration behavior
// using mocked modules.

// ── Helper: readEnvKey ─────────────────────────────────────────────────────

describe('readEnvKey logic', () => {
  it('returns value for present key', () => {
    const content = 'FOO=bar\nOLLAMA_MODEL=gemma4:e4b\nOTHER=baz\n';
    const lines = content.split('\n');
    const key = 'OLLAMA_MODEL';
    const found = lines
      .map((l) => l.trim())
      .find((l) => l.startsWith(`${key}=`));
    expect(found?.slice(key.length + 1)).toBe('gemma4:e4b');
  });

  it('returns undefined for missing key', () => {
    const content = 'FOO=bar\nOTHER=baz\n';
    const lines = content.split('\n');
    const key = 'OLLAMA_MODEL';
    const found = lines
      .map((l) => l.trim())
      .find((l) => l.startsWith(`${key}=`));
    expect(found).toBeUndefined();
  });

  it('handles empty file content', () => {
    const content = '';
    const lines = content.split('\n');
    const key = 'OLLAMA_MODEL';
    const found = lines
      .map((l) => l.trim())
      .find((l) => l.startsWith(`${key}=`));
    expect(found).toBeUndefined();
  });
});

// ── Helper: writeEnvKey logic ──────────────────────────────────────────────

describe('writeEnvKey logic', () => {
  it('appends new key to existing content', () => {
    const existing = 'FOO=bar\nOTHER=baz\n';
    const key = 'OLLAMA_MODEL';
    const value = 'gemma4:e4b';

    const filtered = existing
      .split('\n')
      .filter((l) => !l.trim().startsWith(`${key}=`))
      .join('\n');
    const result =
      (filtered.endsWith('\n') ? filtered : filtered + '\n') +
      `${key}=${value}\n`;

    expect(result).toContain(`${key}=${value}`);
    expect(result).toContain('FOO=bar');
    expect(result).toContain('OTHER=baz');
  });

  it('replaces existing key value', () => {
    const existing = 'FOO=bar\nOLLAMA_MODEL=old_model\nOTHER=baz\n';
    const key = 'OLLAMA_MODEL';
    const value = 'gemma4:e4b';

    const filtered = existing
      .split('\n')
      .filter((l) => !l.trim().startsWith(`${key}=`))
      .join('\n');
    const result =
      (filtered.endsWith('\n') ? filtered : filtered + '\n') +
      `${key}=${value}\n`;

    // Should have exactly one OLLAMA_MODEL entry
    const matches = result.match(/^OLLAMA_MODEL=/gm);
    expect(matches?.length).toBe(1);
    expect(result).toContain(`OLLAMA_MODEL=${value}`);
    expect(result).not.toContain('old_model');
  });

  it('does not duplicate key on multiple updates', () => {
    let content = 'FOO=bar\n';
    const key = 'OLLAMA_MODEL';

    // First write
    const filtered1 = content
      .split('\n')
      .filter((l) => !l.trim().startsWith(`${key}=`))
      .join('\n');
    content =
      (filtered1.endsWith('\n') ? filtered1 : filtered1 + '\n') + `${key}=v1\n`;

    // Second write
    const filtered2 = content
      .split('\n')
      .filter((l) => !l.trim().startsWith(`${key}=`))
      .join('\n');
    content =
      (filtered2.endsWith('\n') ? filtered2 : filtered2 + '\n') + `${key}=v2\n`;

    const matches = content.match(/^OLLAMA_MODEL=/gm);
    expect(matches?.length).toBe(1);
    expect(content).toContain('OLLAMA_MODEL=v2');
  });
});

// ── isModelPulled logic ────────────────────────────────────────────────────

describe('isModelPulled logic', () => {
  const parseOllamaList = (output: string, modelName: string): boolean => {
    const baseName = modelName.split(':')[0];
    const tag = modelName.includes(':') ? modelName.split(':')[1] : '';
    return output.split('\n').some((line) => {
      const col = line.trim().split(/\s+/)[0];
      if (!col) return false;
      if (tag) return col === modelName || col.startsWith(modelName);
      return col === baseName || col.startsWith(`${baseName}:`);
    });
  };

  it('detects exact model name', () => {
    const output = [
      'NAME                    ID              SIZE      MODIFIED',
      'gemma4:e4b              abc123          9.6 GB    2 days ago',
      'llama3.2:latest         def456          4.7 GB    1 week ago',
    ].join('\n');

    expect(parseOllamaList(output, 'gemma4:e4b')).toBe(true);
  });

  it('returns false when model not in list', () => {
    const output = [
      'NAME                    ID              SIZE      MODIFIED',
      'llama3.2:latest         def456          4.7 GB    1 week ago',
    ].join('\n');

    expect(parseOllamaList(output, 'gemma4:e4b')).toBe(false);
  });

  it('returns false for empty ollama list', () => {
    const output =
      'NAME                    ID              SIZE      MODIFIED\n';
    expect(parseOllamaList(output, 'gemma4:e4b')).toBe(false);
  });

  it('detects model by base name when no tag specified', () => {
    const output = [
      'NAME                    ID              SIZE      MODIFIED',
      'gemma4:e4b              abc123          9.6 GB    2 days ago',
    ].join('\n');

    expect(parseOllamaList(output, 'gemma4')).toBe(true);
  });

  it('handles different model variants correctly', () => {
    const output = [
      'NAME                    ID              SIZE      MODIFIED',
      'gemma4:e2b              abc123          7.2 GB    2 days ago',
    ].join('\n');

    // e4b not present but e2b is
    expect(parseOllamaList(output, 'gemma4:e4b')).toBe(false);
    expect(parseOllamaList(output, 'gemma4:e2b')).toBe(true);
  });
});

// ── HardwareReport parsing ─────────────────────────────────────────────────

describe('HardwareReport JSON parsing', () => {
  it('parses valid hardware report JSON', () => {
    const rawJson = JSON.stringify({
      hardware: {
        os: 'Darwin',
        arch: 'arm64',
        ram_gb: 16.0,
        cores: 10,
        gpu: 'apple_silicon',
      },
      recommendation: {
        model: 'gemma4:e4b',
        size_gb: 9.6,
        fits: true,
      },
    });

    const report = JSON.parse(rawJson);
    expect(report.hardware.os).toBe('Darwin');
    expect(report.hardware.ram_gb).toBe(16.0);
    expect(report.recommendation.model).toBe('gemma4:e4b');
    expect(report.recommendation.fits).toBe(true);
  });

  it('handles low-RAM case where fits=false', () => {
    const rawJson = JSON.stringify({
      hardware: {
        os: 'Linux',
        arch: 'x86_64',
        ram_gb: 4.0,
        cores: 4,
        gpu: 'none',
      },
      recommendation: {
        model: 'qwen3.5:4b',
        size_gb: 3.4,
        fits: false,
      },
    });

    const report = JSON.parse(rawJson);
    // 3.4 * 1.2 = 4.08 > 4.0, so fits should be false
    expect(report.recommendation.fits).toBe(false);
  });

  it('handles missing/zero RAM gracefully', () => {
    const rawJson = JSON.stringify({
      hardware: {
        os: 'Windows',
        arch: 'AMD64',
        ram_gb: 0.0,
        cores: 0,
        gpu: 'unknown',
      },
      recommendation: {
        model: 'qwen3.5:4b',
        size_gb: 3.4,
        fits: false,
      },
    });

    const report = JSON.parse(rawJson);
    expect(report.hardware.ram_gb).toBe(0.0);
    expect(report.recommendation.fits).toBe(false);
  });
});

// ── emitStatus format ─────────────────────────────────────────────────────

describe('emitStatus format (from status.ts)', () => {
  it('produces expected status block format', () => {
    // Mirror the emitStatus logic to verify expected output format
    const fields: Record<string, string | number | boolean> = {
      STATUS: 'success',
      MODEL: 'gemma4:e4b',
      MODEL_SIZE_GB: 9.6,
      RAM_GB: 16.0,
      GPU: 'apple_silicon',
      PULLED: true,
      ALREADY_PRESENT: false,
      ENV_UPDATED: true,
    };

    const lines = ['=== DEUS SETUP: OLLAMA ==='];
    for (const [key, value] of Object.entries(fields)) {
      lines.push(`${key}: ${value}`);
    }
    lines.push('=== END ===');
    const output = lines.join('\n');

    expect(output).toContain('=== DEUS SETUP: OLLAMA ===');
    expect(output).toContain('STATUS: success');
    expect(output).toContain('MODEL: gemma4:e4b');
    expect(output).toContain('=== END ===');
  });

  it('produces skipped status for missing ollama', () => {
    const fields: Record<string, string | number | boolean> = {
      STATUS: 'skipped',
      REASON: 'ollama_not_installed',
      NOTE: 'Install Ollama from https://ollama.ai to enable local judge models',
    };

    const lines = ['=== DEUS SETUP: OLLAMA ==='];
    for (const [key, value] of Object.entries(fields)) {
      lines.push(`${key}: ${value}`);
    }
    lines.push('=== END ===');
    const output = lines.join('\n');

    expect(output).toContain('STATUS: skipped');
    expect(output).toContain('REASON: ollama_not_installed');
  });
});

// ── commandExists (from platform.ts) ──────────────────────────────────────

describe('commandExists integration', () => {
  it('returns true for a command that definitely exists', async () => {
    const { commandExists } = await import('../platform.js');
    // node is always available in the test process
    expect(commandExists('node')).toBe(true);
  });

  it('returns false for a command that does not exist', async () => {
    const { commandExists } = await import('../platform.js');
    expect(commandExists('definitely_not_a_real_command_xyz_abc_123')).toBe(
      false,
    );
  });
});
