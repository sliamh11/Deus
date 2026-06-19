import { beforeEach, describe, expect, it, vi } from 'vitest';
import crypto from 'crypto';
import { createGitHubIngressHandler } from './github-ingress.js';
import type { GitHubIngressDeps } from './github-ingress.js';
import type { VerifyResult } from './hmac.js';
import type { LinearContext } from '../linear-dispatcher.js';

/** verify() is synchronous here; the interface types it as VerifyResult | Promise. */
const ok = (r: VerifyResult | Promise<VerifyResult>): boolean =>
  (r as VerifyResult).ok;

const SECRET = 'test-gh-secret';
const REPO = 'test-owner/test-repo';
const PR_NUM = 613;
const PR_URL = `https://github.com/${REPO}/pull/${PR_NUM}`;

function sign(body: Buffer, secret = SECRET): string {
  return (
    'sha256=' + crypto.createHmac('sha256', secret).update(body).digest('hex')
  );
}

let mergeIfGreen: ReturnType<typeof vi.fn>;
let markDoneIfMerged: ReturnType<typeof vi.fn>;
let getIssueByPrUrl: ReturnType<typeof vi.fn>;

function makeHandler(overrides: Partial<GitHubIngressDeps> = {}) {
  mergeIfGreen = vi.fn().mockResolvedValue(undefined);
  markDoneIfMerged = vi.fn().mockResolvedValue(undefined);
  getIssueByPrUrl = vi
    .fn()
    .mockReturnValue({ issue_id: 'issue-1', identifier: 'LIA-1' });
  return createGitHubIngressHandler({
    ctx: {} as unknown as LinearContext,
    secret: SECRET,
    mergeIfGreen,
    markDoneIfMerged,
    getIssueByPrUrl,
    ...overrides,
  } as GitHubIngressDeps);
}

function req(
  event: string,
  sig?: string,
  deliveryId: string | null = 'delivery-1', // null = omit the delivery header
) {
  const headers: Record<string, string> = {};
  if (event) headers['x-github-event'] = event;
  if (sig !== undefined) headers['x-hub-signature-256'] = sig;
  if (deliveryId !== null) headers['x-github-delivery'] = deliveryId;
  return { headers } as never;
}

function fakeRes() {
  const out: { status?: number; body?: string } = {};
  const res = {
    writable: true,
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

function body(obj: object): Buffer {
  return Buffer.from(JSON.stringify(obj));
}

const checkSuiteSuccess = {
  action: 'completed',
  repository: { full_name: REPO },
  check_suite: { conclusion: 'success', pull_requests: [{ number: PR_NUM }] },
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe('createGitHubIngressHandler — verify (fail-closed auth + replay)', () => {
  it('registers the /github prefix', () => {
    expect(makeHandler().pathPrefix).toBe('/github');
  });

  it('accepts a valid signature + delivery id', () => {
    const b = body(checkSuiteSuccess);
    expect(makeHandler().verify(req('check_suite', sign(b)), b)).toEqual({
      ok: true,
    });
  });

  it('rejects a bad signature', () => {
    const b = body(checkSuiteSuccess);
    expect(
      ok(makeHandler().verify(req('check_suite', sign(b, 'wrong')), b)),
    ).toBe(false);
  });

  it('rejects a missing signature', () => {
    const b = body(checkSuiteSuccess);
    expect(ok(makeHandler().verify(req('check_suite', undefined), b))).toBe(
      false,
    );
  });

  it('rejects a missing delivery id (replay header)', () => {
    const b = body(checkSuiteSuccess);
    expect(ok(makeHandler().verify(req('check_suite', sign(b), null), b))).toBe(
      false,
    );
  });

  it('rejects a replayed delivery id', () => {
    const b = body(checkSuiteSuccess);
    const h = makeHandler();
    expect(ok(h.verify(req('check_suite', sign(b), 'dup'), b))).toBe(true);
    expect(ok(h.verify(req('check_suite', sign(b), 'dup'), b))).toBe(false);
  });
});

describe('createGitHubIngressHandler — handle (merge-only dispatch)', () => {
  it('ping → 200, no action', async () => {
    const { res, out } = fakeRes();
    await makeHandler().handle(req('ping'), res, body({}));
    expect(out.status).toBe(200);
    expect(mergeIfGreen).not.toHaveBeenCalled();
    expect(markDoneIfMerged).not.toHaveBeenCalled();
  });

  it('non-allowlisted event → 204 no-op', async () => {
    const { res, out } = fakeRes();
    await makeHandler().handle(req('push'), res, body({}));
    expect(out.status).toBe(204);
    expect(mergeIfGreen).not.toHaveBeenCalled();
  });

  it('check_suite success on a tracked PR → mergeIfGreen, 200', async () => {
    const { res, out } = fakeRes();
    await makeHandler().handle(
      req('check_suite'),
      res,
      body(checkSuiteSuccess),
    );
    expect(out.status).toBe(200);
    expect(mergeIfGreen).toHaveBeenCalledTimes(1);
    expect(mergeIfGreen).toHaveBeenCalledWith({}, 'issue-1', PR_URL, 'LIA-1');
    expect(markDoneIfMerged).not.toHaveBeenCalled();
  });

  it('check_suite NON-success → no merge, 204 no-op (no DB lookup)', async () => {
    const { res, out } = fakeRes();
    await makeHandler().handle(
      req('check_suite'),
      res,
      body({
        repository: { full_name: REPO },
        check_suite: {
          conclusion: 'failure',
          pull_requests: [{ number: PR_NUM }],
        },
      }),
    );
    expect(out.status).toBe(204);
    expect(mergeIfGreen).not.toHaveBeenCalled();
    // Non-actionable events must not even hit the DB lookup.
    expect(getIssueByPrUrl).not.toHaveBeenCalled();
  });

  it('pull_request closed+merged → markDoneIfMerged, 200', async () => {
    const { res, out } = fakeRes();
    await makeHandler().handle(
      req('pull_request'),
      res,
      body({
        action: 'closed',
        repository: { full_name: REPO },
        pull_request: { number: PR_NUM, merged: true },
      }),
    );
    expect(out.status).toBe(200);
    expect(markDoneIfMerged).toHaveBeenCalledTimes(1);
    expect(mergeIfGreen).not.toHaveBeenCalled();
  });

  it('pull_request closed but NOT merged → 204 no-op', async () => {
    const { res, out } = fakeRes();
    await makeHandler().handle(
      req('pull_request'),
      res,
      body({
        action: 'closed',
        repository: { full_name: REPO },
        pull_request: { number: PR_NUM, merged: false },
      }),
    );
    expect(out.status).toBe(204);
    expect(markDoneIfMerged).not.toHaveBeenCalled();
  });

  it('unknown / untracked PR → 204 no-op (no merge fn called)', async () => {
    const { res, out } = fakeRes();
    const h = makeHandler({
      getIssueByPrUrl: vi.fn().mockReturnValue(undefined),
    });
    await h.handle(req('check_suite'), res, body(checkSuiteSuccess));
    expect(out.status).toBe(204);
    expect(mergeIfGreen).not.toHaveBeenCalled();
  });

  it('check_suite with no PR in payload → 204 no-op', async () => {
    const { res, out } = fakeRes();
    await makeHandler().handle(
      req('check_suite'),
      res,
      body({
        repository: { full_name: REPO },
        check_suite: { conclusion: 'success', pull_requests: [] },
      }),
    );
    expect(out.status).toBe(204);
    expect(mergeIfGreen).not.toHaveBeenCalled();
  });

  it('malformed JSON → 400', async () => {
    const { res, out } = fakeRes();
    await makeHandler().handle(
      req('check_suite'),
      res,
      Buffer.from('not json'),
    );
    expect(out.status).toBe(400);
  });
});
