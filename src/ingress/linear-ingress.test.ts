import { beforeEach, describe, expect, it, vi } from 'vitest';
import crypto from 'crypto';
import type { LinearContext } from '../linear-dispatcher.js';
import type { GateSpec } from '../linear-gate-specs.js';

// ── Module-level mocks (declared before the units that consume them) ──────────
// Spy the cache writers so we can assert dispatch happened without real writes,
// and stub the dispatcher so the fire-and-forget handleIssueUpdate path is inert.

vi.mock('../db.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../db.js')>();
  return {
    ...actual,
    upsertIssueCache: vi.fn(),
    softDeleteIssueCache: vi.fn(),
  };
});

vi.mock('../linear-dispatcher.js', async (importOriginal) => {
  const actual =
    await importOriginal<typeof import('../linear-dispatcher.js')>();
  return {
    ...actual,
    executeAgentRun: vi.fn().mockResolvedValue({ text: '', error: '' }),
  };
});

const { upsertIssueCache, _initTestDatabase } = await import('../db.js');
const { createLinearIngressHandler } = await import('../linear-webhook.js');

const SECRET = 'test-webhook-secret';

/** Build a JSON body (with a fresh webhookTimestamp) + its valid HMAC-SHA256 hex sig. */
function signed(payload: Record<string, unknown>, secret = SECRET) {
  const body = Buffer.from(
    JSON.stringify({ webhookTimestamp: Date.now(), ...payload }),
  );
  const sig = crypto.createHmac('sha256', secret).update(body).digest('hex');
  return { body, sig };
}

function req(sig?: string, ts?: string) {
  const headers: Record<string, string> = {};
  if (sig !== undefined) headers['linear-signature'] = sig;
  if (ts !== undefined) headers['linear-timestamp'] = ts;
  return { headers } as never;
}

function fakeRes() {
  const out: { status?: number; body?: string } = {};
  const res = {
    writeHead: (s: number) => {
      out.status = s;
      return res;
    },
    end: (b?: string) => {
      out.body = b;
    },
  } as never;
  return { res, out };
}

function makeHandler() {
  return createLinearIngressHandler(
    { vaultPath: undefined } as unknown as LinearContext,
    new Map<string, GateSpec>(),
    SECRET,
  );
}

const issueData = {
  id: 'i1',
  identifier: 'LIA-1',
  title: 't',
  state: { name: 'In Review' },
  teamId: 'team',
  priority: 0,
  createdAt: 'x',
  updatedAt: 'y',
};

beforeEach(() => {
  _initTestDatabase();
  vi.clearAllMocks();
});

describe('createLinearIngressHandler', () => {
  it('registers the /linear path prefix', () => {
    expect(makeHandler().pathPrefix).toBe('/linear');
  });

  it('verify accepts a valid signature', async () => {
    const { body, sig } = signed({ type: 'Issue', action: 'update' });
    expect(await makeHandler().verify(req(sig), body)).toEqual({ ok: true });
  });

  it('verify rejects a tampered body', async () => {
    const { sig } = signed({ type: 'Issue' });
    const tampered = Buffer.from(JSON.stringify({ type: 'Issue', evil: true }));
    expect((await makeHandler().verify(req(sig), tampered)).ok).toBe(false);
  });

  it('verify rejects a wrong-secret signature', async () => {
    const { body, sig } = signed({ type: 'Issue' }, 'other-secret');
    expect((await makeHandler().verify(req(sig), body)).ok).toBe(false);
  });

  it('verify rejects a missing signature header', async () => {
    const { body } = signed({ type: 'Issue' });
    expect(await makeHandler().verify(req(undefined), body)).toEqual({
      ok: false,
      reason: 'missing signature',
    });
  });

  it('handle dispatches an Issue payload and returns 200', async () => {
    const { body, sig } = signed({
      type: 'Issue',
      action: 'update',
      data: issueData,
    });
    const { res, out } = fakeRes();
    await makeHandler().handle(req(sig), res, body);
    expect(out.status).toBe(200);
    expect(upsertIssueCache).toHaveBeenCalledTimes(1);
  });

  it('handle ignores a non-Issue payload but still returns 200', async () => {
    const { body, sig } = signed({
      type: 'Comment',
      action: 'create',
      data: { id: 'c1' },
    });
    const { res, out } = fakeRes();
    await makeHandler().handle(req(sig), res, body);
    expect(out.status).toBe(200);
    expect(upsertIssueCache).not.toHaveBeenCalled();
  });

  it('handle returns 400 on a bad signature and does not dispatch', async () => {
    const { body, sig } = signed({ type: 'Issue', data: issueData }, 'wrong');
    const { res, out } = fakeRes();
    await makeHandler().handle(req(sig), res, body);
    expect(out.status).toBe(400);
    expect(upsertIssueCache).not.toHaveBeenCalled();
  });
});
