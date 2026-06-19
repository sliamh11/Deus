/**
 * Oracle tests for Ingress Gateway Phase 3 — R6 per-event audit
 * (structural whitelist scrub + secret redaction + append-only JSONL writer).
 *
 * Authored from the SPEC (the public contract in the LIA-315 Phase 3 issue),
 * BEFORE any implementation exists (oracle-author warden). The module under
 * test (`./audit.js`) DOES NOT EXIST YET, so these tests are RED: the import
 * fails to resolve / the symbols are absent. They must go GREEN only once the
 * implementer adds `src/ingress/audit.ts` to the EXACT contract below — never
 * by weakening a case here.
 *
 * Independence note: written blind to any implementation. Every expected value
 * traces to the spec, not to chosen code.
 *
 * Every test is tagged `@oracle` so the commit-side oracle-integrity gate can
 * protect it from silent weakening.
 *
 * Determinism: writer day-partition is driven by an EXPLICIT event.ts value;
 * the audit dir is a fresh os.tmpdir() mkdtemp dir, cleaned up after.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { mkdtemp, rm, readFile, readdir, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  scrubAuditEvent,
  appendAuditEvent,
  type IngressAuditEvent,
} from './audit.js';

// ---------------------------------------------------------------------------
// Planted secrets — these must NEVER survive scrub/redaction into retained
// string fields nor onto disk. Each is a recognisable secret SHAPE per spec.
// ---------------------------------------------------------------------------
const SECRET_SHA256 = 'sha256=deadbeefdeadbeefdeadbeefdeadbeefdeadbeef'; // sha256=<40 hex>
const SECRET_HEX40 = 'a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0'; // 40-char hex token
const SECRET_BEARER = 'Bearer abc123ABC456def789GHI012jkl345MNO678'; // Bearer <token>

// Deterministic UTC-day timestamps for the writer partition assertions.
const DAY_D_TS = Date.UTC(2026, 0, 15, 9, 30, 0); // 2026-01-15
const DAY_D_TS_2 = Date.UTC(2026, 0, 15, 18, 45, 0); // same UTC day, later
const DAY_NEXT_TS = Date.UTC(2026, 0, 16, 1, 0, 0); // 2026-01-16 — next UTC day
const DAY_D_STAMP = '2026-01-15';
const DAY_NEXT_STAMP = '2026-01-16';

let auditDir: string;

beforeEach(async () => {
  auditDir = await mkdtemp(join(tmpdir(), 'ingress-audit-oracle-'));
});

afterEach(async () => {
  await rm(auditDir, { recursive: true, force: true });
});

// ===========================================================================
// CASE 7 — scrubAuditEvent: secret redaction CONTENT in RETAINED string fields
// ===========================================================================
describe('@oracle scrubAuditEvent — redacts secret-shaped substrings in retained fields', () => {
  it('@oracle replaces each planted secret with the literal [REDACTED] and removes the raw secret', () => {
    // @oracle: spec R6 — redact secret-shaped substrings by REPLACING with '[REDACTED]' (never silent omission)
    const raw = {
      ts: DAY_D_TS,
      source: `telegram:${SECRET_HEX40}`, // secret embedded in a retained field
      event: 'rejected',
      decision: 'rejected',
      // reason is a retained string field; plant all three secret shapes.
      reason: `auth failed token=${SECRET_BEARER} digest=${SECRET_SHA256}`,
    };

    const out = scrubAuditEvent(raw);

    const serialized = JSON.stringify(out);
    // Raw secrets must NOT appear anywhere in the scrubbed event.
    expect(serialized).not.toContain(SECRET_HEX40);
    expect(serialized).not.toContain(SECRET_BEARER);
    expect(serialized).not.toContain(SECRET_SHA256);
    // The hex portion of the sha256 digest must also be gone.
    expect(serialized).not.toContain(
      'deadbeefdeadbeefdeadbeefdeadbeefdeadbeef',
    );

    // Redaction is by REPLACEMENT — the marker must be present (not silent drop)
    // in BOTH retained string fields that carried a secret.
    expect(out.reason).toContain('[REDACTED]');
    expect(out.source).toContain('[REDACTED]');
  });
});

// ===========================================================================
// CASE 8 — scrubAuditEvent: structural whitelist (drops unknown/unsafe keys)
// ===========================================================================
describe('@oracle scrubAuditEvent — structural whitelist drops unsafe keys', () => {
  it('@oracle returns an event WITHOUT body/headers/authorization/token keys', () => {
    // @oracle: spec R6 — whitelist scrub drops unknown/unsafe keys (body, headers, token, ...)
    const raw = {
      ts: DAY_D_TS,
      source: 'telegram',
      event: 'received',
      // Disallowed/unsafe keys that must NOT survive the whitelist.
      body: { secret: 'super-sensitive-payload' },
      headers: { authorization: SECRET_BEARER },
      authorization: SECRET_BEARER,
      token: SECRET_HEX40,
    };

    const out = scrubAuditEvent(raw) as Record<string, unknown>;

    expect(Object.prototype.hasOwnProperty.call(out, 'body')).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(out, 'headers')).toBe(false);
    expect(Object.prototype.hasOwnProperty.call(out, 'authorization')).toBe(
      false,
    );
    expect(Object.prototype.hasOwnProperty.call(out, 'token')).toBe(false);

    // And as a defense-in-depth check, the raw payload string is nowhere.
    expect(JSON.stringify(out)).not.toContain('super-sensitive-payload');
  });
});

// ===========================================================================
// CASE 9 — scrubAuditEvent: NEVER throws on maximally-malformed input
// ===========================================================================
describe('@oracle scrubAuditEvent — never throws on malformed input', () => {
  it('@oracle returns an object for undefined input (does not throw)', () => {
    // @oracle: spec R6 — scrub never throws even on maximally-malformed input
    let out: unknown;
    expect(() => {
      out = scrubAuditEvent(undefined);
    }).not.toThrow();
    expect(typeof out).toBe('object');
    expect(out).not.toBeNull();
  });

  it('@oracle returns an object for non-string fields / missing ts (does not throw)', () => {
    // @oracle: spec R6 — scrub tolerates wrong-typed/missing fields without throwing
    const malformed = {
      // ts missing entirely
      source: 12345, // non-string
      reason: { nested: 'object-not-a-string' }, // non-string
      event: ['array', 'not', 'string'],
    };
    let out: unknown;
    expect(() => {
      out = scrubAuditEvent(malformed);
    }).not.toThrow();
    expect(typeof out).toBe('object');
    expect(out).not.toBeNull();
  });
});

// ===========================================================================
// CASE 10 — appendAuditEvent: end-to-end + scrub-on-write
// ===========================================================================
describe('@oracle appendAuditEvent — scrubs before writing, append-only JSONL', () => {
  it('@oracle writes a scrubbed, newline-terminated JSON line to the UTC-day file with no raw secret', async () => {
    // @oracle: spec R6 — scrub-on-write; day-partitioned JSONL; no raw secret on disk
    const event = {
      ts: DAY_D_TS,
      source: 'telegram',
      event: 'rejected',
      decision: 'rejected',
      reason: `blocked token=${SECRET_BEARER}`,
      // disallowed field that must be scrubbed away before hitting disk.
      body: 'raw-request-body-should-never-persist',
    } as unknown as IngressAuditEvent;

    const result = await appendAuditEvent(event, { auditDir });
    expect(result.ok).toBe(true);

    // (a) The file lives at the event.ts UTC-day partition path.
    const expectedFile = join(auditDir, `ingress-audit-${DAY_D_STAMP}.jsonl`);
    const contents = await readFile(expectedFile, 'utf8');

    // (b) The raw secret must NOT be present in the file bytes.
    expect(contents).not.toContain(SECRET_BEARER);
    // (c) The disallowed body field must be absent from disk.
    expect(contents).not.toContain('raw-request-body-should-never-persist');

    // (d) The line parses as valid JSON and is newline-terminated (append-only).
    expect(contents.endsWith('\n')).toBe(true);
    const lines = contents.split('\n').filter((l) => l.length > 0);
    expect(lines).toHaveLength(1);
    const parsed = JSON.parse(lines[0]);
    expect(parsed.source).toBe('telegram');
    expect(parsed.event).toBe('rejected');
  });
});

// ===========================================================================
// CASE 11 — appendAuditEvent: append-only ordering + day partition
// ===========================================================================
describe('@oracle appendAuditEvent — append-only + per-UTC-day partitioning', () => {
  it('@oracle two same-day events land in one file in order; a different-day event lands in a different file', async () => {
    // @oracle: spec R6 — O_APPEND ordering within a day; separate file per UTC day
    const e1 = {
      ts: DAY_D_TS,
      source: 'telegram',
      event: 'received',
    } as unknown as IngressAuditEvent;
    const e2 = {
      ts: DAY_D_TS_2,
      source: 'whatsapp',
      event: 'admitted',
      decision: 'admitted',
    } as unknown as IngressAuditEvent;
    const e3 = {
      ts: DAY_NEXT_TS,
      source: 'slack',
      event: 'dispatched',
    } as unknown as IngressAuditEvent;

    expect((await appendAuditEvent(e1, { auditDir })).ok).toBe(true);
    expect((await appendAuditEvent(e2, { auditDir })).ok).toBe(true);
    expect((await appendAuditEvent(e3, { auditDir })).ok).toBe(true);

    // Same UTC day -> exactly two lines, in append order.
    const dayFile = join(auditDir, `ingress-audit-${DAY_D_STAMP}.jsonl`);
    const dayContents = await readFile(dayFile, 'utf8');
    const dayLines = dayContents.split('\n').filter((l) => l.length > 0);
    expect(dayLines).toHaveLength(2);
    expect(JSON.parse(dayLines[0]).source).toBe('telegram'); // e1 first
    expect(JSON.parse(dayLines[1]).source).toBe('whatsapp'); // e2 second

    // Different UTC day -> a DISTINCT day-stamped file with the third event.
    const nextFile = join(auditDir, `ingress-audit-${DAY_NEXT_STAMP}.jsonl`);
    const nextContents = await readFile(nextFile, 'utf8');
    const nextLines = nextContents.split('\n').filter((l) => l.length > 0);
    expect(nextLines).toHaveLength(1);
    expect(JSON.parse(nextLines[0]).source).toBe('slack');

    // Exactly two day-partition files were produced (no cross-contamination).
    const files = (await readdir(auditDir)).filter((f) =>
      f.startsWith('ingress-audit-'),
    );
    expect(files.sort()).toEqual(
      [
        `ingress-audit-${DAY_D_STAMP}.jsonl`,
        `ingress-audit-${DAY_NEXT_STAMP}.jsonl`,
      ].sort(),
    );
  });
});

// ===========================================================================
// CASE 12 — appendAuditEvent: surfaces failure as {ok:false} (never throws)
// ===========================================================================
describe('@oracle appendAuditEvent — surfaces write failure without throwing', () => {
  it('@oracle returns {ok:false} when auditDir cannot be created (parent is a FILE)', async () => {
    // @oracle: spec R6 — writer surfaces failure as {ok:false}; never throws into caller
    // Make the PARENT of the intended auditDir a regular FILE so mkdir -p fails.
    const blocker = join(auditDir, 'blocker-file');
    await writeFile(blocker, 'i am a file, not a directory');
    // auditDir now points UNDER a file -> mkdir/append must fail.
    const badAuditDir = join(blocker, 'nested', 'audit');

    const event = {
      ts: DAY_D_TS,
      source: 'telegram',
      event: 'received',
    } as unknown as IngressAuditEvent;

    let result: { ok: boolean } | undefined;
    await expect(
      (async () => {
        result = await appendAuditEvent(event, { auditDir: badAuditDir });
      })(),
    ).resolves.toBeUndefined(); // i.e. it did NOT throw/reject

    expect(result).toBeDefined();
    expect(result!.ok).toBe(false);
  });
});
