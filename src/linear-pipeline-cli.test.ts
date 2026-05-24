// Tests for linear-pipeline-cli utilities and buildStageBar
import { describe, it, expect } from 'vitest';
import {
  parseDuration,
  formatElapsed,
  elapsedMs,
  computeColumnWidths,
  renderThroughputFooter,
  type TodayStats,
  type GateRevisionCounts,
  buildStageBar,
  type PipelineEvent,
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
    expect(titleWidth).toBe(23);
  });

  it('scales title width for wide terminals', () => {
    const { titleWidth } = computeColumnWidths(120);
    expect(titleWidth).toBe(63);
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

// ── buildStageBar tests ──────────────────────────────────────────────────────

function makeEvent(event_type: string): PipelineEvent {
  return { event_type, created_at: new Date().toISOString() };
}

describe('buildStageBar', () => {
  describe('all-empty (no events)', () => {
    it('returns a 10-char bar with stage 1 as active frontier', () => {
      const bar = buildStageBar([]);
      const plain = stripAnsi(bar);
      // Fixed format: "[ ░····· ]" — 10 display chars
      // Stage 1 is the active frontier (░), stages 2-6 are not-started (·)
      expect(plain).toBe('[ ░····· ]');
      expect([...plain].length).toBe(10);
    });

    it('stage 1 (index 0) is the active frontier (░)', () => {
      const bar = buildStageBar([]);
      const plain = stripAnsi(bar);
      // "[ ░····· ]"  — first stage char should be ░
      // characters: [, ' ', s0, s1, s2, s3, s4, s5, ' ', ]
      const stages = plain.slice(2, 8); // 6 stage chars
      expect(stages[0]).toBe('░');
      expect(stages.slice(1)).toBe('·····');
    });
  });

  describe('mid-pipeline — after agent_started', () => {
    it('stage 1 and 2 complete, stage 3 is frontier', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'), // stage 1 done (scope)
        makeEvent('agent_started'), // stage 2 done
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      const stages = plain.slice(2, 8);
      expect(stages[0]).toBe('▓'); // stage 1 done
      expect(stages[1]).toBe('▓'); // stage 2 done
      expect(stages[2]).toBe('░'); // stage 3 frontier
      expect(stages[3]).toBe('·');
      expect(stages[4]).toBe('·');
      expect(stages[5]).toBe('·');
    });

    it('gate_ship before agent_started counts as scope (stage 1) only', () => {
      const events: PipelineEvent[] = [makeEvent('gate_ship')];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      const stages = plain.slice(2, 8);
      expect(stages[0]).toBe('▓'); // stage 1 complete
      expect(stages[1]).toBe('░'); // stage 2 frontier
    });

    it('agent_completed triggers stage 3 completion', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'),
        makeEvent('agent_started'),
        makeEvent('agent_completed'),
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      const stages = plain.slice(2, 8);
      expect(stages[0]).toBe('▓');
      expect(stages[1]).toBe('▓');
      expect(stages[2]).toBe('▓');
      expect(stages[3]).toBe('░'); // stage 4 frontier
    });

    it('pr_created also triggers stage 3 completion', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'),
        makeEvent('agent_started'),
        makeEvent('pr_created'),
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      const stages = plain.slice(2, 8);
      expect(stages[2]).toBe('▓');
      expect(stages[3]).toBe('░');
    });

    it('first gate_ship after stage 3 completes stage 4', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'),
        makeEvent('agent_started'),
        makeEvent('agent_completed'),
        makeEvent('gate_ship'),
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      const stages = plain.slice(2, 8);
      expect(stages[3]).toBe('▓');
      expect(stages[4]).toBe('░'); // stage 5 frontier
    });

    it('second gate_ship after stage 3 completes stage 5', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'),
        makeEvent('agent_started'),
        makeEvent('agent_completed'),
        makeEvent('gate_ship'),
        makeEvent('gate_ship'),
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      const stages = plain.slice(2, 8);
      expect(stages[4]).toBe('▓');
      expect(stages[5]).toBe('░'); // stage 6 frontier
    });
  });

  describe('fully-complete pipeline', () => {
    it('all 6 stages complete, no frontier', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'), // stage 1
        makeEvent('agent_started'), // stage 2
        makeEvent('agent_completed'), // stage 3
        makeEvent('gate_ship'), // stage 4
        makeEvent('gate_ship'), // stage 5
        makeEvent('automerge_done'), // stage 6
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      expect(plain).toBe('[ ▓▓▓▓▓▓ ]');
      // No ░ (frontier) or · (not-started) should appear
      const stages = plain.slice(2, 8);
      expect(stages).toBe('▓▓▓▓▓▓');
    });

    it('bar is exactly 10 display chars', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'),
        makeEvent('agent_started'),
        makeEvent('agent_completed'),
        makeEvent('gate_ship'),
        makeEvent('gate_ship'),
        makeEvent('automerge_done'),
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      expect([...plain].length).toBe(10);
    });
  });

  describe('REVISE cases — gate_revise coloring', () => {
    it('gate_revise before scope gate_ship marks stage 1 as revised (RED)', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_revise'),
        makeEvent('gate_ship'), // stage 1 — but had a revise before it
      ];
      const bar = buildStageBar(events);
      // Stage 1 should be ▓ in RED, not DIM
      // Check that RED escape code appears before the ▓
      const idx = bar.indexOf('▓');
      const precedingSection = bar.slice(0, idx);
      // RED = \x1b[31m, DIM = \x1b[2m
      expect(precedingSection).toContain('\x1b[31m');
      expect(precedingSection).not.toContain('\x1b[2m▓'); // the ▓ itself shouldn't be dim
    });

    it('gate_revise before stage 4 gate_ship marks stage 4 as revised (RED)', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'), // stage 1
        makeEvent('agent_started'), // stage 2
        makeEvent('agent_completed'), // stage 3
        makeEvent('gate_revise'), // revise before quality gate
        makeEvent('gate_ship'), // stage 4
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      const stages = plain.slice(2, 8);
      // stage 4 is index 3 — should be ▓
      expect(stages[3]).toBe('▓');
      // Check RED color used for stage 4 slot
      // We'll do a more targeted ANSI check
      // Extract the raw text around the 4th stage character
      // A simpler heuristic: RED code (\x1b[31m) must appear in the bar
      expect(bar).toContain('\x1b[31m');
    });

    it('gate_revise before stage 5 gate_ship marks stage 5 as revised (RED)', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'), // stage 1
        makeEvent('agent_started'), // stage 2
        makeEvent('agent_completed'), // stage 3
        makeEvent('gate_ship'), // stage 4 (clean)
        makeEvent('gate_revise'), // revise before completion gate
        makeEvent('gate_ship'), // stage 5
      ];
      const bar = buildStageBar(events);
      expect(bar).toContain('\x1b[31m'); // RED used
      const plain = stripAnsi(bar);
      const stages = plain.slice(2, 8);
      expect(stages[4]).toBe('▓'); // stage 5 completed
    });

    it('gate_revise between stage 4 and 5 does not mark stage 4 as revised', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'), // stage 1
        makeEvent('agent_started'), // stage 2
        makeEvent('agent_completed'), // stage 3
        makeEvent('gate_ship'), // stage 4 — clean (revise comes AFTER)
        makeEvent('gate_revise'), // revise after stage 4 but before stage 5
        makeEvent('gate_ship'), // stage 5 — revised
      ];
      const bar = buildStageBar(events);
      // Stage 4 should be DIM (clean), stage 5 should be RED
      // We check RED is in bar (for stage 5)
      expect(bar).toContain('\x1b[31m');
      // And stage 4 was clean — we verify by counting dim ▓ vs red ▓
      // Stages 1-4 are clean (DIM), stage 5 is RED
      // Count occurrences of \x1b[2m▓ (dim complete) — should be 4
      // eslint-disable-next-line no-control-regex
      const dimCompleteMatches = bar.match(/\x1b\[2m▓/g) ?? [];
      expect(dimCompleteMatches.length).toBe(4);
      // And \x1b[31m▓ (red complete) — should be 1
      // eslint-disable-next-line no-control-regex
      const redCompleteMatches = bar.match(/\x1b\[31m▓/g) ?? [];
      expect(redCompleteMatches.length).toBe(1);
    });

    it('multiple gate_revise events before a gate_ship still only mark that stage red once', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'), // stage 1
        makeEvent('agent_started'), // stage 2
        makeEvent('agent_completed'), // stage 3
        makeEvent('gate_revise'),
        makeEvent('gate_revise'), // multiple revises
        makeEvent('gate_ship'), // stage 4
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      expect(plain).toBe('[ ▓▓▓▓░· ]');
    });

    it('fully-complete pipeline with revise on stage 4 shows RED for that slot only', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'), // stage 1 — clean
        makeEvent('agent_started'), // stage 2
        makeEvent('agent_completed'), // stage 3
        makeEvent('gate_revise'), // causes stage 4 to be RED
        makeEvent('gate_ship'), // stage 4
        makeEvent('gate_ship'), // stage 5 — clean
        makeEvent('automerge_done'), // stage 6
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      expect(plain).toBe('[ ▓▓▓▓▓▓ ]');
      // Exactly one stage in RED
      // eslint-disable-next-line no-control-regex
      const redCompleteMatches = bar.match(/\x1b\[31m▓/g) ?? [];
      expect(redCompleteMatches.length).toBe(1);
      // Remaining 5 stages in DIM
      // eslint-disable-next-line no-control-regex
      const dimCompleteMatches = bar.match(/\x1b\[2m▓/g) ?? [];
      expect(dimCompleteMatches.length).toBe(5);
    });
  });

  describe('edge cases', () => {
    it('events before agent_started that are not gate_ship/gate_revise are ignored for stage mapping', () => {
      const events: PipelineEvent[] = [
        makeEvent('state_changed'),
        makeEvent('agent_dispatched'),
        makeEvent('gate_ship'), // stage 1
        makeEvent('agent_started'), // stage 2
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      const stages = plain.slice(2, 8);
      expect(stages[0]).toBe('▓');
      expect(stages[1]).toBe('▓');
      expect(stages[2]).toBe('░');
    });

    it('no scope gate_ship before agent_started — stage 1 stays as frontier initially', () => {
      // If agent dispatched directly without a scope gate first, stage 1 is not done
      const events: PipelineEvent[] = [makeEvent('agent_started')];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      // stage 1 not done, stage 2 done — frontier is stage 1 still? No:
      // According to spec stage 1 is "first gate_ship before agent_started"
      // agent_started marks stage 2 done, but stage 1 was never done
      // So frontier = stage 1 (first incomplete)
      const stages = plain.slice(2, 8);
      expect(stages[0]).toBe('░'); // stage 1 not done, frontier
      expect(stages[1]).toBe('▓'); // stage 2 IS done (agent_started seen), shown as complete
      expect(stages[2]).toBe('·');
      expect(stages[3]).toBe('·');
    });

    it('pr_created before agent_completed — stage 3 still only done once', () => {
      const events: PipelineEvent[] = [
        makeEvent('gate_ship'),
        makeEvent('agent_started'),
        makeEvent('pr_created'),
        makeEvent('agent_completed'), // redundant — stage 3 already done
        makeEvent('gate_ship'), // this should be stage 4 (1st post-stage3 gate_ship)
      ];
      const bar = buildStageBar(events);
      const plain = stripAnsi(bar);
      const stages = plain.slice(2, 8);
      expect(stages[3]).toBe('▓'); // stage 4 done (not stage 5)
      expect(stages[4]).toBe('░'); // stage 5 frontier
    });

    it('bar always starts with "[ " and ends with " ]"', () => {
      const cases: PipelineEvent[][] = [
        [],
        [makeEvent('gate_ship')],
        [
          makeEvent('gate_ship'),
          makeEvent('agent_started'),
          makeEvent('agent_completed'),
        ],
        [
          makeEvent('gate_ship'),
          makeEvent('agent_started'),
          makeEvent('agent_completed'),
          makeEvent('gate_ship'),
          makeEvent('gate_ship'),
          makeEvent('automerge_done'),
        ],
      ];
      for (const events of cases) {
        const bar = stripAnsi(buildStageBar(events));
        expect(bar.startsWith('[ ')).toBe(true);
        expect(bar.endsWith(' ]')).toBe(true);
      }
    });

    it('active frontier stage uses bright cyan (\\x1b[96m)', () => {
      const bar = buildStageBar([]); // all empty, stage 1 is frontier
      expect(bar).toContain('\x1b[96m░');
    });
  });
});
