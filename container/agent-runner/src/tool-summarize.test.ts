import { describe, expect, it } from 'vitest';

import { summarizeToolResult, type ToolOutput } from './tool-summarize.js';

function makeOutput(
  overrides: Partial<ToolOutput> & { stdout: string },
): ToolOutput {
  return { stderr: '', exitCode: 0, ...overrides };
}

function padTo(s: string, minLen: number): string {
  if (s.length >= minLen) return s;
  return s + '\n' + 'x'.repeat(minLen - s.length - 1);
}

describe('summarizeToolResult', () => {
  it('passes through unchanged when stdout < 10K chars', () => {
    const input = makeOutput({ command: 'find .', stdout: 'a\nb\nc' });
    const result = summarizeToolResult(input);
    expect(result.stdout).toBe('a\nb\nc');
    expect(result.exitCode).toBe(0);
  });

  it('clamps stderr to 20K chars', () => {
    const input = makeOutput({ stdout: 'ok', stderr: 'e'.repeat(25_000) });
    const result = summarizeToolResult(input);
    expect(result.stderr.length).toBe(20_000);
  });

  describe('non-zero exit code (tail-heavy)', () => {
    it('keeps last 200 lines for failed commands', () => {
      const lines = Array.from({ length: 500 }, (_, i) => `line-${i}-${'z'.repeat(20)}`);
      const input = makeOutput({
        command: 'npm install',
        stdout: lines.join('\n'),
        exitCode: 1,
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('showing last 200 of 500 lines');
      expect(result.stdout).toContain('line-499');
      expect(result.stdout).not.toContain('line-0\n');
    });

    it('keeps all lines when fewer than 200', () => {
      const lines = Array.from({ length: 50 }, (_, i) => `err-${i}`);
      const stdout = lines.join('\n');
      const padded = 'x'.repeat(10_001) + '\n' + stdout;
      const input = makeOutput({ command: 'make', stdout: padded, exitCode: 2 });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('err-49');
    });

    it('takes priority over command-specific strategies', () => {
      const lines = Array.from({ length: 300 }, (_, i) => `/path/file-${i}-${'z'.repeat(40)}`);
      const input = makeOutput({
        command: 'find .',
        stdout: lines.join('\n'),
        exitCode: 1,
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('showing last 200');
      expect(result.stdout).not.toContain('showing 50 of');
    });
  });

  describe('git log strategy', () => {
    it('keeps first 10 + last 5 commits, omits middle', () => {
      const commits = Array.from({ length: 30 }, (_, i) => {
        const hash = `${'a'.repeat(39)}${i.toString().padStart(1, '0')}`;
        return `commit ${hash}\nAuthor: Dev <dev@example.com>\nDate: 2026-01-01 12:00:00 +0000\n\n    message ${i}\n    ${'detail '.repeat(50)}`;
      });
      const input = makeOutput({
        command: 'git log',
        stdout: commits.join('\n'),
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('message 0');
      expect(result.stdout).toContain('message 9');
      expect(result.stdout).toContain('message 29');
      expect(result.stdout).toContain('commits omitted');
      expect(result.stdout).not.toContain('message 15');
    });

    it('passes through git log with --oneline', () => {
      const lines = Array.from({ length: 100 }, (_, i) => `abc${i} msg ${i}`);
      const stdout = lines.join('\n');
      const input = makeOutput({
        command: 'git log --oneline',
        stdout,
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).not.toContain('omitted');
    });

    it('does not trigger for piped git log', () => {
      const lines = Array.from({ length: 200 }, (_, i) => `line-${i}`);
      const input = makeOutput({
        command: 'git log | grep fix',
        stdout: lines.join('\n'),
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).not.toContain('commits omitted');
    });
  });

  describe('find strategy', () => {
    it('keeps first 50 results and shows total count', () => {
      const lines = Array.from(
        { length: 200 },
        (_, i) => `/path/to/some/deeply/nested/directory/structure/file-${i}.typescript`,
      );
      const input = makeOutput({
        command: 'find . -name "*.ts"',
        stdout: lines.join('\n'),
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('file-0');
      expect(result.stdout).toContain('file-49');
      expect(result.stdout).not.toContain('file-50\n');
      expect(result.stdout).toContain('showing 50 of 200 results');
    });

    it('also works for fd command', () => {
      const lines = Array.from({ length: 200 }, (_, i) => `/some/very/long/deeply/nested/path/structure/to/file-${i}.rustlang.source`);
      const input = makeOutput({
        command: 'fd .rs',
        stdout: lines.join('\n'),
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('showing 50 of 200 results');
    });
  });

  describe('npm ls / pip list strategy', () => {
    it('keeps first 30 packages for npm ls', () => {
      const lines = Array.from(
        { length: 60 },
        (_, i) => `├── @scope/really-long-package-name-${i}@1.0.0-beta.${i} ${'extra'.repeat(30)}`,
      );
      const input = makeOutput({
        command: 'npm ls',
        stdout: lines.join('\n'),
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('package-name-0');
      expect(result.stdout).toContain('package-name-29');
      expect(result.stdout).toContain('30 more packages');
    });

    it('keeps first 30 for pip list', () => {
      const lines = Array.from(
        { length: 50 },
        (_, i) => `really-long-python-package-name-${i}   1.0.0 ${'info'.repeat(40)}`,
      );
      const input = makeOutput({
        command: 'pip list',
        stdout: lines.join('\n'),
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('20 more packages');
    });
  });

  describe('cat strategy', () => {
    it('keeps first 100 + last 20 lines when > 200 lines', () => {
      const lines = Array.from({ length: 500 }, (_, i) => `line-${i}-${'content'.repeat(5)}`);
      const input = makeOutput({
        command: 'cat largefile.txt',
        stdout: lines.join('\n'),
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('line-0');
      expect(result.stdout).toContain('line-99');
      expect(result.stdout).toContain('line-499');
      expect(result.stdout).toContain('lines omitted');
      expect(result.stdout).not.toContain('line-150\n');
    });
  });

  describe('ls -R / tree strategy', () => {
    it('truncates deep directory entries for tree', () => {
      const shallow: string[] = ['.', '├── src', '│   ├── index.ts'];
      const deep: string[] = [];
      for (let i = 0; i < 200; i++) {
        deep.push(`│                   └── deeply-nested-file-with-long-name-${i}.typescript`);
      }
      const stdout = [...shallow, ...deep].join('\n');
      const input = makeOutput({ command: 'tree', stdout });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('deeper entries omitted');
    });
  });

  describe('default strategy', () => {
    it('head-slices at 60K for unknown commands over limit', () => {
      const big = 'x'.repeat(70_000);
      const input = makeOutput({ command: 'some-tool', stdout: big });
      const result = summarizeToolResult(input);
      expect(result.stdout.length).toBe(60_000);
    });

    it('passes through unknown commands under 60K', () => {
      const medium = 'y'.repeat(15_000);
      const input = makeOutput({ command: 'some-tool', stdout: medium });
      const result = summarizeToolResult(input);
      expect(result.stdout.length).toBe(15_000);
    });
  });

  describe('piped commands', () => {
    it('uses default strategy for piped commands', () => {
      const lines = Array.from({ length: 200 }, (_, i) => `/file-${i}`);
      const input = makeOutput({
        command: 'find . | head -100',
        stdout: lines.join('\n'),
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).not.toContain('showing 50 of');
    });
  });

  describe('runProgram interface', () => {
    it('works with program + args instead of command', () => {
      const lines = Array.from({ length: 200 }, (_, i) => `/some/very/long/deeply/nested/directory/path/to/file-${i}.typescript`);
      const input = makeOutput({
        program: 'find',
        args: ['.', '-name', '*.ts'],
        stdout: lines.join('\n'),
      });
      const result = summarizeToolResult(input);
      expect(result.stdout).toContain('showing 50 of 200 results');
    });
  });
});
