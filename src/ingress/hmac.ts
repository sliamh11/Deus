// Generic HMAC-SHA256 verification + source-configurable replay protection for
// the ingress gateway. Pure `crypto` — no new dependency, no Linear-SDK coupling.
//
// Why this exists: the Linear webhook delegates signature checks to
// `@linear/sdk/webhooks`, which is Linear-specific. A generic webhook source
// (GitHub, etc.) needs its own constant-time HMAC verify plus a replay guard
// whose CONTRACT matches the source — GitHub signs deliveries and sends a
// delivery UUID (`X-GitHub-Delivery`), NOT a timestamp+nonce pair, so a fixed
// timestamp-nonce guard would reject every valid GitHub delivery. Replay is
// therefore a per-source strategy.

import crypto from 'crypto';

export interface VerifyResult {
  ok: boolean;
  reason?: string;
}

/**
 * Constant-time HMAC-SHA256 verification over the RAW request body.
 *
 * `signature` is the value of the source's signature header; an optional
 * `sha256=` prefix (GitHub style) is stripped. Comparison is on the hex
 * representations (length-checked first, since `crypto.timingSafeEqual` throws
 * on length mismatch — same trade-off as `odysseus-server.ts:timingSafeEqualStr`).
 *
 * Fails CLOSED: a missing secret or missing signature is a rejection, never a pass.
 */
export function verifyHmacSha256(
  secret: string | undefined,
  body: Buffer,
  signature: string | undefined,
): VerifyResult {
  if (!secret) return { ok: false, reason: 'no HMAC secret configured' };
  if (!signature) return { ok: false, reason: 'missing signature header' };

  const provided = signature.startsWith('sha256=')
    ? signature.slice('sha256='.length)
    : signature;
  const expected = crypto
    .createHmac('sha256', secret)
    .update(body)
    .digest('hex');

  const ab = Buffer.from(provided);
  const bb = Buffer.from(expected);
  if (ab.length !== bb.length)
    return { ok: false, reason: 'signature mismatch' };
  if (!crypto.timingSafeEqual(ab, bb)) {
    return { ok: false, reason: 'signature mismatch' };
  }
  return { ok: true };
}

/**
 * Bounded-TTL dedupe store for replay protection (nonce / delivery-id).
 *
 * The TTL is uniform per store, so `Map` insertion order == expiry order — the
 * eviction sweep can stop at the first non-expired entry (O(expired) amortized).
 * A hard `maxEntries` cap bounds memory against a flood of unique ids
 * (process-lifetime only — documented non-persistent; sufficient behind the
 * tunnel's TLS for the current threat model).
 */
export class ReplayStore {
  private readonly seen = new Map<string, number>(); // id → expiry epoch ms

  constructor(
    private readonly ttlMs: number,
    private readonly maxEntries = 1000,
  ) {}

  /** Records `id`; returns true if fresh, false if already seen (a replay). */
  checkAndRecord(id: string, now: number): boolean {
    this.evictExpired(now);
    if (this.seen.has(id)) return false;
    this.seen.set(id, now + this.ttlMs);
    if (this.seen.size > this.maxEntries) {
      const oldest = this.seen.keys().next().value;
      if (oldest !== undefined) this.seen.delete(oldest);
    }
    return true;
  }

  private evictExpired(now: number): void {
    for (const [key, expiry] of this.seen) {
      if (expiry <= now) this.seen.delete(key);
      else break; // uniform TTL + non-decreasing `now` ⇒ insertion order == expiry order
    }
  }

  /** @internal — test visibility */
  size(): number {
    return this.seen.size;
  }
}

export type ReplayStrategy = 'delivery-id' | 'timestamp-nonce' | 'none';

export interface ReplayConfig {
  strategy: ReplayStrategy;
  /** Header carrying the unique delivery id (strategy 'delivery-id'). */
  idHeader?: string;
  /** Header carrying the epoch-ms timestamp (strategy 'timestamp-nonce'). */
  tsHeader?: string;
  /** Header carrying the nonce (strategy 'timestamp-nonce'). */
  nonceHeader?: string;
  /** Allowed clock skew in ms (strategy 'timestamp-nonce'). Default 5 min. */
  maxSkewMs?: number;
}

type Headers = Record<string, string | string[] | undefined>;

function headerValue(headers: Headers, name?: string): string | undefined {
  if (!name) return undefined;
  const raw = headers[name.toLowerCase()] ?? headers[name];
  return Array.isArray(raw) ? raw[0] : raw;
}

/**
 * Per-source replay check. Fails CLOSED for the inputs the chosen strategy
 * requires, but the `'none'` strategy is an explicit opt-out (signature-only).
 */
export function checkReplay(
  cfg: ReplayConfig,
  headers: Headers,
  store: ReplayStore,
  now: number,
): VerifyResult {
  if (cfg.strategy === 'none') return { ok: true };

  if (cfg.strategy === 'delivery-id') {
    const id = headerValue(headers, cfg.idHeader);
    if (!id) {
      return { ok: false, reason: `missing replay id header ${cfg.idHeader}` };
    }
    if (!store.checkAndRecord(id, now)) {
      return { ok: false, reason: 'duplicate delivery id (replay)' };
    }
    return { ok: true };
  }

  // timestamp-nonce
  const tsRaw = headerValue(headers, cfg.tsHeader);
  const nonce = headerValue(headers, cfg.nonceHeader);
  if (!tsRaw || !nonce) {
    return { ok: false, reason: 'missing timestamp or nonce header' };
  }
  const ts = Number(tsRaw);
  if (!Number.isFinite(ts)) return { ok: false, reason: 'invalid timestamp' };
  const skew = cfg.maxSkewMs ?? 300_000;
  if (Math.abs(now - ts) > skew) {
    return { ok: false, reason: 'timestamp outside skew window' };
  }
  if (!store.checkAndRecord(nonce, now)) {
    return { ok: false, reason: 'duplicate nonce (replay)' };
  }
  return { ok: true };
}
