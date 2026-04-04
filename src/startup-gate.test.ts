import { describe, it, expect, beforeEach, vi } from 'vitest';

// Mock the checks module entirely — startup-gate just orchestrates them
vi.mock('./checks.js', () => ({
  hasApiCredentials: vi.fn(() => false),
  hasGeminiApiKey: vi.fn(() => false),
  hasMemoryVault: vi.fn(() => ({ ok: false, path: null })),
  hasPythonDeps: vi.fn(() => ({ ok: false, missing: ['python3'] })),
  hasAnyChannelAuth: vi.fn(() => false),
  hasContainerImage: vi.fn(() => false),
  countRegisteredGroups: vi.fn(() => 0),
}));

vi.mock('./logger.js', () => ({
  logger: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
}));

import {
  hasApiCredentials,
  hasGeminiApiKey,
  hasMemoryVault,
  hasPythonDeps,
  hasAnyChannelAuth,
  hasContainerImage,
  countRegisteredGroups,
} from './checks.js';
import {
  runStartupChecks,
  registerStartupCheck,
  printStartupReport,
  pad,
  emptyLine,
  wrapHint,
  formatResult,
  StartupCheck,
  CheckResult,
  StartupCheckReport,
} from './startup-gate.js';
import { logger } from './logger.js';

const mockHasApiCredentials = vi.mocked(hasApiCredentials);
const mockHasGeminiApiKey = vi.mocked(hasGeminiApiKey);
const mockHasMemoryVault = vi.mocked(hasMemoryVault);
const mockHasPythonDeps = vi.mocked(hasPythonDeps);
const mockHasAnyChannelAuth = vi.mocked(hasAnyChannelAuth);
const mockHasContainerImage = vi.mocked(hasContainerImage);
const mockCountRegisteredGroups = vi.mocked(countRegisteredGroups);

beforeEach(() => {
  // Reset all check mocks to failing defaults
  mockHasApiCredentials.mockReturnValue(false);
  mockHasGeminiApiKey.mockReturnValue(false);
  mockHasMemoryVault.mockReturnValue({ ok: false, path: null });
  mockHasPythonDeps.mockReturnValue({ ok: false, missing: ['python3'] });
  mockHasAnyChannelAuth.mockReturnValue(false);
  mockHasContainerImage.mockReturnValue(false);
  mockCountRegisteredGroups.mockReturnValue(0);
});

// ── runStartupChecks ──────────────────────────────────────────────────────

describe('runStartupChecks', () => {
  it('returns fatal when API credentials are missing', () => {
    mockHasApiCredentials.mockReturnValue(false);
    const report = runStartupChecks();
    const fatalNames = report.fatals.map((r) => r.name);
    expect(fatalNames).toContain('API credentials');
  });

  it('does not put API credentials in fatals when configured', () => {
    mockHasApiCredentials.mockReturnValue(true);
    const report = runStartupChecks();
    const passedNames = report.passed.map((r) => r.name);
    expect(passedNames).toContain('API credentials');
  });

  it('puts Memory vault in warnings when not configured', () => {
    mockHasApiCredentials.mockReturnValue(true);
    mockHasMemoryVault.mockReturnValue({ ok: false, path: null });
    const report = runStartupChecks();
    const warnNames = report.warnings.map((r) => r.name);
    expect(warnNames).toContain('Memory vault');
  });

  it('puts Memory vault in passed when configured and exists', () => {
    mockHasApiCredentials.mockReturnValue(true);
    mockHasMemoryVault.mockReturnValue({ ok: true, path: '/tmp/vault' });
    const report = runStartupChecks();
    const passedNames = report.passed.map((r) => r.name);
    expect(passedNames).toContain('Memory vault');
  });

  it('puts Gemini API key in suggestions when not configured', () => {
    mockHasApiCredentials.mockReturnValue(true);
    const report = runStartupChecks();
    const suggestNames = report.suggestions.map((r) => r.name);
    expect(suggestNames).toContain('Gemini API key');
  });

  it('puts Channels in suggestions when none configured', () => {
    mockHasApiCredentials.mockReturnValue(true);
    const report = runStartupChecks();
    const suggestNames = report.suggestions.map((r) => r.name);
    expect(suggestNames).toContain('Channels');
  });

  it('puts Registered groups in suggestions when none registered', () => {
    mockHasApiCredentials.mockReturnValue(true);
    const report = runStartupChecks();
    const suggestNames = report.suggestions.map((r) => r.name);
    expect(suggestNames).toContain('Registered groups');
  });

  it('shows setup hint when no groups and no channels configured', () => {
    mockHasApiCredentials.mockReturnValue(true);
    mockHasAnyChannelAuth.mockReturnValue(false);
    mockCountRegisteredGroups.mockReturnValue(0);
    const report = runStartupChecks();
    const groupResult = report.suggestions.find(
      (r) => r.name === 'Registered groups',
    );
    expect(groupResult).toBeDefined();
    expect(groupResult!.hint).toContain('/add-whatsapp');
    expect(groupResult!.hint).toContain('/add-telegram');
    expect(groupResult!.hint).not.toContain('messages will be ignored');
  });

  it('shows channel-connected hint when channels exist but no groups registered', () => {
    mockHasApiCredentials.mockReturnValue(true);
    mockHasAnyChannelAuth.mockReturnValue(true);
    mockCountRegisteredGroups.mockReturnValue(0);
    const report = runStartupChecks();
    const groupResult = report.suggestions.find(
      (r) => r.name === 'Registered groups',
    );
    expect(groupResult).toBeDefined();
    expect(groupResult!.hint).toContain('Channel connected');
    expect(groupResult!.hint).toContain('/setup');
  });

  it('returns all passed when everything is healthy', () => {
    mockHasApiCredentials.mockReturnValue(true);
    mockHasGeminiApiKey.mockReturnValue(true);
    mockHasMemoryVault.mockReturnValue({ ok: true, path: '/tmp/vault' });
    mockHasPythonDeps.mockReturnValue({ ok: true, missing: [] });
    mockHasAnyChannelAuth.mockReturnValue(true);
    mockHasContainerImage.mockReturnValue(true);
    mockCountRegisteredGroups.mockReturnValue(2);

    const report = runStartupChecks();
    expect(report.fatals).toHaveLength(0);
    expect(report.warnings).toHaveLength(0);
    expect(report.suggestions).toHaveLength(0);
    expect(report.passed.length).toBeGreaterThan(0);
  });
});

// ── registerStartupCheck ──────────────────────────────────────────────────

describe('registerStartupCheck', () => {
  it('custom registered check appears in the report', () => {
    const customCheck: StartupCheck = {
      name: 'Custom test check',
      level: 'suggest',
      run: () => ({
        name: 'Custom test check',
        level: 'suggest',
        ok: false,
        hint: 'This is a test hint',
      }),
    };

    registerStartupCheck(customCheck);
    const report = runStartupChecks();

    const allResults = [
      ...report.fatals,
      ...report.warnings,
      ...report.suggestions,
      ...report.passed,
    ];
    const found = allResults.find((r) => r.name === 'Custom test check');
    expect(found).toBeDefined();
    expect(found!.hint).toBe('This is a test hint');
  });
});

// ── Output Formatting ─────────────────────────────────────────────────────────

const INNER = 64; // must match startup-gate.ts constant

describe('pad', () => {
  it('short text is padded to fill the box width', () => {
    const result = pad('Hello');
    // format: ║ <text><spaces> ║  → total inner = INNER chars, outer cols = INNER+4
    expect(result).toBe(
      '║ ' + 'Hello' + ' '.repeat(INNER - 'Hello'.length) + ' ║',
    );
  });

  it('text exactly at INNER chars is not truncated', () => {
    const text = 'x'.repeat(INNER);
    const result = pad(text);
    expect(result).toBe('║ ' + text + ' ║');
  });

  it('text longer than INNER chars is truncated with ellipsis', () => {
    const text = 'a'.repeat(INNER + 10);
    const result = pad(text);
    // visible content must be exactly INNER chars wide
    expect(result).toMatch(/^║ .{64} ║$/u);
    expect(result).toContain('…');
    expect(result).not.toContain('a'.repeat(INNER));
  });

  it('truncated output still has the correct total visual width', () => {
    const text = 'b'.repeat(INNER + 5);
    const result = pad(text);
    // strip leading "║ " and trailing " ║" — inner should be INNER chars
    const inner = result.slice(2, result.length - 2);
    expect([...inner].length).toBe(INNER);
  });
});

describe('emptyLine', () => {
  it('returns a box line with only spaces between borders', () => {
    const result = emptyLine();
    expect(result).toBe('║' + ' '.repeat(INNER + 2) + '║');
  });

  it('has the same total width as pad output', () => {
    const padResult = pad('x');
    const emptyResult = emptyLine();
    // Both should have the same character length
    expect([...emptyResult].length).toBe([...padResult].length);
  });
});

describe('wrapHint', () => {
  it('returns single-element array when hint fits in one line', () => {
    const short = 'Short hint';
    const lines = wrapHint(short, 4);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toBe(short);
  });

  it('wraps long text into multiple lines without breaking words', () => {
    // indent=4 means maxLen = INNER - 4 = 60
    const words = Array.from({ length: 20 }, (_, i) => `word${i}`);
    const hint = words.join(' ');
    const lines = wrapHint(hint, 4);
    expect(lines.length).toBeGreaterThan(1);
    // every line must fit within maxLen
    for (const line of lines) {
      expect(line.length).toBeLessThanOrEqual(INNER - 4);
    }
  });

  it('reassembles to the original text when lines are joined', () => {
    const hint =
      'The quick brown fox jumps over the lazy dog and keeps on running across the meadow';
    const lines = wrapHint(hint, 4);
    expect(lines.join(' ')).toBe(hint);
  });

  it('respects a larger indent value (fewer chars per line)', () => {
    const hint = 'aaaa bbbb cccc dddd eeee ffff gggg hhhh iiii jjjj kkkk';
    const linesSmallIndent = wrapHint(hint, 4);
    const linesLargeIndent = wrapHint(hint, 30);
    expect(linesLargeIndent.length).toBeGreaterThanOrEqual(
      linesSmallIndent.length,
    );
  });
});

describe('formatResult', () => {
  it('uses ✓ icon and single-line OK format for a passing result', () => {
    const r: CheckResult = {
      name: 'My check',
      level: 'suggest',
      ok: true,
      hint: '',
    };
    const lines = formatResult(r);
    expect(lines).toHaveLength(1);
    expect(lines[0]).toMatch(/^✓/);
    expect(lines[0]).toContain('OK');
  });

  it('uses ✗ icon for a fatal failure', () => {
    const r: CheckResult = {
      name: 'Fatal check',
      level: 'fatal',
      ok: false,
      hint: 'Fix it now',
    };
    const lines = formatResult(r);
    expect(lines[0]).toMatch(/^✗/);
    expect(lines[0]).toContain('Fatal check');
  });

  it('uses ⚠ icon for a warn failure', () => {
    const r: CheckResult = {
      name: 'Warn check',
      level: 'warn',
      ok: false,
      hint: 'Please fix',
    };
    const lines = formatResult(r);
    expect(lines[0]).toMatch(/^⚠/);
  });

  it('uses ○ icon for a suggest failure', () => {
    const r: CheckResult = {
      name: 'Suggest check',
      level: 'suggest',
      ok: false,
      hint: 'Optional',
    };
    const lines = formatResult(r);
    expect(lines[0]).toMatch(/^○/);
  });

  it('includes the hint on subsequent lines for a failing result', () => {
    const r: CheckResult = {
      name: 'My check',
      level: 'warn',
      ok: false,
      hint: 'Some actionable hint here',
    };
    const lines = formatResult(r);
    expect(lines.length).toBeGreaterThan(1);
    expect(lines[1]).toContain('Some actionable hint here');
    expect(lines[1]).toContain('→');
  });

  it('wraps a very long hint across multiple lines', () => {
    const longHint =
      'This is a very long hint that will definitely exceed the inner box width and should be wrapped across multiple output lines by the wrapHint helper function';
    const r: CheckResult = {
      name: 'Long hint check',
      level: 'warn',
      ok: false,
      hint: longHint,
    };
    const lines = formatResult(r);
    // first line = check name, subsequent lines = wrapped hint
    expect(lines.length).toBeGreaterThan(2);
  });
});

// ── printStartupReport ────────────────────────────────────────────────────────

describe('printStartupReport', () => {
  const mockLogger = vi.mocked(logger);

  beforeEach(() => {
    mockLogger.error.mockClear();
  });

  function makeReport(
    overrides: Partial<StartupCheckReport> = {},
  ): StartupCheckReport {
    return {
      fatals: [],
      warnings: [],
      suggestions: [],
      passed: [],
      ...overrides,
    };
  }

  it('prints nothing when all checks pass (early return)', () => {
    const passed: CheckResult[] = [
      { name: 'API credentials', level: 'fatal', ok: true, hint: '' },
    ];
    printStartupReport(makeReport({ passed }));
    expect(mockLogger.error).not.toHaveBeenCalled();
  });

  it('prints nothing when fatals/warnings/suggestions are all empty even with passed items', () => {
    const passed: CheckResult[] = [
      { name: 'Check A', level: 'suggest', ok: true, hint: '' },
      { name: 'Check B', level: 'warn', ok: true, hint: '' },
    ];
    printStartupReport(makeReport({ passed }));
    expect(mockLogger.error).not.toHaveBeenCalled();
  });

  it('uses "FAILED" in the title when there are fatals', () => {
    const fatals: CheckResult[] = [
      {
        name: 'API credentials',
        level: 'fatal',
        ok: false,
        hint: 'Set your API key',
      },
    ];
    printStartupReport(makeReport({ fatals }));
    const allCalls = mockLogger.error.mock.calls.flat().join('\n');
    expect(allCalls).toContain('FAILED');
  });

  it('includes fatal error count in the footer when there are fatals', () => {
    const fatals: CheckResult[] = [
      {
        name: 'API credentials',
        level: 'fatal',
        ok: false,
        hint: 'Set your API key',
      },
      {
        name: 'Other fatal',
        level: 'fatal',
        ok: false,
        hint: 'Another problem',
      },
    ];
    printStartupReport(makeReport({ fatals }));
    const allCalls = mockLogger.error.mock.calls.flat().join('\n');
    expect(allCalls).toContain('2 fatal error(s)');
    expect(allCalls).toContain('cannot start');
  });

  it('uses "running with limited functionality" footer when only warnings present', () => {
    const warnings: CheckResult[] = [
      {
        name: 'Memory vault',
        level: 'warn',
        ok: false,
        hint: 'Vault not found',
      },
    ];
    printStartupReport(makeReport({ warnings }));
    const allCalls = mockLogger.error.mock.calls.flat().join('\n');
    expect(allCalls).toContain('running with limited functionality');
    expect(allCalls).not.toContain('FAILED');
  });

  it('uses "running with limited functionality" footer when only suggestions present', () => {
    const suggestions: CheckResult[] = [
      {
        name: 'Channels',
        level: 'suggest',
        ok: false,
        hint: 'No channels configured',
      },
    ];
    printStartupReport(makeReport({ suggestions }));
    const allCalls = mockLogger.error.mock.calls.flat().join('\n');
    expect(allCalls).toContain('running with limited functionality');
  });

  it('outputs box borders (╔ top and ╚ bottom)', () => {
    const warnings: CheckResult[] = [
      {
        name: 'Memory vault',
        level: 'warn',
        ok: false,
        hint: 'Vault not found',
      },
    ];
    printStartupReport(makeReport({ warnings }));
    const calls = mockLogger.error.mock.calls.flat();
    expect(calls[0]).toMatch(/^╔/);
    expect(calls[calls.length - 1]).toMatch(/^╚/);
  });

  it('every output line has the same visual width', () => {
    const warnings: CheckResult[] = [
      {
        name: 'Memory vault',
        level: 'warn',
        ok: false,
        hint: 'Vault not found',
      },
    ];
    printStartupReport(makeReport({ warnings }));
    const calls = mockLogger.error.mock.calls.flat();
    const widths = calls.map((line) => [...(line as string)].length);
    const first = widths[0];
    for (const w of widths) {
      expect(w).toBe(first);
    }
  });

  it('includes ✗ icon in output for fatal failures', () => {
    const fatals: CheckResult[] = [
      {
        name: 'API credentials',
        level: 'fatal',
        ok: false,
        hint: 'Set your key',
      },
    ];
    printStartupReport(makeReport({ fatals }));
    const allCalls = mockLogger.error.mock.calls.flat().join('\n');
    expect(allCalls).toContain('✗');
  });

  it('includes ✓ icon for passed checks when mixed with failures', () => {
    const passed: CheckResult[] = [
      { name: 'API credentials', level: 'fatal', ok: true, hint: '' },
    ];
    const suggestions: CheckResult[] = [
      { name: 'Channels', level: 'suggest', ok: false, hint: 'No channels' },
    ];
    printStartupReport(makeReport({ passed, suggestions }));
    const allCalls = mockLogger.error.mock.calls.flat().join('\n');
    expect(allCalls).toContain('✓');
  });

  it('long hints are wrapped and all lines stay within box width', () => {
    const longHint =
      'This is an extremely long hint message that exceeds the inner box width and must be word-wrapped so each line fits inside the decorative box without overflowing';
    const warnings: CheckResult[] = [
      { name: 'Memory vault', level: 'warn', ok: false, hint: longHint },
    ];
    printStartupReport(makeReport({ warnings }));
    const calls = mockLogger.error.mock.calls.flat();
    const widths = calls.map((line) => [...(line as string)].length);
    const expectedWidth = widths[0]; // take from border line
    for (const w of widths) {
      expect(w).toBe(expectedWidth);
    }
  });
});
