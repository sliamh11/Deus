import { afterEach, describe, expect, it } from 'vitest';
import {
  READ_OVERSIZE_NUDGE,
  ReadOversizeNudgeTracker,
  createReadOversizeNudgeHook,
  readOversizeMaxNudges,
  readOversizeThresholdBytes,
} from './read-oversize-nudge.js';

const THRESHOLD_ENV = 'DEUS_READ_OVERSIZE_THRESHOLD_BYTES';
const MAX_ENV = 'DEUS_READ_OVERSIZE_NUDGE_MAX';

afterEach(() => {
  delete process.env[THRESHOLD_ENV];
  delete process.env[MAX_ENV];
});

describe('readOversizeThresholdBytes', () => {
  it('returns the default when the env is unset', () => {
    expect(readOversizeThresholdBytes()).toBe(20_000);
  });

  it('returns a valid positive override', () => {
    process.env[THRESHOLD_ENV] = '5000';
    expect(readOversizeThresholdBytes()).toBe(5000);
  });

  it.each(['not-a-number', '0', '-5'])(
    'falls back to the default on invalid value %s',
    (value) => {
      process.env[THRESHOLD_ENV] = value;
      expect(readOversizeThresholdBytes()).toBe(20_000);
    },
  );
});

describe('readOversizeMaxNudges', () => {
  it('returns the default when the env is unset', () => {
    expect(readOversizeMaxNudges()).toBe(3);
  });

  it('returns a valid positive override', () => {
    process.env[MAX_ENV] = '1';
    expect(readOversizeMaxNudges()).toBe(1);
  });

  it.each(['abc', '0', '-1'])(
    'falls back to the default on invalid value %s',
    (value) => {
      process.env[MAX_ENV] = value;
      expect(readOversizeMaxNudges()).toBe(3);
    },
  );
});

describe('ReadOversizeNudgeTracker', () => {
  it('nudges once per key, never on repeat', () => {
    const tracker = new ReadOversizeNudgeTracker(3);
    expect(tracker.shouldNudge('/a.ts')).toBe(true);
    expect(tracker.shouldNudge('/a.ts')).toBe(false);
  });

  it('stops after the cap even for brand-new keys', () => {
    const tracker = new ReadOversizeNudgeTracker(2);
    expect(tracker.shouldNudge('/a.ts')).toBe(true);
    expect(tracker.shouldNudge('/b.ts')).toBe(true);
    expect(tracker.shouldNudge('/c.ts')).toBe(false);
  });
});

function readEvent(overrides: Record<string, unknown> = {}) {
  return {
    tool_name: 'Read',
    tool_input: { file_path: '/workspace/project/big.ts' },
    tool_response: 'x'.repeat(100),
    tool_use_id: 'tu_1',
    ...overrides,
  };
}

// The hook's second positional arg pins the threshold so these tests are
// independent of process.env.
describe('createReadOversizeNudgeHook', () => {
  const opts = { signal: new AbortController().signal };

  it('ignores non-Read tools regardless of size', async () => {
    const hook = createReadOversizeNudgeHook(
      new ReadOversizeNudgeTracker(),
      10,
    );
    const out = await hook(
      readEvent({
        tool_name: 'Bash',
        tool_response: 'x'.repeat(10_000),
      }) as never,
      undefined,
      opts,
    );
    expect(out).toEqual({});
  });

  it('is silent below the threshold', async () => {
    const hook = createReadOversizeNudgeHook(
      new ReadOversizeNudgeTracker(),
      101,
    );
    const out = await hook(readEvent() as never, undefined, opts);
    expect(out).toEqual({});
  });

  it('fires exactly at the threshold, not one byte under', async () => {
    const at = createReadOversizeNudgeHook(new ReadOversizeNudgeTracker(), 100);
    const under = createReadOversizeNudgeHook(
      new ReadOversizeNudgeTracker(),
      101,
    );
    const fired = await at(readEvent() as never, undefined, opts);
    const silent = await under(readEvent() as never, undefined, opts);
    expect(fired).toEqual({
      hookSpecificOutput: {
        hookEventName: 'PostToolUse',
        additionalContext: READ_OVERSIZE_NUDGE,
      },
    });
    expect(silent).toEqual({});
  });

  it('dedups repeat oversize reads of the same file', async () => {
    const hook = createReadOversizeNudgeHook(
      new ReadOversizeNudgeTracker(),
      10,
    );
    const first = await hook(readEvent() as never, undefined, opts);
    const second = await hook(readEvent() as never, undefined, opts);
    expect(first).not.toEqual({});
    expect(second).toEqual({});
  });

  it('fires for distinct files until the cap, then stops', async () => {
    const hook = createReadOversizeNudgeHook(
      new ReadOversizeNudgeTracker(2),
      10,
    );
    const a = await hook(
      readEvent({ tool_input: { file_path: '/a.ts' } }) as never,
      undefined,
      opts,
    );
    const b = await hook(
      readEvent({ tool_input: { file_path: '/b.ts' } }) as never,
      undefined,
      opts,
    );
    const c = await hook(
      readEvent({ tool_input: { file_path: '/c.ts' } }) as never,
      undefined,
      opts,
    );
    expect(a).not.toEqual({});
    expect(b).not.toEqual({});
    expect(c).toEqual({});
  });

  it('falls back to tool_use_id when tool_input is malformed, without throwing', async () => {
    const hook = createReadOversizeNudgeHook(
      new ReadOversizeNudgeTracker(),
      10,
    );
    const first = await hook(
      readEvent({ tool_input: undefined }) as never,
      undefined,
      opts,
    );
    const second = await hook(
      readEvent({ tool_input: undefined }) as never,
      undefined,
      opts,
    );
    expect(first).not.toEqual({});
    expect(second).toEqual({}); // same tool_use_id key dedups
  });
});
