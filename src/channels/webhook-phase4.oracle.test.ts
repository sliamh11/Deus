/**
 * Oracle tests for Ingress Phase 4 — Part B & C: webhook channel
 * (IngressHandler shape + HMAC verify + handle framing + ownsJid + R3 provisioning)
 *
 * Authored from the SPEC (LIA-315 Phase 4 contract), BEFORE any implementation
 * exists (oracle-author warden). The module under test
 * (`./webhook.js`) DOES NOT EXIST YET — all tests are RED by import failure.
 *
 * Independence: written blind to any implementation. Every expected value traces
 * to the spec. The @oracle tags protect this file from silent weakening after
 * the implementation ships.
 *
 * Harness: the channel factory is called with a fake ChannelOpts that captures
 * the handler pushed via registerIngressHandler. The fake spy records all calls
 * so tests can assert handler interactions without the real gateway or network.
 */

import crypto from 'node:crypto';
import { describe, it, expect, vi } from 'vitest';

// The module under test — DOES NOT EXIST YET. Import failure = RED (expected).
import { createWebhookChannel } from './webhook.js';

import type { Channel, OnInboundMessage } from '../types.js';
import type { ChannelOpts } from './registry.js';
import type { IngressHandler } from '../ingress/gateway.js';

// ─── Source config used in all B/C tests ─────────────────────────────────────

const KNOWN_SECRET = 'test-oracle-secret-phase4';
const SOURCE_NAME = 'github';
const SOURCE_FOLDER = 'webhook-sandbox-github';

const SINGLE_SOURCE = {
  name: SOURCE_NAME,
  hmacHeader: 'x-hub-signature-256',
  hmacSecret: KNOWN_SECRET,
  replayStrategy: 'none' as const,
  targetGroupFolder: SOURCE_FOLDER,
};

// ─── Helpers ─────────────────────────────────────────────────────────────────

function makeHmac(body: Buffer, secret = KNOWN_SECRET): string {
  return (
    'sha256=' + crypto.createHmac('sha256', secret).update(body).digest('hex')
  );
}

function makeReq(
  headers: Record<string, string>,
  url = `/hook/${SOURCE_NAME}`,
) {
  return {
    headers,
    url,
    socket: { remoteAddress: '127.0.0.1' },
  } as unknown as import('http').IncomingMessage;
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
    writableEnded: false,
    headersSent: false,
  } as unknown as import('http').ServerResponse;
  return { res, out };
}

/** Minimal ChannelOpts spy that captures the pushed IngressHandler. */
function makeOpts(
  over: {
    sources?: (typeof SINGLE_SOURCE)[];
    registeredGroupFolders?: string[];
  } = {},
) {
  const capturedHandlers: IngressHandler[] = [];
  const registeredGroups: Record<
    string,
    import('../types.js').RegisteredGroup
  > = {};

  // Pre-populate registeredGroups from the test scenario
  for (const folder of over.registeredGroupFolders ?? []) {
    const jid = `existing:${folder}`;
    registeredGroups[jid] = {
      name: `existing-${folder}`,
      folder,
      trigger: 'always',
      added_at: '2026-01-01T00:00:00.000Z',
      // NOT publicIngress — simulates a pre-existing non-webhook group
      containerConfig: { publicIngress: false },
    };
  }

  const opts: ChannelOpts & {
    capturedHandlers: IngressHandler[];
    registeredGroupSpy: ReturnType<typeof vi.fn>;
    onMessageSpy: ReturnType<typeof vi.fn>;
  } = {
    onMessage: vi.fn(),
    onReaction: vi.fn(),
    onChatMetadata: vi.fn(),
    registeredGroups: () => registeredGroups,
    capturedHandlers,
    registeredGroupSpy: vi.fn(),
    onMessageSpy: vi.fn(),
  };

  // Splice the real spies in
  opts.onMessage = opts.onMessageSpy as unknown as OnInboundMessage;

  return opts;
}

/** Build the channel with the given sources, capture the pushed handler. */
function buildChannel(
  sources: (typeof SINGLE_SOURCE)[] = [SINGLE_SOURCE],
  registeredGroupFolders: string[] = [],
): {
  channel: Channel;
  handler: IngressHandler;
  opts: ReturnType<typeof makeOpts>;
} {
  const capturedHandlers: IngressHandler[] = [];
  const opts = makeOpts({ sources, registeredGroupFolders });

  // The channel factory receives opts + the sources config + a registerIngressHandler sink
  const channel = createWebhookChannel(
    opts,
    sources,
    (handler: IngressHandler) => capturedHandlers.push(handler),
  );

  if (!channel)
    throw new Error('channel factory returned null — check sources config');

  return {
    channel,
    // Invariant from spec B: exactly ONE handler registered
    handler: capturedHandlers[0]!,
    opts,
  };
}

// =============================================================================
// CASE B0 — channel registers exactly ONE handler with pathPrefix '/hook'
// =============================================================================
describe('@oracle webhook channel — registers one handler with pathPrefix /hook', () => {
  it('@oracle createWebhookChannel pushes exactly one IngressHandler with pathPrefix "/hook"', () => {
    // @oracle: spec B — channel registers ONE handler with pathPrefix === '/hook'
    const captured: IngressHandler[] = [];
    createWebhookChannel(makeOpts(), [SINGLE_SOURCE], (h: IngressHandler) =>
      captured.push(h),
    );

    expect(captured).toHaveLength(1);
    expect(captured[0]!.pathPrefix).toBe('/hook');
  });
});

// =============================================================================
// CASE B1 — verify: correct HMAC-SHA256 with sha256= prefix → {ok: true}
// =============================================================================
describe('@oracle webhook channel — verify accepts correct HMAC-SHA256 signature', () => {
  it('@oracle verify({ok:true}) for a correct sha256=<hex> signature', async () => {
    // @oracle: spec B — correct HMAC-SHA256 hex with "sha256=" prefix → {ok:true}
    const { handler } = buildChannel();
    const body = Buffer.from(
      JSON.stringify({ action: 'opened', issue: { id: 1 } }),
    );
    const sig = makeHmac(body);

    const result = await handler.verify(
      makeReq({ 'x-hub-signature-256': sig }),
      body,
    );

    expect(result).toEqual({ ok: true });
  });

  it('@oracle verify({ok:true}) for a bare hex signature (no sha256= prefix)', async () => {
    // @oracle: spec B — the sha256= prefix is optional; bare hex must also be accepted
    const { handler } = buildChannel();
    const body = Buffer.from('{"event":"push"}');
    const bareHex = crypto
      .createHmac('sha256', KNOWN_SECRET)
      .update(body)
      .digest('hex');

    const result = await handler.verify(
      makeReq({ 'x-hub-signature-256': bareHex }),
      body,
    );

    expect(result).toEqual({ ok: true });
  });
});

// =============================================================================
// CASE B2 — verify rejects tampered body / wrong secret / missing header
// =============================================================================
describe('@oracle webhook channel — verify rejects invalid signatures (never throws)', () => {
  it('@oracle verify({ok:false}) for a tampered body', async () => {
    // @oracle: spec B — tampered body → {ok:false}; signature mismatch must fail closed
    const { handler } = buildChannel();
    const originalBody = Buffer.from('{"action":"opened"}');
    const sig = makeHmac(originalBody);
    const tamperedBody = Buffer.from('{"action":"deleted"}');

    const result = await handler.verify(
      makeReq({ 'x-hub-signature-256': sig }),
      tamperedBody,
    );

    expect(result.ok).toBe(false);
  });

  it('@oracle verify({ok:false}) for a wrong secret', async () => {
    // @oracle: spec B — wrong secret → {ok:false}; the HMAC won't match
    const { handler } = buildChannel();
    const body = Buffer.from('{"action":"opened"}');
    const wrongSig =
      'sha256=' +
      crypto.createHmac('sha256', 'wrong-secret').update(body).digest('hex');

    const result = await handler.verify(
      makeReq({ 'x-hub-signature-256': wrongSig }),
      body,
    );

    expect(result.ok).toBe(false);
  });

  it('@oracle verify({ok:false}) for a missing signature header', async () => {
    // @oracle: spec B — missing signature header → {ok:false}; fail closed, no secret = no access
    const { handler } = buildChannel();
    const body = Buffer.from('{}');

    const result = await handler.verify(
      makeReq({}), // no signature header
      body,
    );

    expect(result.ok).toBe(false);
  });

  it('@oracle verify never throws — always returns a VerifyResult', async () => {
    // @oracle: spec B — verify must never throw; even garbage inputs return {ok:false}
    const { handler } = buildChannel();

    let result: { ok: boolean } | undefined;
    await expect(
      (async () => {
        result = await handler.verify(
          makeReq({ 'x-hub-signature-256': 'sha256=deadbeef' }),
          Buffer.from('garbage'),
        );
      })(),
    ).resolves.toBeUndefined();

    expect(result).toBeDefined();
    expect(result!.ok).toBe(false);
  });
});

// =============================================================================
// CASE B3 — unknown source in path → verify {ok:false}
// =============================================================================
describe('@oracle webhook channel — verify rejects unknown source path', () => {
  it('@oracle verify({ok:false}) for path /hook/doesnotexist (no source registered)', async () => {
    // @oracle: spec B — unknown source segment in path → {ok:false}; attacker-controlled paths must not pass
    const { handler } = buildChannel();
    const body = Buffer.from('{}');
    const sig = makeHmac(body);

    const result = await handler.verify(
      makeReq(
        { 'x-hub-signature-256': sig },
        '/hook/doesnotexist', // not a known source name
      ),
      body,
    );

    expect(result.ok).toBe(false);
  });
});

// =============================================================================
// CASE B4 — handle: calls onMessage exactly once with correct jid + framed content
//           + sentinel injection framing + HTTP 202
// =============================================================================
describe('@oracle webhook channel — handle dispatches framed onMessage and responds 202', () => {
  it('@oracle handle calls onMessage once with jid "webhook:github" on a valid POST', async () => {
    // @oracle: spec B — valid verify → handle calls opts.onMessage with chat_jid === 'webhook:<name>'
    const { handler, opts } = buildChannel();
    const { res } = fakeRes();
    const body = Buffer.from('{"action":"opened","issue":{"id":42}}');

    await handler.handle(
      makeReq({ 'x-hub-signature-256': makeHmac(body) }),
      res,
      body,
    );

    expect(opts.onMessageSpy).toHaveBeenCalledTimes(1);
    const [calledJid] = opts.onMessageSpy.mock.calls[0] as [string, unknown];
    expect(calledJid).toBe('webhook:github');
  });

  it('@oracle handle responds HTTP 202 on a dispatched event', async () => {
    // @oracle: spec B — after a valid verify, handle must respond HTTP 202 (accepted, processing async)
    const { handler, opts } = buildChannel();
    const { res, out } = fakeRes();
    const body = Buffer.from('{"action":"opened"}');

    await handler.handle(
      makeReq({ 'x-hub-signature-256': makeHmac(body) }),
      res,
      body,
    );

    expect(out.status).toBe(202);
    // onMessage must have been called (the event was accepted for dispatch)
    expect(opts.onMessageSpy).toHaveBeenCalledTimes(1);
  });

  it('@oracle handle content contains the raw payload text', async () => {
    // @oracle: spec B — message content must include the raw payload so the model sees it
    const { handler, opts } = buildChannel();
    const { res } = fakeRes();
    const payload = '{"action":"closed","issue":{"id":99}}';
    const body = Buffer.from(payload);

    await handler.handle(
      makeReq({ 'x-hub-signature-256': makeHmac(body) }),
      res,
      body,
    );

    const [, message] = opts.onMessageSpy.mock.calls[0] as [
      string,
      { content: string },
    ];
    expect(message.content).toContain(payload);
  });

  it('@oracle handle content wraps payload with untrusted-input framing (sentinel injection marker)', async () => {
    // @oracle: spec B — content must include framing that tells the model the block is
    // untrusted external input and NOT instructions; the framing must be present
    const { handler, opts } = buildChannel();
    const { res } = fakeRes();
    const body = Buffer.from('{"event":"push"}');

    await handler.handle(
      makeReq({ 'x-hub-signature-256': makeHmac(body) }),
      res,
      body,
    );

    const [, message] = opts.onMessageSpy.mock.calls[0] as [
      string,
      { content: string },
    ];
    // The framing must signal the model that the block is untrusted external input.
    // We do NOT prescribe the exact wording — just that it is present and the
    // model-facing content carries the "do not obey" semantic.
    // Check for keywords that any reasonable framing would include:
    const lower = message.content.toLowerCase();
    const hasUntrustedSignal =
      lower.includes('untrusted') ||
      lower.includes('do not obey') ||
      lower.includes('external webhook') ||
      lower.includes('not instructions') ||
      lower.includes('ignore instructions') ||
      lower.includes('webhook payload') ||
      lower.includes('sentinel');
    expect(hasUntrustedSignal).toBe(true);
  });

  it('@oracle handle produces a unique sentinel marker per request (two calls → two different tokens)', async () => {
    // @oracle: spec B — per-request random sentinel marker; two different requests must
    // produce different tokens so an attacker cannot predict or close the framing
    const { handler, opts } = buildChannel();
    const body = Buffer.from('{"event":"push"}');

    const { res: res1 } = fakeRes();
    await handler.handle(
      makeReq({ 'x-hub-signature-256': makeHmac(body) }),
      res1,
      body,
    );

    const { res: res2 } = fakeRes();
    await handler.handle(
      makeReq({ 'x-hub-signature-256': makeHmac(body) }),
      res2,
      body,
    );

    const [, msg1] = opts.onMessageSpy.mock.calls[0] as [
      string,
      { content: string },
    ];
    const [, msg2] = opts.onMessageSpy.mock.calls[1] as [
      string,
      { content: string },
    ];

    // The two messages must differ in at least one character (different sentinel tokens)
    expect(msg1.content).not.toBe(msg2.content);
  });
});

// =============================================================================
// CASE B5 — ownsJid
// =============================================================================
describe('@oracle webhook channel — ownsJid predicate', () => {
  it('@oracle ownsJid("webhook:anything") returns true', () => {
    // @oracle: spec B — ownsJid must return true for any webhook: JID
    const { channel } = buildChannel();
    expect(channel.ownsJid('webhook:github')).toBe(true);
    expect(channel.ownsJid('webhook:stripe')).toBe(true);
    expect(channel.ownsJid('webhook:anything-at-all')).toBe(true);
  });

  it('@oracle ownsJid("123@g.us") returns false', () => {
    // @oracle: spec B — ownsJid must return false for non-webhook JIDs
    const { channel } = buildChannel();
    expect(channel.ownsJid('123@g.us')).toBe(false);
    expect(channel.ownsJid('tg:12345')).toBe(false);
    expect(channel.ownsJid('linear:LIA-1')).toBe(false);
  });
});

// ─── Local helper used in C1b: build a minimal valid source config ────────────

function validSource(over: {
  name: string;
  targetGroupFolder: string;
}): typeof SINGLE_SOURCE {
  return {
    name: over.name,
    hmacHeader: 'x-hub-signature-256',
    hmacSecret: KNOWN_SECRET,
    replayStrategy: 'none' as const,
    targetGroupFolder: over.targetGroupFolder,
  };
}

// =============================================================================
// CASE C1 — R3 provisioning: conflict with existing non-publicIngress group
//            → FATAL-SKIP that source; other valid sources still register
// =============================================================================
describe('@oracle webhook channel connect() — R3 fatal-skip on folder conflict', () => {
  it('@oracle connect() skips a source whose targetGroupFolder is already owned by a non-publicIngress group', async () => {
    // @oracle: spec C — a source whose targetGroupFolder matches an already-registered
    // non-publicIngress group must be FATAL-SKIPPED: no registerGroup called for it,
    // and it must NOT produce a usable dispatch route (ownsJid route absent or verify fails closed)
    const conflictingFolder = SOURCE_FOLDER; // same as SINGLE_SOURCE.targetGroupFolder
    const registeredGroupJid = `existing:${conflictingFolder}`;

    const capturedGroups: Array<{
      jid: string;
      group: import('../types.js').RegisteredGroup;
    }> = [];

    // Simulate: registeredGroups() already contains a group at that folder that is NOT publicIngress
    const existingGroups: Record<
      string,
      import('../types.js').RegisteredGroup
    > = {
      [registeredGroupJid]: {
        name: 'existing-non-public',
        folder: conflictingFolder,
        trigger: 'always',
        added_at: '2026-01-01T00:00:00.000Z',
        containerConfig: { publicIngress: false },
      },
    };

    const opts: ChannelOpts & { capturedGroups: typeof capturedGroups } = {
      onMessage: vi.fn(),
      onReaction: vi.fn(),
      onChatMetadata: vi.fn(),
      registeredGroups: () => existingGroups,
      capturedGroups,
    };

    const registerGroupSpy = vi.fn(
      (jid: string, group: import('../types.js').RegisteredGroup) => {
        capturedGroups.push({ jid, group });
      },
    );

    const capturedHandlers: IngressHandler[] = [];
    const channel = createWebhookChannel(
      opts,
      [SINGLE_SOURCE], // single source, but its folder conflicts
      (h: IngressHandler) => capturedHandlers.push(h),
      registerGroupSpy, // injected registerGroup dependency
    );

    if (!channel) throw new Error('factory returned null');

    await channel.connect();

    // The conflicting source must NOT have caused a registerGroup call
    expect(registerGroupSpy).not.toHaveBeenCalled();
  });

  it('@oracle connect() still registers non-conflicting sources when one is fatal-skipped', async () => {
    // @oracle: spec C — other valid sources still register even if one is fatal-skipped
    const conflictingFolder = 'sandbox-conflict';
    const safeFolder = 'sandbox-safe';

    const existingGroups: Record<
      string,
      import('../types.js').RegisteredGroup
    > = {
      'existing:sandbox-conflict': {
        name: 'existing',
        folder: conflictingFolder,
        trigger: 'always',
        added_at: '2026-01-01T00:00:00.000Z',
        containerConfig: { publicIngress: false },
      },
    };

    const opts: ChannelOpts = {
      onMessage: vi.fn(),
      onReaction: vi.fn(),
      onChatMetadata: vi.fn(),
      registeredGroups: () => existingGroups,
    };

    const capturedGroups: Array<{
      jid: string;
      group: import('../types.js').RegisteredGroup;
    }> = [];
    const registerGroupSpy = vi.fn(
      (jid: string, group: import('../types.js').RegisteredGroup) => {
        capturedGroups.push({ jid, group });
      },
    );

    const capturedHandlers: IngressHandler[] = [];
    const channel = createWebhookChannel(
      opts,
      [
        validSource({
          name: 'bad-source',
          targetGroupFolder: conflictingFolder,
        }),
        validSource({ name: 'good-source', targetGroupFolder: safeFolder }),
      ],
      (h: IngressHandler) => capturedHandlers.push(h),
      registerGroupSpy,
    );

    if (!channel) throw new Error('factory returned null');
    await channel.connect();

    // The non-conflicting source MUST have been registered
    const registeredJids = capturedGroups.map((g) => g.jid);
    expect(registeredJids).toContain('webhook:good-source');
    // The conflicting source must NOT appear
    expect(registeredJids).not.toContain('webhook:bad-source');
  });
});

// =============================================================================
// CASE C2 — R3 provisioning: valid source → registerGroup with publicIngress + curatedTools=[]
// =============================================================================
describe('@oracle webhook channel connect() — provisioned source has publicIngress + curatedTools=[]', () => {
  it('@oracle registerGroup is called with jid "webhook:<name>" and publicIngress===true, curatedTools===[]', async () => {
    // @oracle: spec C — a provisioned source must registerGroup with:
    //   jid = 'webhook:<name>', containerConfig.publicIngress===true, curatedTools===[] (notify-only)
    const capturedGroups: Array<{
      jid: string;
      group: import('../types.js').RegisteredGroup;
    }> = [];

    const opts: ChannelOpts = {
      onMessage: vi.fn(),
      onReaction: vi.fn(),
      onChatMetadata: vi.fn(),
      registeredGroups: () => ({}), // no pre-existing groups = no conflict
    };

    const registerGroupSpy = vi.fn(
      (jid: string, group: import('../types.js').RegisteredGroup) => {
        capturedGroups.push({ jid, group });
      },
    );

    const capturedHandlers: IngressHandler[] = [];
    const channel = createWebhookChannel(
      opts,
      [SINGLE_SOURCE],
      (h: IngressHandler) => capturedHandlers.push(h),
      registerGroupSpy,
    );

    if (!channel) throw new Error('factory returned null');
    await channel.connect();

    expect(capturedGroups).toHaveLength(1);
    const { jid, group } = capturedGroups[0]!;
    expect(jid).toBe(`webhook:${SOURCE_NAME}`);
    expect(group.containerConfig?.publicIngress).toBe(true);
    expect(group.containerConfig?.curatedTools).toEqual([]);
  });
});
