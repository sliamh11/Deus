// Unit tests for the R6 audit sink — edge cases NOT covered by the blind oracle
// (ingress-audit-phase3.oracle.test.ts). The oracle owns redaction CONTENT,
// whitelist, no-throw, day-partition, and failure-surfacing; these cover numeric
// retention, dir auto-create, concurrent appends, and multi-secret-per-field.

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, readFile, readdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { scrubAuditEvent, appendAuditEvent } from './audit.js';

const TS = Date.UTC(2026, 0, 15, 9, 30, 0); // 2026-01-15
const STAMP = '2026-01-15';

let auditDir: string;
beforeEach(async () => {
  auditDir = await mkdtemp(join(tmpdir(), 'ingress-audit-unit-'));
});
afterEach(async () => {
  await rm(auditDir, { recursive: true, force: true });
});

describe('scrubAuditEvent retention + coercion', () => {
  it('retains a numeric status and event/decision enums', () => {
    const out = scrubAuditEvent({
      ts: TS,
      source: 'github',
      event: 'admitted',
      decision: 'admitted',
      status: 200,
    });
    expect(out.ts).toBe(TS);
    expect(out.source).toBe('github');
    expect(out.event).toBe('admitted');
    expect(out.decision).toBe('admitted');
    expect(out.status).toBe(200);
  });

  it('coerces a missing/non-string source to empty and missing ts to 0', () => {
    const out = scrubAuditEvent({ event: 'received' });
    expect(out.source).toBe('');
    expect(out.ts).toBe(0);
    expect(out.event).toBe('received');
  });

  it('redacts MULTIPLE distinct secrets in a single field', () => {
    const out = scrubAuditEvent({
      ts: TS,
      source: 'x',
      event: 'rejected',
      reason:
        'a=Bearer tok_abcdefabcdefabcdef b=0123456789abcdef0123456789abcdef',
    });
    expect(out.reason).not.toContain('tok_abcdefabcdefabcdef');
    expect(out.reason).not.toContain('0123456789abcdef0123456789abcdef');
    // Both replaced with the marker.
    expect(
      (out.reason!.match(/\[REDACTED\]/g) ?? []).length,
    ).toBeGreaterThanOrEqual(2);
  });
});

describe('appendAuditEvent filesystem behavior', () => {
  it('auto-creates a missing (nested) audit directory', async () => {
    const nested = join(auditDir, 'deep', 'nested', 'audit');
    const res = await appendAuditEvent(
      { ts: TS, source: 'github', event: 'received' },
      { auditDir: nested },
    );
    expect(res.ok).toBe(true);
    const files = await readdir(nested);
    expect(files).toContain(`ingress-audit-${STAMP}.jsonl`);
  });

  it('concurrent appends to the same day all land (append-only, no truncation)', async () => {
    const n = 12;
    const results = await Promise.all(
      Array.from({ length: n }, (_, i) =>
        appendAuditEvent(
          { ts: TS, source: `s${i}`, event: 'received' },
          { auditDir },
        ),
      ),
    );
    expect(results.every((r) => r.ok)).toBe(true);
    const file = join(auditDir, `ingress-audit-${STAMP}.jsonl`);
    const lines = (await readFile(file, 'utf8'))
      .split('\n')
      .filter((l) => l.length > 0);
    expect(lines).toHaveLength(n);
    // Every line is independently valid JSON (no interleaved/torn writes).
    const sources = lines.map((l) => JSON.parse(l).source).sort();
    expect(sources).toEqual(
      Array.from({ length: n }, (_, i) => `s${i}`).sort(),
    );
  });
});
