import { describe, expect, it } from 'vitest';
import {
  NON_MODEL_FACING_FIELDS,
  measureToolResponse,
} from './tool-size-measure.js';

describe('measureToolResponse', () => {
  it('measures string responses as-is (Read/Bash path)', () => {
    const text = 'hello world';
    const { bytes, stripped } = measureToolResponse('Read', text);
    expect(bytes).toBe(Buffer.byteLength(text, 'utf8'));
    expect(stripped).toBe(false);
  });

  it('strips originalFile for Edit and shrinks the measured size', () => {
    const resp = {
      filePath: '/f.ts',
      oldString: 'a',
      newString: 'b',
      originalFile: 'X'.repeat(50_000),
      structuredPatch: [
        {
          oldStart: 1,
          oldLines: 1,
          newStart: 1,
          newLines: 1,
          lines: ['-a', '+b'],
        },
      ],
      userModified: false,
      replaceAll: false,
    };
    const { bytes, stripped } = measureToolResponse('Edit', resp);
    expect(stripped).toBe(true);
    expect(bytes).toBeLessThan(1_000); // 50k originalFile excluded
    // Model-facing fields retained.
    const roundtrip = JSON.stringify({ ...resp, originalFile: undefined });
    expect(bytes).toBeLessThan(Buffer.byteLength(JSON.stringify(resp), 'utf8'));
    expect(roundtrip).toContain('structuredPatch');
  });

  it('strips originalFile AND content for Write', () => {
    const resp = {
      type: 'update',
      filePath: '/f.ts',
      content: 'C'.repeat(40_000),
      originalFile: 'O'.repeat(40_000),
      structuredPatch: [],
    };
    const { bytes, stripped } = measureToolResponse('Write', resp);
    expect(stripped).toBe(true);
    expect(bytes).toBeLessThan(1_000); // both full-file fields excluded
  });

  it('strips original_file AND updated_file for NotebookEdit, keeps new_source', () => {
    const resp = {
      new_source: 'print(1)',
      cell_type: 'code',
      language: 'python',
      edit_mode: 'replace',
      notebook_path: '/n.ipynb',
      original_file: 'N'.repeat(60_000),
      updated_file: 'U'.repeat(60_000),
    };
    const { bytes, stripped } = measureToolResponse('NotebookEdit', resp);
    expect(stripped).toBe(true);
    expect(bytes).toBeLessThan(1_000);
  });

  it('does NOT strip model-facing content on non-denylisted tools', () => {
    // A Read-like result carries a model-facing `content` field — must be kept.
    const resp = { filePath: '/f.ts', content: 'Z'.repeat(10_000) };
    const { bytes, stripped } = measureToolResponse('Read', resp);
    expect(stripped).toBe(false);
    expect(bytes).toBe(Buffer.byteLength(JSON.stringify(resp), 'utf8'));
  });

  it('marks stripped=false when a denylisted tool lacks the fields', () => {
    const resp = { filePath: '/f.ts', structuredPatch: [] };
    const { stripped } = measureToolResponse('Edit', resp);
    expect(stripped).toBe(false);
  });

  it('handles null/undefined without throwing', () => {
    expect(() => measureToolResponse('Edit', null)).not.toThrow();
    expect(() => measureToolResponse('Read', undefined)).not.toThrow();
    expect(measureToolResponse('Edit', null).stripped).toBe(false);
  });

  it('does not mutate the input object', () => {
    const resp = { originalFile: 'x', structuredPatch: [] };
    measureToolResponse('Edit', resp);
    expect(resp.originalFile).toBe('x'); // shallow-copied, not deleted in place
  });

  it('denylist covers exactly the allowed file-mutation tools', () => {
    expect(Object.keys(NON_MODEL_FACING_FIELDS).sort()).toEqual([
      'Edit',
      'NotebookEdit',
      'Write',
    ]);
  });
});
