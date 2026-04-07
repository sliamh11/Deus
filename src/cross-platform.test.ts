/**
 * Cross-platform compatibility tests.
 *
 * These tests verify that the codebase doesn't contain patterns that break on
 * Windows. They scan source files for known anti-patterns (hardcoded Unix paths,
 * unsafe file:// stripping, missing platform branches, etc.).
 *
 * Run on every CI platform to catch regressions early.
 */

import fs from 'fs';
import path from 'path';
import { describe, it, expect } from 'vitest';

const SRC_DIR = path.resolve(__dirname);

/** Recursively collect .ts files (excluding tests, node_modules, dist). */
function collectTsFiles(dir: string): string[] {
  const files: string[] = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (['node_modules', 'dist', '.claude'].includes(entry.name)) continue;
      files.push(...collectTsFiles(full));
    } else if (entry.name.endsWith('.ts') && !entry.name.endsWith('.test.ts')) {
      files.push(full);
    }
  }
  return files;
}

const sourceFiles = collectTsFiles(SRC_DIR);

describe('cross-platform: no unsafe file:// stripping', () => {
  it('should use fileURLToPath instead of .replace("file://", "")', () => {
    const violations: string[] = [];
    for (const file of sourceFiles) {
      const content = fs.readFileSync(file, 'utf-8');
      const lines = content.split('\n');
      for (let i = 0; i < lines.length; i++) {
        if (
          lines[i].includes(".replace('file://'") ||
          lines[i].includes('.replace("file://"')
        ) {
          violations.push(`${path.relative(SRC_DIR, file)}:${i + 1}`);
        }
      }
    }
    expect(
      violations,
      `Found unsafe file:// stripping (breaks Windows paths). Use fileURLToPath() instead:\n${violations.join('\n')}`,
    ).toEqual([]);
  });
});

describe('cross-platform: no new URL("file://"+path) pattern', () => {
  it('should use pathToFileURL instead of manual file:// URL construction', () => {
    const violations: string[] = [];
    // Pattern: new URL(`file://${...}`) or new URL('file://' + ...)
    const pattern = /new URL\([`'"]file:\/\//;
    for (const file of sourceFiles) {
      const content = fs.readFileSync(file, 'utf-8');
      const lines = content.split('\n');
      for (let i = 0; i < lines.length; i++) {
        if (pattern.test(lines[i])) {
          violations.push(`${path.relative(SRC_DIR, file)}:${i + 1}`);
        }
      }
    }
    expect(
      violations,
      `Found manual file:// URL construction (breaks Windows drive letters). Use pathToFileURL() instead:\n${violations.join('\n')}`,
    ).toEqual([]);
  });
});

describe('cross-platform: no hardcoded /dev/null', () => {
  it('should use os.devNull instead of literal /dev/null in source code', () => {
    const violations: string[] = [];
    for (const file of sourceFiles) {
      const content = fs.readFileSync(file, 'utf-8');
      const lines = content.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        // Skip comments and string literals that are documentation
        if (
          line.trimStart().startsWith('//') ||
          line.trimStart().startsWith('*')
        )
          continue;
        // Match /dev/null in string literals (quotes or backticks)
        if (/['"`]\/dev\/null['"`]/.test(line)) {
          violations.push(`${path.relative(SRC_DIR, file)}:${i + 1}`);
        }
      }
    }
    expect(
      violations,
      `Found hardcoded /dev/null (doesn't exist on Windows). Use os.devNull instead:\n${violations.join('\n')}`,
    ).toEqual([]);
  });
});

describe('cross-platform: no bare SIGKILL/SIGTERM without platform check', () => {
  it('should use platform-aware kill instead of direct signal sends', () => {
    const violations: string[] = [];
    const pattern = /\.kill\(['"]SIG(KILL|TERM)['"]\)/;
    for (const file of sourceFiles) {
      const content = fs.readFileSync(file, 'utf-8');
      const lines = content.split('\n');
      for (let i = 0; i < lines.length; i++) {
        if (!pattern.test(lines[i])) continue;
        // Check if there's a platform check nearby (within 15 lines before)
        const context = lines.slice(Math.max(0, i - 15), i + 1).join('\n');
        if (context.includes('win32') || context.includes('platform')) continue;
        violations.push(`${path.relative(SRC_DIR, file)}:${i + 1}`);
      }
    }
    expect(
      violations,
      `Found signal sends without platform check (SIGKILL/SIGTERM are unsupported on Windows). Use killProcess() helper or add platform branch:\n${violations.join('\n')}`,
    ).toEqual([]);
  });
});
