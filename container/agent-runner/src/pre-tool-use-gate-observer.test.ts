import { describe, it, expect } from 'vitest';
import {
  createBlockingPreToolUseObserver,
  isRecursiveForceRmOutsideWorkspace,
} from './pre-tool-use-gate-observer.js';

describe('createBlockingPreToolUseObserver', () => {
  const observer = createBlockingPreToolUseObserver();

  it('blocks rm -rf targeting outside /workspace', async () => {
    const result = await observer('PreToolUse', {
      tool_name: 'Bash',
      tool_input: { command: 'rm -rf /tmp/deus-delete-me' },
    });
    expect(result.decision).toBe('block');
    expect(String(result.reason)).toMatch(/outside \/workspace/);
  });

  it('allows rm -rf inside /workspace', async () => {
    const result = await observer('PreToolUse', {
      tool_name: 'Bash',
      tool_input: { command: 'rm -rf /workspace/tmp/build' },
    });
    expect(result).toEqual({});
  });

  it('allows non-Bash tools even with a matching command string', async () => {
    const result = await observer('PreToolUse', {
      tool_name: 'Write',
      tool_input: { file_path: '/tmp/foo', content: 'rm -rf /etc' },
    });
    expect(result).toEqual({});
  });

  it('does not block (or throw) on a malformed payload', async () => {
    expect(await observer('PreToolUse', null)).toEqual({});
    expect(await observer('PreToolUse', 'nope')).toEqual({});
    expect(await observer('PreToolUse', { tool_name: 'Bash' })).toEqual({});
    expect(
      await observer('PreToolUse', { tool_name: 'Bash', tool_input: {} }),
    ).toEqual({});
  });
});

describe('isRecursiveForceRmOutsideWorkspace', () => {
  it.each([
    // recursive + force, absolute target outside /workspace → block
    ['rm -rf /tmp/x', true],
    ['rm -fr /etc/foo', true],
    ['rm -r -f /var/data', true],
    ['rm --recursive --force /opt/x', true],
    ['rm -rf /', true],
    ['rm -rf /workspaces/x', true], // sibling dir is genuinely outside /workspace
    // in-workspace → allow
    ['rm -rf /workspace', false],
    ['rm -rf /workspace/tmp/x', false],
    ['rm -rf relative/path', false], // relative → in-workspace assumption
    // not both flags → allow
    ['rm -r /tmp/x', false],
    ['rm -f /tmp/x', false],
    ['rm /tmp/x', false],
    // not rm → allow
    ['ls -la /tmp', false],
    ['', false],
  ])('classifies %j as %s', (cmd, expected) => {
    expect(isRecursiveForceRmOutsideWorkspace(cmd as string)).toBe(expected);
  });
});
