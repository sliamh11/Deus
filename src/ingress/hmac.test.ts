import { describe, it, expect } from 'vitest';
import crypto from 'crypto';
import { verifyHmacSha256, ReplayStore, checkReplay } from './hmac.js';

function sign(secret: string, body: string, prefix = ''): string {
  return (
    prefix + crypto.createHmac('sha256', secret).update(body).digest('hex')
  );
}

describe('verifyHmacSha256', () => {
  const secret = 'topsecret';
  const body = Buffer.from('{"hello":"world"}');

  it('accepts a valid signature', () => {
    expect(
      verifyHmacSha256(secret, body, sign(secret, body.toString())).ok,
    ).toBe(true);
  });

  it('accepts the sha256= prefixed form (GitHub style)', () => {
    const sig = sign(secret, body.toString(), 'sha256=');
    expect(verifyHmacSha256(secret, body, sig).ok).toBe(true);
  });

  it('rejects a tampered body', () => {
    const sig = sign(secret, body.toString());
    const r = verifyHmacSha256(secret, Buffer.from('{"hello":"evil"}'), sig);
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/mismatch/);
  });

  it('rejects a wrong secret', () => {
    expect(
      verifyHmacSha256('other', body, sign(secret, body.toString())).ok,
    ).toBe(false);
  });

  it('fails closed on a missing secret', () => {
    const r = verifyHmacSha256(undefined, body, sign(secret, body.toString()));
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/no HMAC secret/);
  });

  it('fails closed on a missing signature', () => {
    const r = verifyHmacSha256(secret, body, undefined);
    expect(r.ok).toBe(false);
    expect(r.reason).toMatch(/missing signature/);
  });

  it('rejects a length-mismatched signature without throwing', () => {
    expect(verifyHmacSha256(secret, body, 'deadbeef').ok).toBe(false);
  });
});

describe('ReplayStore', () => {
  it('records fresh ids and rejects duplicates', () => {
    const s = new ReplayStore(1000);
    expect(s.checkAndRecord('a', 0)).toBe(true);
    expect(s.checkAndRecord('a', 0)).toBe(false);
    expect(s.checkAndRecord('b', 0)).toBe(true);
  });

  it('expires entries after the TTL window', () => {
    const s = new ReplayStore(1000);
    expect(s.checkAndRecord('a', 0)).toBe(true);
    // After TTL, the same id is fresh again (entry evicted).
    expect(s.checkAndRecord('a', 2000)).toBe(true);
  });

  it('bounds memory with the maxEntries cap', () => {
    const s = new ReplayStore(1_000_000, 3);
    for (const id of ['a', 'b', 'c', 'd', 'e']) s.checkAndRecord(id, 0);
    expect(s.size()).toBeLessThanOrEqual(3);
  });
});

describe('checkReplay', () => {
  it("strategy 'none' always passes", () => {
    const s = new ReplayStore(1000);
    expect(checkReplay({ strategy: 'none' }, {}, s, 0).ok).toBe(true);
  });

  describe("strategy 'delivery-id' (GitHub)", () => {
    const cfg = {
      strategy: 'delivery-id' as const,
      idHeader: 'X-GitHub-Delivery',
    };

    it('accepts a fresh delivery id, dedupes a redelivery', () => {
      const s = new ReplayStore(60_000);
      const headers = { 'x-github-delivery': 'uuid-1' };
      expect(checkReplay(cfg, headers, s, 0).ok).toBe(true);
      const r = checkReplay(cfg, headers, s, 10);
      expect(r.ok).toBe(false);
      expect(r.reason).toMatch(/replay/);
    });

    it('fails closed when the id header is absent', () => {
      const s = new ReplayStore(60_000);
      const r = checkReplay(cfg, {}, s, 0);
      expect(r.ok).toBe(false);
      expect(r.reason).toMatch(/missing replay id/);
    });
  });

  describe("strategy 'timestamp-nonce'", () => {
    const cfg = {
      strategy: 'timestamp-nonce' as const,
      tsHeader: 'X-Timestamp',
      nonceHeader: 'X-Nonce',
      maxSkewMs: 1000,
    };

    it('accepts fresh timestamp + unique nonce', () => {
      const s = new ReplayStore(60_000);
      const headers = { 'x-timestamp': '1000', 'x-nonce': 'n1' };
      expect(checkReplay(cfg, headers, s, 1000).ok).toBe(true);
    });

    it('rejects a timestamp outside the skew window', () => {
      const s = new ReplayStore(60_000);
      const headers = { 'x-timestamp': '1000', 'x-nonce': 'n1' };
      expect(checkReplay(cfg, headers, s, 5000).ok).toBe(false);
    });

    it('rejects a duplicate nonce', () => {
      const s = new ReplayStore(60_000);
      const headers = { 'x-timestamp': '1000', 'x-nonce': 'n1' };
      expect(checkReplay(cfg, headers, s, 1000).ok).toBe(true);
      expect(checkReplay(cfg, headers, s, 1000).ok).toBe(false);
    });

    it('fails closed on missing headers', () => {
      const s = new ReplayStore(60_000);
      expect(checkReplay(cfg, {}, s, 1000).ok).toBe(false);
    });

    it('rejects a non-numeric timestamp', () => {
      const s = new ReplayStore(60_000);
      const headers = { 'x-timestamp': 'notanumber', 'x-nonce': 'n1' };
      expect(checkReplay(cfg, headers, s, 1000).ok).toBe(false);
    });
  });
});
