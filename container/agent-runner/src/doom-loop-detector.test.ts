import { describe, expect, it, beforeEach } from 'vitest';

import {
  DoomLoopDetector,
  createDoomLoopHook,
  normalizeArgs,
} from './doom-loop-detector.js';

describe('DoomLoopDetector', () => {
  let detector: DoomLoopDetector;

  beforeEach(() => {
    detector = new DoomLoopDetector(3);
  });

  it('detects on the 3rd consecutive identical failure', () => {
    const call = {
      toolName: 'Bash',
      normalizedArgs: 'ls /nonexistent',
      exitCode: 1,
      succeeded: false,
    };
    expect(detector.record(call).detected).toBe(false);
    expect(detector.record(call).detected).toBe(false);
    const result = detector.record(call);
    expect(result.detected).toBe(true);
    expect(result.repeatCount).toBe(3);
  });

  it('success resets streak so 2 failures + 1 success + 2 failures does not detect', () => {
    const fail = { toolName: 'Bash', normalizedArgs: 'ls', exitCode: 1, succeeded: false };
    const success = { toolName: 'Bash', normalizedArgs: 'ls', exitCode: 0, succeeded: true };
    detector.record(fail);
    detector.record(fail);
    detector.record(success);
    expect(detector.record(fail).detected).toBe(false);
    expect(detector.record(fail).detected).toBe(false);
  });

  it('3 failures of A then 3 failures of B triggers two detections', () => {
    const callA = { toolName: 'Bash', normalizedArgs: 'cmd-a', exitCode: 1, succeeded: false };
    const callB = { toolName: 'Bash', normalizedArgs: 'cmd-b', exitCode: 1, succeeded: false };

    detector.record(callA);
    detector.record(callA);
    const firstDetection = detector.record(callA);
    expect(firstDetection.detected).toBe(true);

    detector.record(callB);
    detector.record(callB);
    const secondDetection = detector.record(callB);
    expect(secondDetection.detected).toBe(true);
  });

  it('threshold=2 detects on the 2nd failure', () => {
    const d = new DoomLoopDetector(2);
    const call = { toolName: 'Read', normalizedArgs: '/a/b', exitCode: 1, succeeded: false };
    expect(d.record(call).detected).toBe(false);
    expect(d.record(call).detected).toBe(true);
  });

  it('different tool names produce different keys so no detection', () => {
    const args = 'same-args';
    detector.record({ toolName: 'Bash', normalizedArgs: args, exitCode: 1, succeeded: false });
    detector.record({ toolName: 'Read', normalizedArgs: args, exitCode: 1, succeeded: false });
    const result = detector.record({ toolName: 'Write', normalizedArgs: args, exitCode: 1, succeeded: false });
    expect(result.detected).toBe(false);
  });

  it('reset() clears state and re-arms the detector', () => {
    const call = { toolName: 'Bash', normalizedArgs: 'fail', exitCode: 1, succeeded: false };
    detector.record(call);
    detector.record(call);
    detector.reset();
    expect(detector.record(call).detected).toBe(false);
    expect(detector.record(call).detected).toBe(false);
    expect(detector.record(call).detected).toBe(true);
  });

  it('alternating failures reset each other, no detection', () => {
    const callA = { toolName: 'Bash', normalizedArgs: 'cmd-a', exitCode: 1, succeeded: false };
    const callB = { toolName: 'Bash', normalizedArgs: 'cmd-b', exitCode: 1, succeeded: false };
    for (let i = 0; i < 4; i++) {
      expect(detector.record(callA).detected).toBe(false);
      expect(detector.record(callB).detected).toBe(false);
    }
  });
});

describe('normalizeArgs', () => {
  it('normalizes Bash commands: lowercase, whitespace collapse, 100 char limit', () => {
    const input = { command: '  LS   -LA   /TMP  ' };
    expect(normalizeArgs('Bash', input)).toBe('ls -la /tmp');
  });

  it('truncates Bash command to 100 chars', () => {
    const long = 'x'.repeat(200);
    const input = { command: long };
    expect(normalizeArgs('Bash', input).length).toBe(100);
  });

  it('uses file_path for Read', () => {
    expect(normalizeArgs('Read', { file_path: '/some/file.ts' })).toBe('/some/file.ts');
  });

  it('uses file_path for Write', () => {
    expect(normalizeArgs('Write', { file_path: '/out/file.js' })).toBe('/out/file.js');
  });

  it('uses file_path for Edit', () => {
    expect(normalizeArgs('Edit', { file_path: '/edit/me.ts' })).toBe('/edit/me.ts');
  });

  it('falls back to JSON.stringify for unknown tools', () => {
    const input = { query: 'something' };
    expect(normalizeArgs('WebSearch', input)).toBe(JSON.stringify(input).slice(0, 100));
  });
});

describe('createDoomLoopHook', () => {
  it('PostToolUse with success returns empty object', async () => {
    const detector = new DoomLoopDetector(3);
    const hook = createDoomLoopHook(detector);
    const input = {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'ls' },
      tool_response: { exitCode: 0 },
      tool_use_id: 'abc',
      session_id: 's1',
    };
    const result = await hook(input as Parameters<typeof hook>[0], undefined, {} as Parameters<typeof hook>[2]);
    expect(result).toEqual({});
  });

  it('PostToolUse with 3rd consecutive failure returns additionalContext', async () => {
    const detector = new DoomLoopDetector(3);
    const hook = createDoomLoopHook(detector);
    const input = {
      hook_event_name: 'PostToolUse',
      tool_name: 'Bash',
      tool_input: { command: 'fail cmd' },
      tool_response: { exitCode: 1 },
      tool_use_id: 'abc',
      session_id: 's1',
    };
    await hook(input as Parameters<typeof hook>[0], undefined, {} as Parameters<typeof hook>[2]);
    await hook(input as Parameters<typeof hook>[0], undefined, {} as Parameters<typeof hook>[2]);
    const result = await hook(input as Parameters<typeof hook>[0], undefined, {} as Parameters<typeof hook>[2]);
    expect(result).toHaveProperty('hookSpecificOutput.hookEventName', 'PostToolUse');
    expect(result).toHaveProperty('hookSpecificOutput.additionalContext');
    const output = result as { hookSpecificOutput: { additionalContext: string } };
    expect(output.hookSpecificOutput.additionalContext).toContain('[LOOP DETECTED]');
  });

  it('PostToolUseFailure always records as failure', async () => {
    const detector = new DoomLoopDetector(2);
    const hook = createDoomLoopHook(detector);
    const input = {
      hook_event_name: 'PostToolUseFailure',
      tool_name: 'Read',
      tool_input: { file_path: '/missing.ts' },
      error: 'File not found',
      tool_use_id: 'xyz',
      session_id: 's1',
    };
    const first = await hook(input as Parameters<typeof hook>[0], undefined, {} as Parameters<typeof hook>[2]);
    expect(first).toEqual({});
    const second = await hook(input as Parameters<typeof hook>[0], undefined, {} as Parameters<typeof hook>[2]);
    expect(second).toHaveProperty('hookSpecificOutput.hookEventName', 'PostToolUseFailure');
  });
});
