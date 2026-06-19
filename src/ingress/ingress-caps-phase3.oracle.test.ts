/**
 * Oracle tests for Ingress Gateway Phase 3 — R5 DoS/spend caps (fail-closed)
 * + R6 pre-dispatch audit gating.
 *
 * Authored from the SPEC (the public contract in the LIA-315 Phase 3 issue),
 * BEFORE any implementation exists (oracle-author warden). The module under
 * test (`./caps.js`) DOES NOT EXIST YET, so these tests are RED: the import
 * fails to resolve / the symbols are absent. They must go GREEN only once the
 * implementer adds `src/ingress/caps.ts` to the EXACT contract below — never
 * by weakening a case here.
 *
 * Independence note: written blind to any implementation. Every expected value
 * traces to the spec, not to chosen code.
 *
 * Every test is tagged `@oracle` so the commit-side oracle-integrity gate can
 * protect it from silent weakening.
 *
 * Determinism: the cap/ledger/limiter logic is driven by an EXPLICIT `now`
 * argument in every assertion — never the real wall clock.
 */

import { describe, it, expect } from 'vitest';
import {
  InflightCap,
  SourceRateLimiter,
  DailySpendLedger,
  createIngressCaps,
  type IngressCapsConfig,
  type AuditWriter,
  type AdmitResult,
} from './caps.js';
import type { AuditWriteResult, IngressAuditEvent } from './audit.js';

// ---------------------------------------------------------------------------
// Deterministic UTC-day timestamps (explicit epoch ms; no wall-clock reliance)
// ---------------------------------------------------------------------------
// Two DISTINCT UTC days, chosen so the day-rollover assertions are unambiguous.
const DAY_D = Date.UTC(2026, 0, 15, 12, 0, 0); // 2026-01-15 12:00:00 UTC
const DAY_D_LATER = Date.UTC(2026, 0, 15, 23, 59, 59); // same UTC day, later
const DAY_NEXT = Date.UTC(2026, 0, 16, 0, 0, 1); // 2026-01-16 — the NEXT UTC day

// ---------------------------------------------------------------------------
// Test doubles for the injected AuditWriter (NOT a mock of the module under
// test — these are caller-supplied dependencies the contract explicitly allows
// so tests can supply succeeding / failing / throwing writers).
// ---------------------------------------------------------------------------

/** Records every event it is asked to append; always succeeds. */
function makeSucceedingWriter(): AuditWriter & { calls: IngressAuditEvent[] } {
  const calls: IngressAuditEvent[] = [];
  return {
    calls,
    async append(event: IngressAuditEvent): Promise<AuditWriteResult> {
      calls.push(event);
      return { ok: true };
    },
  };
}

/** Resolves a {ok:false} failure result (the RESULT-path failure). */
function makeResultFailWriter(): AuditWriter & { calls: IngressAuditEvent[] } {
  const calls: IngressAuditEvent[] = [];
  return {
    calls,
    async append(event: IngressAuditEvent): Promise<AuditWriteResult> {
      calls.push(event);
      return { ok: false, error: new Error('disk full') };
    },
  };
}

/** Rejects/throws (the THROW-path failure — exercises the catch branch). */
function makeThrowingWriter(): AuditWriter & { calls: IngressAuditEvent[] } {
  const calls: IngressAuditEvent[] = [];
  return {
    calls,
    async append(event: IngressAuditEvent): Promise<AuditWriteResult> {
      calls.push(event);
      throw new Error('writer exploded');
    },
  };
}

function baseConfig(over: Partial<IngressCapsConfig> = {}): IngressCapsConfig {
  return {
    maxInflight: 2,
    rateCapacity: 3,
    rateRefillMs: 1000,
    dailySpendLimit: 100,
    ...over,
  };
}

// ===========================================================================
// CASE 1 — InflightCap: fail-closed ceiling + clamped over-release
// ===========================================================================
describe('@oracle InflightCap — fail-closed global in-flight ceiling', () => {
  it('@oracle acquires up to ceiling then returns false (fail-closed)', () => {
    // @oracle: spec R5 — global in-flight ceiling; tryAcquire() === false at cap
    const cap = new InflightCap(2);
    expect(cap.tryAcquire()).toBe(true);
    expect(cap.tryAcquire()).toBe(true);
    expect(cap.inUse()).toBe(2);
    // At ceiling: next acquire MUST fail closed.
    expect(cap.tryAcquire()).toBe(false);
    expect(cap.inUse()).toBe(2);
  });

  it('@oracle release() restores exactly one slot', () => {
    // @oracle: spec R5 — release frees exactly one inflight slot
    const cap = new InflightCap(2);
    cap.tryAcquire();
    cap.tryAcquire();
    expect(cap.tryAcquire()).toBe(false);
    cap.release();
    expect(cap.inUse()).toBe(1);
    // Exactly one slot was freed — one more acquire succeeds, the next fails.
    expect(cap.tryAcquire()).toBe(true);
    expect(cap.tryAcquire()).toBe(false);
  });

  it('@oracle over-release is clamped >= 0 and cannot inflate capacity', () => {
    // @oracle: spec R5 — over-release clamps inUse>=0; cannot raise ceiling
    const cap = new InflightCap(2);
    // Release far more than ever acquired.
    cap.release();
    cap.release();
    cap.release();
    expect(cap.inUse()).toBe(0);
    // Capacity is still exactly the ceiling — never inflated by over-release.
    expect(cap.tryAcquire()).toBe(true);
    expect(cap.tryAcquire()).toBe(true);
    expect(cap.tryAcquire()).toBe(false); // ceiling still 2, not 5
    expect(cap.inUse()).toBe(2);
  });
});

// ===========================================================================
// CASE 2 — SourceRateLimiter: per-source token bucket + lazy refill
// ===========================================================================
describe('@oracle SourceRateLimiter — per-source token bucket', () => {
  it('@oracle drains a source to empty then rejects, isolates other sources, refills lazily', () => {
    // @oracle: spec R5 — per-source bucket: capacity, isolation, lazy refill
    const limiter = new SourceRateLimiter(3, 1000); // capacity 3, 1000ms / token
    const now = DAY_D;

    // Drain source 'A' of its 3 tokens at a FIXED instant.
    expect(limiter.tryConsume('A', now)).toBe(true);
    expect(limiter.tryConsume('A', now)).toBe(true);
    expect(limiter.tryConsume('A', now)).toBe(true);
    // 'A' is now empty at this instant — next consume fails closed.
    expect(limiter.tryConsume('A', now)).toBe(false);

    // SAME instant, DIFFERENT source 'B' — buckets are per-source isolated.
    expect(limiter.tryConsume('B', now)).toBe(true);

    // Advancing now by one refillMs gives 'A' exactly one fresh token.
    expect(limiter.tryConsume('A', now + 1000)).toBe(true);
    // ...and only one — a second consume at the same advanced instant fails.
    expect(limiter.tryConsume('A', now + 1000)).toBe(false);
  });
});

// ===========================================================================
// CASE 3 — DailySpendLedger: hard daily cutoff + UTC-day rollover
// ===========================================================================
describe('@oracle DailySpendLedger — hard daily ceiling with UTC rollover', () => {
  it('@oracle blocks once today total reaches the limit, allows again next UTC day', () => {
    // @oracle: spec R5 — daily hard ceiling; check()===false at limit; UTC rollover resets
    const ledger = new DailySpendLedger(100);
    // Below the limit on day D: allowed.
    expect(ledger.check(DAY_D)).toBe(true);
    ledger.add(60, DAY_D);
    expect(ledger.check(DAY_D)).toBe(true); // 60 < 100
    ledger.add(40, DAY_D_LATER); // total 100 on the SAME UTC day
    // At/above the limit on day D: hard cutoff (today's total < limit is FALSE).
    expect(ledger.check(DAY_D)).toBe(false);
    expect(ledger.check(DAY_D_LATER)).toBe(false);
    // The NEXT UTC day starts fresh — allowed again (rollover).
    expect(ledger.check(DAY_NEXT)).toBe(true);
  });
});

// ===========================================================================
// CASE 4 — Facade fail-closed composition (succeeding writer)
// ===========================================================================
describe('@oracle createIngressCaps — fail-closed admission composition', () => {
  it('@oracle rejects spend-limit WITHOUT consuming an inflight slot', async () => {
    // @oracle: spec R5 — spend.check is first; over-limit => reason 'spend-limit', inUse unchanged
    const writer = makeSucceedingWriter();
    const caps = createIngressCaps(
      baseConfig({ dailySpendLimit: 100 }),
      writer,
    );
    // Drive spend to the limit BEFORE admitting.
    caps.recordSpend(100, DAY_D);
    const before = caps.snapshot().inUse;

    const res: AdmitResult = await caps.tryAdmit({ source: 'A' }, DAY_D);
    expect(res.ok).toBe(false);
    expect(res.ok === false && res.reason).toBe('spend-limit');
    // No inflight slot was consumed (spend gate precedes acquire).
    expect(caps.snapshot().inUse).toBe(before);
  });

  it('@oracle rejects inflight-cap when already at the ceiling', async () => {
    // @oracle: spec R5 — at inflight ceiling => reason 'inflight-cap'
    const writer = makeSucceedingWriter();
    const caps = createIngressCaps(baseConfig({ maxInflight: 1 }), writer);

    // First admit succeeds and occupies the only slot.
    const first = await caps.tryAdmit({ source: 'A' }, DAY_D);
    expect(first.ok).toBe(true);
    expect(caps.snapshot().inUse).toBe(1);

    // Second admit hits the cap and is rejected with 'inflight-cap'.
    const second = await caps.tryAdmit({ source: 'A' }, DAY_D);
    expect(second.ok).toBe(false);
    expect(second.ok === false && second.reason).toBe('inflight-cap');
    expect(caps.snapshot().inUse).toBe(1); // unchanged — no extra slot consumed
  });

  it('@oracle successful admit produces exactly one durable "admitted" audit append and increments inUse', async () => {
    // @oracle: spec R6 — a successful admit ALWAYS produced one durable 'admitted' append
    const writer = makeSucceedingWriter();
    const caps = createIngressCaps(baseConfig(), writer);
    const before = caps.snapshot().inUse;

    const res = await caps.tryAdmit(
      { source: 'A', requestId: 'req-1', path: '/hook' },
      DAY_D,
    );
    expect(res.ok).toBe(true);
    expect(caps.snapshot().inUse).toBe(before + 1);

    // Exactly one 'admitted' event was appended (mandatory pre-dispatch audit).
    const admitted = writer.calls.filter((e) => e.event === 'admitted');
    expect(admitted).toHaveLength(1);
    expect(admitted[0].source).toBe('A');
  });
});

// ===========================================================================
// CASE 5 — Fail-closed audit, RESULT path ({ok:false}) — GPT co-gate case
// ===========================================================================
describe('@oracle createIngressCaps — audit RESULT-failure releases the slot', () => {
  it('@oracle rejects "audit-unavailable" and leaks NO inflight slot when append resolves {ok:false}', async () => {
    // @oracle: spec R6 — {ok:false} append => reject 'audit-unavailable' + release acquired slot (no leak)
    const writer = makeResultFailWriter();
    const caps = createIngressCaps(baseConfig(), writer);
    const before = caps.snapshot().inUse;

    const res = await caps.tryAdmit({ source: 'A' }, DAY_D);
    expect(res.ok).toBe(false);
    expect(res.ok === false && res.reason).toBe('audit-unavailable');
    // The slot acquired during the gate MUST be released — inUse returns to baseline.
    expect(caps.snapshot().inUse).toBe(before);
  });
});

// ===========================================================================
// CASE 6 — Fail-closed audit, THROW path (rejected promise) — Claude co-gate
// DISTINCT from case 5: exercises the catch branch, not the result-check branch.
// ===========================================================================
describe('@oracle createIngressCaps — audit THROW-failure releases the slot', () => {
  it('@oracle rejects "audit-unavailable" and leaks NO inflight slot when append throws/rejects', async () => {
    // @oracle: spec R6 — thrown/rejected append => reject 'audit-unavailable' + release acquired slot (catch branch)
    const writer = makeThrowingWriter();
    const caps = createIngressCaps(baseConfig(), writer);
    const before = caps.snapshot().inUse;

    // Must NOT propagate the throw into the caller — contract: never throws.
    const res = await caps.tryAdmit({ source: 'A' }, DAY_D);
    expect(res.ok).toBe(false);
    expect(res.ok === false && res.reason).toBe('audit-unavailable');
    expect(caps.snapshot().inUse).toBe(before);
  });
});
