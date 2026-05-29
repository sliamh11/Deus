/**
 * Tests for ipc-protocol.ts Zod schemas.
 *
 * Covers:
 *   - ContainerOutputSchema: valid input, invalid JSON, schema mismatches
 *   - IpcMessageFileSchema: valid messages, schema mismatches
 */

import { describe, it, expect } from 'vitest';
import { ContainerOutputSchema, IpcMessageFileSchema } from './ipc-protocol.js';

// ── ContainerOutputSchema ───────────────────────────────────────────────────

describe('ContainerOutputSchema', () => {
  it('accepts a minimal valid ContainerOutput', () => {
    const result = ContainerOutputSchema.safeParse({
      status: 'success',
      result: 'hello',
    });
    expect(result.success).toBe(true);
  });

  it('accepts a null result (error path)', () => {
    const result = ContainerOutputSchema.safeParse({
      status: 'error',
      result: null,
      error: 'Something went wrong',
    });
    expect(result.success).toBe(true);
  });

  it('accepts a fully-populated ContainerOutput', () => {
    const result = ContainerOutputSchema.safeParse({
      status: 'success',
      result: 'Done',
      newSessionId: 'sess-123',
      newSessionRef: {
        backend: 'claude',
        session_id: 'sess-123',
        resume_cursor: 'cursor-abc',
      },
      prUrl: 'https://github.com/org/repo/pull/42',
      contextStats: {
        tokens: 50000,
        limit: 200000,
        pct: 25,
        warn: false,
        autoCompact: false,
      },
      compactionEvent: {
        trigger: 'auto',
        preTokens: 190000,
        summary: 'Compacted 100 turns',
      },
    });
    expect(result.success).toBe(true);
  });

  // (a) invalid JSON string: JSON.parse would throw before safeParse, but we
  // test safeParse on the parsed-but-wrong-typed value to verify schema rejection
  it('(a) rejects a value parsed from schema-mismatched JSON (no required fields)', () => {
    // Simulate: JSON.parse('{"foo":"bar"}') → { foo: 'bar' }
    const parsed: unknown = { foo: 'bar' };
    const result = ContainerOutputSchema.safeParse(parsed);
    expect(result.success).toBe(false);
  });

  // (b) valid JSON but schema-mismatched: status not in enum
  it('(b) rejects valid-JSON but schema-mismatched input (invalid status enum)', () => {
    const parsed: unknown = { status: 'pending', result: null };
    const result = ContainerOutputSchema.safeParse(parsed);
    expect(result.success).toBe(false);
  });

  it('throws via .parse() on schema mismatch', () => {
    expect(() =>
      ContainerOutputSchema.parse({ notStatus: 'oops', result: null }),
    ).toThrow();
  });

  it('throws via .parse() when status is missing', () => {
    expect(() => ContainerOutputSchema.parse({ result: 'hello' })).toThrow();
  });
});

// ── IpcMessageFileSchema ────────────────────────────────────────────────────

describe('IpcMessageFileSchema', () => {
  it('accepts a valid message IPC file', () => {
    const result = IpcMessageFileSchema.safeParse({
      type: 'message',
      chatJid: '123@g.us',
      text: 'Hello',
    });
    expect(result.success).toBe(true);
  });

  it('accepts a message with only type (other optional fields absent)', () => {
    const result = IpcMessageFileSchema.safeParse({ type: 'schedule_task' });
    expect(result.success).toBe(true);
  });

  // (a) invalid JSON string test: safeParse on non-object returns failure
  it('(a) rejects a non-object value (as would result from bad JSON)', () => {
    const result = IpcMessageFileSchema.safeParse(null);
    expect(result.success).toBe(false);
  });

  // (b) valid JSON but schema-mismatched: missing `type` field
  it('(b) rejects valid-JSON but schema-mismatched object (missing type field)', () => {
    const result = IpcMessageFileSchema.safeParse({
      chatJid: '123@g.us',
      text: 'Hello',
    });
    expect(result.success).toBe(false);
  });
});
