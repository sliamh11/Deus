export interface ToolOutput {
  command?: string;
  program?: string;
  args?: string[];
  stdout: string;
  stderr: string;
  exitCode: number;
}

export interface SummarizedOutput {
  stdout: string;
  stderr: string;
  exitCode: number;
}

const STDOUT_THRESHOLD = 10_000;
const STDOUT_MAX = 60_000;
const STDERR_MAX = 20_000;

function commandStr(input: ToolOutput): string {
  if (input.command !== undefined) return input.command;
  const parts = [input.program ?? '', ...(input.args ?? [])];
  return parts.join(' ');
}

function hasPipe(cmd: string): boolean {
  let inSingle = false;
  let inDouble = false;
  for (let i = 0; i < cmd.length; i++) {
    const ch = cmd[i];
    if (ch === "'" && !inDouble) { inSingle = !inSingle; continue; }
    if (ch === '"' && !inSingle) { inDouble = !inDouble; continue; }
    if (!inSingle && !inDouble && ch === '|') return true;
  }
  return false;
}

function clampStderr(s: string): string {
  return s.length > STDERR_MAX ? s.slice(0, STDERR_MAX) : s;
}

function out(input: ToolOutput, stdout: string): SummarizedOutput {
  return { stdout, stderr: clampStderr(input.stderr), exitCode: input.exitCode };
}

interface Strategy {
  test: (input: ToolOutput, cmd: string) => boolean;
  apply: (input: ToolOutput, cmd: string) => SummarizedOutput;
}

const strategies: Strategy[] = [
  // Non-zero exit: tail-heavy — errors tend to appear at the end
  {
    test: (input) => input.exitCode !== 0,
    apply: (input) => {
      const lines = input.stdout.split('\n');
      const kept = lines.length > 200 ? lines.slice(-200) : lines;
      const prefix =
        lines.length > 200
          ? `[... showing last 200 of ${lines.length} lines]\n`
          : '';
      return out(input, prefix + kept.join('\n'));
    },
  },

  // git log (no --oneline): keep first 10 + last 5 commits
  {
    test: (_input, cmd) =>
      /(?:^|\s)git\s+log\b/.test(cmd) &&
      !cmd.includes('--oneline') &&
      !hasPipe(cmd),
    apply: (input) => {
      const raw = input.stdout;
      const entries: string[] = [];
      let current = '';
      for (const line of raw.split('\n')) {
        if (/^commit [0-9a-f]{40}/.test(line) && current) {
          entries.push(current);
          current = line;
        } else {
          current = current ? current + '\n' + line : line;
        }
      }
      if (current) entries.push(current);

      if (entries.length <= 15) return out(input, raw);
      const omitted = entries.length - 15;
      const marker = `\n[... ${omitted} commits omitted ...]\n`;
      const result =
        entries.slice(0, 10).join('\n') +
        marker +
        entries.slice(-5).join('\n');
      return out(input, result);
    },
  },

  // find / fd: keep first 50 lines
  {
    test: (_input, cmd) =>
      /(?:^|\s)(?:find|fd)\b/.test(cmd) && !hasPipe(cmd),
    apply: (input) => {
      const lines = input.stdout.split('\n');
      if (lines.length <= 50) return out(input, input.stdout);
      const kept = lines.slice(0, 50).join('\n');
      return out(input, `${kept}\n[... showing 50 of ${lines.length} results]`);
    },
  },

  // ls -R / tree: keep first 3 depth levels
  {
    test: (_input, cmd) =>
      (/(?:^|\s)ls\s+.*-[a-zA-Z]*R/.test(cmd) ||
        /(?:^|\s)tree\b/.test(cmd)) &&
      !hasPipe(cmd),
    apply: (input) => {
      const lines = input.stdout.split('\n');
      const shallow: string[] = [];
      const deeper: string[] = [];
      for (const line of lines) {
        const prefix = line.match(/^([\s│├└─┬┤┼|]*)/)?.[1].length ?? 0;
        const depth = Math.floor(prefix / 4) + (line.includes('/') ? line.split('/').length - 1 : 0);
        if (depth <= 3) {
          shallow.push(line);
        } else {
          deeper.push(line);
        }
      }
      if (deeper.length === 0) return out(input, input.stdout);
      return out(
        input,
        shallow.join('\n') +
          `\n[... ${deeper.length} deeper entries omitted]`,
      );
    },
  },

  // npm ls / pip list / pip freeze: keep first 30 lines
  {
    test: (_input, cmd) =>
      (/(?:^|\s)npm\s+ls\b/.test(cmd) ||
        /(?:^|\s)pip\s+(?:list|freeze)\b/.test(cmd)) &&
      !hasPipe(cmd),
    apply: (input) => {
      const lines = input.stdout.split('\n');
      if (lines.length <= 30) return out(input, input.stdout);
      const kept = lines.slice(0, 30).join('\n');
      const more = lines.length - 30;
      return out(input, `${kept}\n[... ${more} more packages]`);
    },
  },

  // cat: when output > 200 lines, keep first 100 + last 20
  {
    test: (input, cmd) => {
      if (!/(?:^|\s)cat\b/.test(cmd) || hasPipe(cmd)) return false;
      return input.stdout.split('\n').length > 200;
    },
    apply: (input) => {
      const lines = input.stdout.split('\n');
      const head = lines.slice(0, 100);
      const tail = lines.slice(-20);
      const omitted = lines.length - 120;
      return out(
        input,
        head.join('\n') +
          `\n[... ${omitted} lines omitted ...]\n` +
          tail.join('\n'),
      );
    },
  },

  // Default over 60K: head-slice
  {
    test: (input) => input.stdout.length > STDOUT_MAX,
    apply: (input) => out(input, input.stdout.slice(0, STDOUT_MAX)),
  },
];

export function summarizeToolResult(input: ToolOutput): SummarizedOutput {
  if (input.stdout.length <= STDOUT_THRESHOLD) {
    return {
      stdout: input.stdout,
      stderr: clampStderr(input.stderr),
      exitCode: input.exitCode,
    };
  }

  const cmd = commandStr(input);
  for (const strategy of strategies) {
    if (strategy.test(input, cmd)) {
      return strategy.apply(input, cmd);
    }
  }

  // Under 60K, no specific strategy matched: passthrough with stderr clamp
  return out(input, input.stdout);
}
