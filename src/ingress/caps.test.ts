// Unit tests for the R5 cap primitives + facade — edge cases NOT covered by the
// blind oracle (ingress-caps-phase3.oracle.test.ts). The oracle owns the
// fail-closed security contract; these cover refill arithmetic, accumulation, and
// the rate-limit rejection path.

import { describe, it, expect } from 'vitest';
import {
  InflightCap,
  SourceRateLimiter,
  DailySpendLedger,
  createIngressCaps,
  type IngressCapsConfig,
  type AuditWriter,
} from './caps.js';
import type { AuditWriteResult, IngressAuditEvent } from './audit.js';

function okWriter(): AuditWriter & { calls: IngressAuditEvent[] } {
  const calls: IngressAuditEvent[] = [];
  return {
    calls,
    async append(e: IngressAuditEvent): Promise<AuditWriteResult> {
      calls.push(e);
      return { ok: true };
    },
  };
}

function cfg(over: Partial<IngressCapsConfig> = {}): IngressCapsConfig {
  return {
    maxInflight: 2,
    rateCapacity: 2,
    rateRefillMs: 1000,
    dailySpendLimit: 100,
    ...over,
  };
}

const T0 = Date.UTC(2026, 2, 1, 0, 0, 0); // 2026-03-01 00:00 UTC

describe('InflightCap edge cases', () => {
  it('release below zero is a no-op; capacity stays exactly the ceiling', () => {
    const cap = new InflightCap(1);
    cap.release(); // nothing acquired
    cap.release();
    expect(cap.inUse()).toBe(0);
    expect(cap.tryAcquire()).toBe(true);
    expect(cap.tryAcquire()).toBe(false); // ceiling 1 honored
  });

  it('ceiling of 0 admits nothing (fail-closed)', () => {
    const cap = new InflightCap(0);
    expect(cap.tryAcquire()).toBe(false);
    expect(cap.inUse()).toBe(0);
  });
});

describe('SourceRateLimiter refill arithmetic', () => {
  it('preserves the sub-quantum remainder (no double grant from leftover time)', () => {
    const lim = new SourceRateLimiter(1, 1000); // capacity 1, 1s/token
    expect(lim.tryConsume('s', T0)).toBe(true); // tokens 1 -> 0
    expect(lim.tryConsume('s', T0 + 1500)).toBe(true); // +1 token (floor 1.5)=1
    // Only 500ms of credit remains; another 500ms (total +2000 from T0) tops it up.
    expect(lim.tryConsume('s', T0 + 1999)).toBe(false); // <1 full token since last grant
    expect(lim.tryConsume('s', T0 + 2000)).toBe(true); // now a full token elapsed
  });

  it('caps refill at capacity (no unbounded accumulation over long idle)', () => {
    const lim = new SourceRateLimiter(2, 1000);
    lim.tryConsume('s', T0); // 2 -> 1
    lim.tryConsume('s', T0); // 1 -> 0
    // Idle 1 hour: refill is clamped to capacity (2), not 3600.
    expect(lim.tryConsume('s', T0 + 3_600_000)).toBe(true); // 2 -> 1
    expect(lim.tryConsume('s', T0 + 3_600_000)).toBe(true); // 1 -> 0
    expect(lim.tryConsume('s', T0 + 3_600_000)).toBe(false); // capped, no 3rd
  });

  it('refillMs <= 0 never refills a drained bucket (guarded)', () => {
    const lim = new SourceRateLimiter(1, 0);
    expect(lim.tryConsume('s', T0)).toBe(true);
    expect(lim.tryConsume('s', T0 + 10_000)).toBe(false);
  });
});

describe('DailySpendLedger accumulation', () => {
  it('accumulates across multiple adds within the same UTC day', () => {
    const led = new DailySpendLedger(100);
    led.add(30, T0);
    led.add(30, T0 + 3_600_000); // same day, +1h
    expect(led.check(T0)).toBe(true); // 60 < 100
    led.add(40, T0 + 7_200_000); // total 100
    expect(led.check(T0)).toBe(false); // hard cutoff
  });

  it('a fresh day zeroes the running total even after a prior-day breach', () => {
    const led = new DailySpendLedger(50);
    led.add(50, T0);
    expect(led.check(T0)).toBe(false);
    const nextDay = T0 + 24 * 3_600_000;
    expect(led.check(nextDay)).toBe(true);
    led.add(50, nextDay);
    expect(led.check(nextDay)).toBe(false); // independent budget
    expect(led.check(T0)).toBe(true); // querying the old day reads 0 (not stored)
  });
});

describe('createIngressCaps rate-limit rejection', () => {
  it('rate-limit rejection releases the inflight slot and reports the reason', async () => {
    const writer = okWriter();
    const caps = createIngressCaps(
      cfg({ rateCapacity: 1, maxInflight: 5 }),
      writer,
    );

    const first = await caps.tryAdmit({ source: 'A' }, T0);
    expect(first.ok).toBe(true);
    expect(caps.snapshot().inUse).toBe(1);

    // Source A's bucket (capacity 1) is now empty -> rate-limit.
    const second = await caps.tryAdmit({ source: 'A' }, T0);
    expect(second.ok).toBe(false);
    expect(second.ok === false && second.reason).toBe('rate-limit');
    // Slot acquired during the gate was released — only the first admit holds one.
    expect(caps.snapshot().inUse).toBe(1);
  });

  it('release() frees a post-dispatch slot so a later admit succeeds', async () => {
    const writer = okWriter();
    const caps = createIngressCaps(
      cfg({ maxInflight: 1, rateCapacity: 5 }),
      writer,
    );

    expect((await caps.tryAdmit({ source: 'A' }, T0)).ok).toBe(true);
    expect((await caps.tryAdmit({ source: 'A' }, T0)).ok).toBe(false); // at cap
    caps.release();
    expect(caps.snapshot().inUse).toBe(0);
    expect((await caps.tryAdmit({ source: 'A' }, T0)).ok).toBe(true);
  });

  it('recordSpend drives the gate, and a new day re-opens it', async () => {
    const writer = okWriter();
    const caps = createIngressCaps(cfg({ dailySpendLimit: 100 }), writer);
    caps.recordSpend(100, T0);
    expect((await caps.tryAdmit({ source: 'A' }, T0)).ok).toBe(false);
    const nextDay = T0 + 24 * 3_600_000;
    expect((await caps.tryAdmit({ source: 'A' }, nextDay)).ok).toBe(true);
  });
});
