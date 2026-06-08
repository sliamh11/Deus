import { describe, expect, it } from 'vitest';

import { extractToolCallFields } from './tool-call-log.js';

describe('extractToolCallFields', () => {
  it('extracts file_path for Read/Edit/Write', () => {
    for (const name of ['Read', 'Edit', 'Write']) {
      const f = extractToolCallFields(
        name,
        { file_path: '/a/b.ts' },
        undefined,
      );
      expect(f).toEqual({ name, file_path: '/a/b.ts', is_error: false });
    }
  });

  it('extracts notebook_path for NotebookEdit', () => {
    const f = extractToolCallFields(
      'NotebookEdit',
      { notebook_path: '/n.ipynb' },
      undefined,
    );
    expect(f.file_path).toBe('/n.ipynb');
  });

  it('extracts command for Bash', () => {
    const f = extractToolCallFields(
      'Bash',
      { command: 'git status' },
      undefined,
    );
    expect(f).toEqual({ name: 'Bash', command: 'git status', is_error: false });
  });

  it('extracts subagent_type for Agent and Task', () => {
    for (const name of ['Agent', 'Task']) {
      const f = extractToolCallFields(
        name,
        { subagent_type: 'code-reviewer' },
        undefined,
      );
      expect(f.subagent_type).toBe('code-reviewer');
    }
  });

  it('caps long command/file_path at 1024 chars', () => {
    const long = 'x'.repeat(5000);
    expect(
      extractToolCallFields('Bash', { command: long }, undefined).command,
    ).toHaveLength(1024);
    expect(
      extractToolCallFields('Read', { file_path: long }, undefined).file_path,
    ).toHaveLength(1024);
  });

  it('detects is_error from {is_error} and {error} shapes, defaults false', () => {
    expect(
      extractToolCallFields('Bash', { command: 'x' }, { is_error: true })
        .is_error,
    ).toBe(true);
    expect(
      extractToolCallFields('Bash', { command: 'x' }, { error: 'boom' })
        .is_error,
    ).toBe(true);
    expect(
      extractToolCallFields('Bash', { command: 'x' }, 'plain string output')
        .is_error,
    ).toBe(false);
    expect(
      extractToolCallFields('Bash', { command: 'x' }, undefined).is_error,
    ).toBe(false);
    expect(
      extractToolCallFields('Bash', { command: 'x' }, { is_error: false })
        .is_error,
    ).toBe(false);
  });

  it('omits args for unknown tools and tolerates non-object input', () => {
    expect(
      extractToolCallFields('mcp__x__y', { foo: 'bar' }, undefined),
    ).toEqual({
      name: 'mcp__x__y',
      is_error: false,
    });
    expect(extractToolCallFields('Bash', null, undefined)).toEqual({
      name: 'Bash',
      is_error: false,
    });
    // empty-string fields are omitted, not emitted as ''
    expect(
      extractToolCallFields('Read', { file_path: '' }, undefined).file_path,
    ).toBeUndefined();
  });
});
