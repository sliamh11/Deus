// R5 (LIA-315 Phase 3): DoS/cost caps for webhook-originated agent runs, plus the
// R6 pre-dispatch audit gate. An anonymous external trigger must not be able to
// spawn unbounded container runs or burn unbounded LLM spend, and no run may be
// admitted without a durable forensic record.
//
// Three independent primitives (each unit-testable in isolation) + one facade
// (`createIngressCaps`) that encodes the fail-closed admission ORDERING once, so
// Phase 4 (the webhook dispatch path — the concrete, already-planned caller) does
// not re-implement the subtle sequence.
//
// Dormant in Phase 3: nothing in the live runtime constructs the facade yet.

import type { AuditWriteResult, IngressAuditEvent } from './audit.js';

export interface IngressCapsConfig {
  /** Global in-flight ceiling, shared across ALL webhook sources. */
  maxInflight: number;
  /** Per-source token-bucket capacity (burst). */
  rateCapacity: number;
  /** Milliseconds to refill ONE token in a source's bucket. */
  rateRefillMs: number;
  /** Hard daily spend ceiling (unit-agnostic; Phase 4 feeds the unit). */
  dailySpendLimit: number;
}

/**
 * Counting semaphore for the global number of concurrent in-flight webhook runs.
 * `tryAcquire` fails CLOSED at the ceiling; `release` is clamped at 0 so an
 * over-release (double-release / release-without-acquire) can never inflate the
 * effective capacity above the ceiling.
 */
export class InflightCap {
  private used = 0;
  constructor(private readonly ceiling: number) {}

  tryAcquire(): boolean {
    if (this.used >= this.ceiling) return false;
    this.used += 1;
    return true;
  }

  release(): void {
    if (this.used > 0) this.used -= 1;
  }

  inUse(): number {
    return this.used;
  }
}

/**
 * Per-source token bucket with lazy refill. One bucket per source name; one
 * source draining its bucket does NOT affect another (isolation).
 *
 * `last` advances only by whole consumed quanta (`refill * refillMs`), preserving
 * the sub-quantum remainder, so a token is granted exactly once per `refillMs`.
 *
 * SEQUENCING CONSTRAINT (Phase 4): `tryConsume` must only ever be called with a
 * source name drawn from the VALIDATED operator source config — never an arbitrary
 * attacker-controlled path segment. The webhook channel validates source identity
 * before calling, which is what bounds the per-source `Map` key set.
 */
export class SourceRateLimiter {
  private readonly buckets = new Map<
    string,
    { tokens: number; last: number }
  >();
  constructor(
    private readonly capacity: number,
    private readonly refillMs: number,
  ) {}

  tryConsume(source: string, now: number): boolean {
    let b = this.buckets.get(source);
    if (!b) {
      b = { tokens: this.capacity, last: now };
      this.buckets.set(source, b);
    } else if (now > b.last && this.refillMs > 0) {
      const refill = Math.floor((now - b.last) / this.refillMs);
      if (refill > 0) {
        b.tokens = Math.min(this.capacity, b.tokens + refill);
        b.last += refill * this.refillMs;
      }
    }
    if (b.tokens >= 1) {
      b.tokens -= 1;
      return true;
    }
    return false;
  }
}

/**
 * Daily spend ledger with a hard cutoff. `check` reports whether today's running
 * total is still UNDER the ceiling; once it reaches the ceiling, `check` returns
 * false until the UTC day rolls over (a fresh day starts at zero).
 *
 * `check` is a pure read (no mutation); `add` performs the rollover reset.
 * Single-threaded correctness: the Node event loop serializes check→add, so there
 * is no check-then-act race. A future worker-thread / distributed host would need
 * an atomic check-and-increment instead.
 */
export class DailySpendLedger {
  private day = '';
  private total = 0;
  constructor(private readonly limit: number) {}

  private static dayKey(now: number): string {
    return new Date(Number.isFinite(now) ? now : 0).toISOString().slice(0, 10);
  }

  check(now: number): boolean {
    const todayTotal =
      DailySpendLedger.dayKey(now) === this.day ? this.total : 0;
    return todayTotal < this.limit;
  }

  add(cost: number, now: number): void {
    const key = DailySpendLedger.dayKey(now);
    if (key !== this.day) {
      this.day = key;
      this.total = 0;
    }
    this.total += cost;
  }
}

export type AdmitResult =
  | { ok: true }
  | { ok: false; reason: AdmitRejectReason };

export type AdmitRejectReason =
  | 'spend-limit'
  | 'inflight-cap'
  | 'rate-limit'
  | 'audit-unavailable';

/** The minimum a webhook event must carry to be admitted + audited. */
export interface AdmitEventInput {
  source: string;
  requestId?: string;
  // Phase-4 caller constraint: pass the SANITIZED URL path only (no query string).
  // The audit redactor catches Bearer/hex/sha-digest shapes but a short non-hex
  // secret in a query param (`?secret=abc12`) would escape it, so credentials must
  // never reach `path` in the first place.
  path?: string;
}

/** Injected dependency so the facade is testable with succeeding/failing writers. */
export interface AuditWriter {
  append(event: IngressAuditEvent): Promise<AuditWriteResult>;
}

export interface IngressCaps {
  tryAdmit(event: AdmitEventInput, now: number): Promise<AdmitResult>;
  release(): void;
  recordSpend(cost: number, now: number): void;
  snapshot(): { inUse: number };
}

/**
 * The single fail-closed R5+R6 admission gate. Ordering (each step fails closed):
 *
 *   1. spend.check        — pure read; over-limit rejects BEFORE acquiring a slot,
 *                           so a spend rejection consumes no inflight slot.
 *   2. inflight.tryAcquire — reversible; released on any later failure.
 *   3. rate.tryConsume     — if empty, release the slot and reject.
 *   4. MANDATORY audit     — append the `admitted` record. The await is wrapped in
 *                            try/catch: on a {ok:false} result OR any thrown/
 *                            rejected error, release the slot and reject
 *                            'audit-unavailable'. This makes the slot-safe + the
 *                            "no admission without a durable record" guarantees
 *                            UNCONDITIONAL — even a future no-throw-contract
 *                            violation in the writer cannot leak a slot (a leaked
 *                            slot would drain the cap to 0 and mimic a real DoS).
 *
 * Note: the per-source rate token consumed at step 3 is NOT refunded if step 4
 * fails or if Phase 4 aborts a post-admit dispatch — deliberate (penalize
 * admission attempts, not just dispatches). Rejections at steps 1–3 emit a
 * best-effort, fire-and-forget `rejected` audit event (a missing rejection record
 * is not a security hole, unlike a missing admission record).
 */
export function createIngressCaps(
  cfg: IngressCapsConfig,
  audit: AuditWriter,
): IngressCaps {
  const inflight = new InflightCap(cfg.maxInflight);
  const rate = new SourceRateLimiter(cfg.rateCapacity, cfg.rateRefillMs);
  const spend = new DailySpendLedger(cfg.dailySpendLimit);

  // Fire-and-forget rejection breadcrumb; swallow errors so a broken writer never
  // surfaces an unhandled rejection (the security control is the reject itself).
  const emitRejected = (
    event: AdmitEventInput,
    now: number,
    reason: AdmitRejectReason,
  ): void => {
    void audit
      .append({
        ts: now,
        source: event.source,
        event: 'rejected',
        decision: 'rejected',
        reason,
        requestId: event.requestId,
        path: event.path,
      })
      .catch(() => {});
  };

  return {
    async tryAdmit(event: AdmitEventInput, now: number): Promise<AdmitResult> {
      if (!spend.check(now)) {
        emitRejected(event, now, 'spend-limit');
        return { ok: false, reason: 'spend-limit' };
      }
      if (!inflight.tryAcquire()) {
        emitRejected(event, now, 'inflight-cap');
        return { ok: false, reason: 'inflight-cap' };
      }
      if (!rate.tryConsume(event.source, now)) {
        inflight.release();
        emitRejected(event, now, 'rate-limit');
        return { ok: false, reason: 'rate-limit' };
      }
      // Mandatory pre-dispatch audit — fail-closed on result OR throw.
      try {
        const r = await audit.append({
          ts: now,
          source: event.source,
          event: 'admitted',
          decision: 'admitted',
          requestId: event.requestId,
          path: event.path,
        });
        if (!r.ok) {
          inflight.release();
          return { ok: false, reason: 'audit-unavailable' };
        }
      } catch {
        inflight.release();
        return { ok: false, reason: 'audit-unavailable' };
      }
      return { ok: true };
    },

    release(): void {
      inflight.release();
    },

    // Phase-4 caller constraint: pass the SAME `now` to the `tryAdmit` that gated a
    // run and to its matching `recordSpend`, so the ledger's day-key cannot diverge
    // across the UTC boundary (the check and the add must agree on "today").
    recordSpend(cost: number, now: number): void {
      spend.add(cost, now);
    },

    snapshot(): { inUse: number } {
      return { inUse: inflight.inUse() };
    },
  };
}
