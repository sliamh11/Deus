/**
 * Startup validation gate for Deus.
 *
 * Validates prerequisites before heavy initialization (DB, channels, subsystems).
 * Uses a check registry so new checks can be added without modifying this file.
 *
 * Three severity levels:
 *   fatal   — blocks startup (e.g., missing API credentials)
 *   warn    — allows startup, prints warning (e.g., memory vault not configured)
 *   suggest — allows startup, one-line hint (e.g., Gemini API key, channels)
 */

import {
  hasApiCredentials,
  hasGeminiApiKey,
  hasMemoryVault,
  hasPythonDeps,
  hasAnyChannelAuth,
  countRegisteredGroups,
} from './checks.js';
import { logger } from './logger.js';

// ── Check Registry ──────────────────────────────────────────────────────────

export interface StartupCheck {
  name: string;
  level: 'fatal' | 'warn' | 'suggest';
  run: () => CheckResult;
}

export interface CheckResult {
  name: string;
  level: 'fatal' | 'warn' | 'suggest';
  ok: boolean;
  hint: string;
}

const checks: StartupCheck[] = [];

export function registerStartupCheck(check: StartupCheck): void {
  checks.push(check);
}

// ── Built-in Checks ─────────────────────────────────────────────────────────

registerStartupCheck({
  name: 'API credentials',
  level: 'fatal',
  run: () => ({
    name: 'API credentials',
    level: 'fatal',
    ok: hasApiCredentials(),
    hint: 'Set ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN in .env (or run: deus auth)',
  }),
});

registerStartupCheck({
  name: 'Memory vault',
  level: 'warn',
  run: () => {
    const result = hasMemoryVault();
    let hint: string;
    if (!result.path) {
      hint =
        'Not configured. Run /setup → "memory" in Claude Code to set up your vault.';
    } else {
      hint = `Path ${result.path} does not exist. Create it or update ~/.config/deus/config.json`;
    }
    return { name: 'Memory vault', level: 'warn', ok: result.ok, hint };
  },
});

registerStartupCheck({
  name: 'Python + memory deps',
  level: 'warn',
  run: () => {
    const result = hasPythonDeps();
    return {
      name: 'Python + memory deps',
      level: 'warn',
      ok: result.ok,
      hint: result.missing.includes('python3')
        ? 'Python 3.11+ not found. Install it to enable the memory system.'
        : `Missing: ${result.missing.join(', ')}. Run: pip install ${result.missing.join(' ')}`,
    };
  },
});

registerStartupCheck({
  name: 'Gemini API key',
  level: 'suggest',
  run: () => ({
    name: 'Gemini API key',
    level: 'suggest',
    ok: hasGeminiApiKey(),
    hint: 'Memory search disabled. Get a free key at https://aistudio.google.com/apikey → add to .env',
  }),
});

registerStartupCheck({
  name: 'Channels',
  level: 'suggest',
  run: () => ({
    name: 'Channels',
    level: 'suggest',
    ok: hasAnyChannelAuth(),
    hint: 'No messaging channels configured. Run /add-whatsapp or /add-telegram when ready.',
  }),
});

registerStartupCheck({
  name: 'Registered groups',
  level: 'suggest',
  run: () => ({
    name: 'Registered groups',
    level: 'suggest',
    ok: countRegisteredGroups() > 0,
    hint: 'No groups registered — messages will be ignored. Run /setup → "register" to add one.',
  }),
});

// ── Runner ──────────────────────────────────────────────────────────────────

export interface StartupCheckReport {
  fatals: CheckResult[];
  warnings: CheckResult[];
  suggestions: CheckResult[];
  passed: CheckResult[];
}

export function runStartupChecks(): StartupCheckReport {
  const report: StartupCheckReport = {
    fatals: [],
    warnings: [],
    suggestions: [],
    passed: [],
  };

  for (const check of checks) {
    const result = check.run();
    if (result.ok) {
      report.passed.push(result);
    } else {
      switch (result.level) {
        case 'fatal':
          report.fatals.push(result);
          break;
        case 'warn':
          report.warnings.push(result);
          break;
        case 'suggest':
          report.suggestions.push(result);
          break;
      }
    }
  }

  return report;
}

// ── Output Formatting ───────────────────────────────────────────────────────

const ICON = { fatal: '✗', warn: '⚠', suggest: '○', pass: '✓' } as const;
const INNER = 64; // visible text width between ║ borders
const BORDER_TOP = '╔' + '═'.repeat(INNER + 2) + '╗';
const BORDER_BOT = '╚' + '═'.repeat(INNER + 2) + '╝';

function pad(text: string): string {
  const truncated = text.length > INNER ? text.slice(0, INNER - 1) + '…' : text;
  const padding = Math.max(0, INNER - truncated.length);
  return '║ ' + truncated + ' '.repeat(padding) + ' ║';
}

function emptyLine(): string {
  return '║' + ' '.repeat(INNER + 2) + '║';
}

/** Word-wrap a hint string into lines that fit the box. */
function wrapHint(hint: string, indent: number): string[] {
  const maxLen = INNER - indent;
  if (hint.length <= maxLen) return [hint];
  const words = hint.split(' ');
  const lines: string[] = [];
  let current = '';
  for (const word of words) {
    if (current && current.length + 1 + word.length > maxLen) {
      lines.push(current);
      current = word;
    } else {
      current = current ? current + ' ' + word : word;
    }
  }
  if (current) lines.push(current);
  return lines;
}

function formatResult(r: CheckResult): string[] {
  const icon = r.ok ? ICON.pass : ICON[r.level];
  if (r.ok) {
    return [`${icon} ${r.name.padEnd(22)} OK`];
  }
  const lines = [`${icon} ${r.name}`];
  const prefix = '  → ';
  const wrapped = wrapHint(r.hint, prefix.length);
  lines.push(`${prefix}${wrapped[0]}`);
  for (let i = 1; i < wrapped.length; i++) {
    lines.push(`    ${wrapped[i]}`);
  }
  return lines;
}

export function printStartupReport(report: StartupCheckReport): void {
  const allResults = [
    ...report.passed,
    ...report.fatals,
    ...report.warnings,
    ...report.suggestions,
  ];

  // If everything passes, print nothing (clean startup)
  if (
    report.fatals.length === 0 &&
    report.warnings.length === 0 &&
    report.suggestions.length === 0
  ) {
    return;
  }

  const hasFatals = report.fatals.length > 0;
  const title = hasFatals ? 'Deus startup check FAILED' : 'Deus startup check';

  const lines: string[] = [BORDER_TOP, pad(title), emptyLine()];

  for (const r of allResults) {
    for (const line of formatResult(r)) {
      lines.push(pad(line));
    }
  }

  lines.push(emptyLine());

  if (hasFatals) {
    lines.push(pad(`${report.fatals.length} fatal error(s) — cannot start.`));
    lines.push(pad('Run /setup in Claude Code to get started.'));
  } else {
    lines.push(pad('Deus is running with limited functionality.'));
    lines.push(pad('Run /setup in Claude Code for full setup.'));
  }

  lines.push(BORDER_BOT);

  for (const line of lines) {
    logger.error(line);
  }
}
