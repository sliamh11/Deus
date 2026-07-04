import { afterEach, describe, expect, it } from 'vitest';
import {
  MASKED_PLACEHOLDER,
  maskStaleToolResults,
  maskingEnabled,
  maskKeepTurns,
  maskMinBytes,
} from './observation-masking.js';

afterEach(() => {
  delete process.env.DEUS_OBSERVATION_MASKING;
  delete process.env.DEUS_MASK_KEEP_TURNS;
  delete process.env.DEUS_MASK_MIN_BYTES;
});

type Msg = {
  role: 'system' | 'user' | 'assistant' | 'tool';
  content?: string | Array<Record<string, unknown>> | null;
  tool_call_id?: string;
};

const BIG = 'x'.repeat(600);

/** system + N user-turns, each: user → assistant → tool(BIG). */
function conversation(turns: number): Msg[] {
  const msgs: Msg[] = [{ role: 'system', content: 'sys' }];
  for (let i = 0; i < turns; i++) {
    msgs.push({ role: 'user', content: `q${i}` });
    msgs.push({ role: 'assistant', content: `a${i}` });
    msgs.push({ role: 'tool', tool_call_id: `t${i}`, content: BIG });
  }
  return msgs;
}

describe('env parsing', () => {
  it('masking is OFF by default (dark ship)', () => {
    expect(maskingEnabled()).toBe(false);
    process.env.DEUS_OBSERVATION_MASKING = '1';
    expect(maskingEnabled()).toBe(true);
  });

  it('keep-turns and min-bytes defaults and overrides', () => {
    expect(maskKeepTurns()).toBe(3);
    expect(maskMinBytes()).toBe(500);
    process.env.DEUS_MASK_KEEP_TURNS = '5';
    process.env.DEUS_MASK_MIN_BYTES = '100';
    expect(maskKeepTurns()).toBe(5);
    expect(maskMinBytes()).toBe(100);
    process.env.DEUS_MASK_KEEP_TURNS = '0';
    expect(maskKeepTurns()).toBe(3); // invalid → default
  });
});

describe('maskStaleToolResults', () => {
  it('masks tool results older than the keep-window, keeps recent ones', () => {
    const msgs = conversation(5);
    const { masked } = maskStaleToolResults(msgs, {
      keepRecentTurns: 3,
      minBytes: 500,
    });
    expect(masked).toBe(2); // turns 0,1 masked; 2,3,4 kept
    expect(msgs[3].content).toBe(MASKED_PLACEHOLDER); // turn 0 tool
    expect(msgs[6].content).toBe(MASKED_PLACEHOLDER); // turn 1 tool
    expect(msgs[9].content).toBe(BIG); // turn 2 tool intact
    expect(msgs[12].content).toBe(BIG);
    expect(msgs[15].content).toBe(BIG);
  });

  it('never masks non-tool messages', () => {
    const msgs = conversation(5);
    maskStaleToolResults(msgs, { keepRecentTurns: 1, minBytes: 1 });
    for (const m of msgs) {
      if (m.role !== 'tool') expect(m.content).not.toBe(MASKED_PLACEHOLDER);
    }
  });

  it('respects minBytes — small results stay', () => {
    const msgs = conversation(5);
    msgs[3].content = 'tiny'; // turn 0 tool result small
    const { masked } = maskStaleToolResults(msgs, {
      keepRecentTurns: 3,
      minBytes: 500,
    });
    expect(masked).toBe(1); // only turn 1's big result
    expect(msgs[3].content).toBe('tiny');
  });

  it('is idempotent — placeholders never re-masked or re-counted', () => {
    const msgs = conversation(5);
    const first = maskStaleToolResults(msgs, {
      keepRecentTurns: 3,
      minBytes: 500,
    });
    const second = maskStaleToolResults(msgs, {
      keepRecentTurns: 3,
      minBytes: 500,
    });
    expect(first.masked).toBe(2);
    expect(second.masked).toBe(0);
  });

  it('skips non-string content untouched (array + null)', () => {
    const msgs = conversation(4);
    const arr = [{ type: 'text', text: BIG }];
    msgs[3].content = arr; // turn 0 tool, array content
    msgs[6].content = null; // turn 1 tool, null content
    const { masked } = maskStaleToolResults(msgs, {
      keepRecentTurns: 1,
      minBytes: 1,
    });
    expect(msgs[3].content).toBe(arr);
    expect(msgs[6].content).toBeNull();
    expect(masked).toBe(1); // only turn 2's string result; turn 3 is kept (last 1)
  });

  it('handles fewer turns than the keep-window as a no-op', () => {
    const msgs = conversation(2);
    const { masked } = maskStaleToolResults(msgs, {
      keepRecentTurns: 3,
      minBytes: 1,
    });
    expect(masked).toBe(0);
  });

  it('fails open on malformed input (no throw, no mutation)', () => {
    const weird = [{ role: 'tool' }, { role: 'tool', content: BIG }] as Msg[];
    expect(() =>
      maskStaleToolResults(weird, { keepRecentTurns: 1, minBytes: 1 }),
    ).not.toThrow();
    // No user turns at all → nothing is "older than the last K user turns".
    expect(weird[1].content).toBe(BIG);
  });
});
