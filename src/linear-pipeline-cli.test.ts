import { describe, it, expect } from 'vitest';
import {
  parseDuration,
  formatElapsed,
  elapsedMs,
  computeColumnWidths,
  renderThroughputFooter,
  type TodayStats,
  type GateRevisionCounts,
} from './linear-pipeline-cli.js';

// eslint-disable-next-line no-control-regex
const ANSI_RE = /\x1b\[[0-9;]*m/g;
const stripAnsi = (s: string) => s.replace(ANSI_RE, '');

describe('parseDuration', () => {
  it('parses minutes', () => {
    const result = parseDuration('30m');
    expect(result).toBeTruthy();
    const diff = Date.now() - new Date(result!).getTime();
    expect(diff).toBeGreaterThan(29 * 60_000);
    expect(diff).toBeLessThan(31 * 60_000);
  });

  it('parses hours', () => {
    const result = parseDuration('24h');
    expect(result).toBeTruthy();
    const diff = Date.now() - new Date(result!).getTime();
    expect(diff).toBeGreaterThan(23.9 * 3_600_000);
    expect(diff).toBeLessThan(24.1 * 3_600_000);
  });

  it('parses days', () => {
    const result = parseDuration('7d');
    expect(result).toBeTruthy();
    const diff = Date.now() - new Date(result!).getTime();
    expect(diff).toBeGreaterThan(6.9 * 86_400_000);
    expect(diff).toBeLessThan(7.1 * 86_400_000);
  });

  it('returns null for invalid input', () => {
    expect(parseDuration('abc')).toBeNull();
    expect(parseDuration('24')).toBeNull();
    expect(parseDuration('h')).toBeNull();
    expect(parseDuration('')).toBeNull();
  });
});

describe('formatElapsed', () => {
  it('formats seconds', () => {
    const ts = new Date(Date.now() - 45_000).toISOString();
    expect(formatElapsed(ts)).toBe('45s');
  });

  it('formats minutes', () => {
    const ts = new Date(Date.now() - 5 * 60_000).toISOString();
    expect(formatElapsed(ts)).toBe('5m');
  });

  it('formats hours', () => {
    const ts = new Date(Date.now() - 3 * 3_600_000).toISOString();
    expect(formatElapsed(ts)).toBe('3h');
  });

  it('formats days', () => {
    const ts = new Date(Date.now() - 2 * 86_400_000).toISOString();
    expect(formatElapsed(ts)).toBe('2d');
  });

  it('returns 0s for future timestamps', () => {
    const ts = new Date(Date.now() + 10_000).toISOString();
    expect(formatElapsed(ts)).toBe('0s');
  });
});

describe('elapsedMs', () => {
  it('returns positive ms for past timestamps', () => {
    const ts = new Date(Date.now() - 5000).toISOString();
    const ms = elapsedMs(ts);
    expect(ms).toBeGreaterThanOrEqual(4900);
    expect(ms).toBeLessThan(6000);
  });

  it('returns 0 for future timestamps', () => {
    const ts = new Date(Date.now() + 10_000).toISOString();
    expect(elapsedMs(ts)).toBe(0);
  });

  it('crosses 30m threshold', () => {
    const ts = new Date(Date.now() - 31 * 60_000).toISOString();
    expect(elapsedMs(ts)).toBeGreaterThan(1_800_000);
  });

  it('crosses 2h threshold', () => {
    const ts = new Date(Date.now() - 121 * 60_000).toISOString();
    expect(elapsedMs(ts)).toBeGreaterThan(7_200_000);
  });
});

describe('computeColumnWidths', () => {
  it('returns minimum title width for narrow terminals', () => {
    const { titleWidth } = computeColumnWidths(60);
    expect(titleWidth).toBe(24);
  });

  it('scales title width for wide terminals', () => {
    const { titleWidth } = computeColumnWidths(120);
    expect(titleWidth).toBe(64);
  });

  it('calculates separator width as cols - 2', () => {
    const { separatorWidth } = computeColumnWidths(100);
    expect(separatorWidth).toBe(98);
  });

  it('clamps to min 80 cols', () => {
    const narrow = computeColumnWidths(40);
    const min = computeColumnWidths(80);
    expect(narrow.titleWidth).toBe(min.titleWidth);
  });
});

// ── renderThroughputFooter ────────────────────────────────────────────────────

const baseStats: TodayStats = {
  shipped: 3,
  failed: 1,
  medianAgentMs: 14 * 60_000, // 14m
  automergeFailRate: 33,
};

describe('renderThroughputFooter', () => {
  it('renders line 1 with shipped / failed / median / automerge-fail', () => {
    const lines = renderThroughputFooter(baseStats, {}, 120);
    expect(lines.length).toBeGreaterThanOrEqual(1);
    const plain = lines[0].replace(ANSI_RE, '');
    expect(plain).toContain('3 shipped');
    expect(plain).toContain('1 failed');
    expect(plain).toContain('Median agent 14m');
    expect(plain).toContain('Automerge fail 33%');
  });

  it('renders em-dash when median and automerge-fail are null', () => {
    const stats: TodayStats = {
      shipped: 0,
      failed: 0,
      medianAgentMs: null,
      automergeFailRate: null,
    };
    const lines = renderThroughputFooter(stats, {}, 120);
    const plain = lines[0].replace(ANSI_RE, '');
    expect(plain).toContain('Median agent —');
    expect(plain).toContain('Automerge fail —');
  });

  it('renders gate revision bars on line 2', () => {
    const gates: GateRevisionCounts = {
      'completion-gate': 5,
      'readiness-gate': 2,
      'quality-gate': 1,
    };
    const lines = renderThroughputFooter(baseStats, gates, 120);
    expect(lines.length).toBe(2);
    const plain = lines[1].replace(ANSI_RE, '');
    expect(plain).toContain('Gate revisions');
    expect(plain).toContain('completion-gate');
    expect(plain).toContain('readiness-gate');
    expect(plain).toContain('quality-gate');
  });

  it('highest-count gate gets solid bar (▓), others get light bar (░)', () => {
    const gates: GateRevisionCounts = { top: 10, mid: 5 };
    const lines = renderThroughputFooter(baseStats, gates, 120);
    expect(lines[1]).toContain('▓'); // top gate
    expect(lines[1]).toContain('░'); // mid gate
  });

  it('bar length is proportional to revision count', () => {
    const gates: GateRevisionCounts = { big: 10, small: 2 };
    const lines = renderThroughputFooter(baseStats, gates, 120);
    const plain = lines[1].replace(ANSI_RE, '');
    // big should have more bar chars than small
    const bigMatch = plain.match(/big\s+(▓+)/);
    const smallMatch = plain.match(/small\s+(░+)/);
    expect(bigMatch).not.toBeNull();
    expect(smallMatch).not.toBeNull();
    expect(bigMatch![1].length).toBeGreaterThan(smallMatch![1].length);
  });

  it('omits gate line when no revisions recorded', () => {
    const lines = renderThroughputFooter(baseStats, {}, 120);
    expect(lines.length).toBe(1);
  });

  it('truncates gates that would overflow terminal width', () => {
    // Very narrow terminal — only the first gate should fit
    const gates: GateRevisionCounts = { alpha: 5, beta: 4, gamma: 3, delta: 2 };
    const lines = renderThroughputFooter(baseStats, gates, 30);
    const plain = lines[1].replace(ANSI_RE, '');
    // Should contain "Gate revisions" prefix and at least alpha
    expect(plain).toContain('Gate revisions');
    // beta/gamma/delta may be clipped; no assertion on their presence
    expect(plain.length).toBeLessThanOrEqual(30);
  });

  it('formats median hours correctly', () => {
    const stats: TodayStats = { ...baseStats, medianAgentMs: 90 * 60_000 }; // 90m = 1h30m
    const lines = renderThroughputFooter(stats, {}, 120);
    const plain = lines[0].replace(ANSI_RE, '');
    expect(plain).toContain('Median agent 1h30m');
  });

  it('formats exact hours without remainder', () => {
    const stats: TodayStats = { ...baseStats, medianAgentMs: 2 * 3_600_000 }; // exactly 2h
    const lines = renderThroughputFooter(stats, {}, 120);
    const plain = lines[0].replace(ANSI_RE, '');
    expect(plain).toContain('Median agent 2h');
    expect(plain).not.toContain('2h0m');
  });
});
