// Regression coverage for createWebhookChannel beyond the blind @oracle suite.
// Specifically locks the R3 fatal-skip fix surfaced by the GPT code-reviewer
// co-gate: a source whose targetGroupFolder collides with an existing
// non-publicIngress group must have an INERT route (verify rejects it), not
// merely an un-provisioned group — otherwise it could still pass HMAC + dispatch.

import crypto from 'node:crypto';
import { describe, it, expect, vi } from 'vitest';
import { createWebhookChannel } from './webhook.js';
import type { ChannelOpts } from './registry.js';
import type { IngressHandler } from '../ingress/gateway.js';
import type { OnInboundMessage, RegisteredGroup } from '../types.js';

const SECRET = 'regression-secret';

function source(name: string, targetGroupFolder: string) {
  return {
    name,
    hmacHeader: 'x-hub-signature-256',
    hmacSecret: SECRET,
    replayStrategy: 'none' as const,
    targetGroupFolder,
  };
}

function makeReq(url: string, sig: string, _body: Buffer) {
  return {
    headers: { 'x-hub-signature-256': sig },
    url,
    socket: { remoteAddress: '127.0.0.1' },
  } as unknown as import('http').IncomingMessage;
}

function sign(body: Buffer): string {
  return (
    'sha256=' + crypto.createHmac('sha256', SECRET).update(body).digest('hex')
  );
}

function fakeRes() {
  const out: { status?: number } = {};
  const res = {
    writeHead: (s: number) => {
      out.status = s;
      return res;
    },
    end: () => {},
    writableEnded: false,
  } as unknown as import('http').ServerResponse;
  return { res, out };
}

function build(
  sources: ReturnType<typeof source>[],
  registeredGroups: Record<string, RegisteredGroup>,
) {
  const handlers: IngressHandler[] = [];
  const opts: ChannelOpts = {
    onMessage: vi.fn() as unknown as OnInboundMessage,
    onReaction: vi.fn(),
    onChatMetadata: vi.fn(),
    registeredGroups: () => registeredGroups,
  };
  const channel = createWebhookChannel(opts, sources, (h) => handlers.push(h));
  return {
    channel,
    handler: handlers[0]!,
    onMessage: opts.onMessage,
    onChatMetadata: opts.onChatMetadata as ReturnType<typeof vi.fn>,
  };
}

describe('createWebhookChannel — R3 fatal-skip makes the route inert', () => {
  it('a source colliding with an existing non-publicIngress group is rejected by verify (403)', async () => {
    const collidingFolder = 'whatsapp_main';
    const { handler } = build([source('evil', collidingFolder)], {
      'main@g.us': {
        name: 'Main',
        folder: collidingFolder,
        trigger: 'always',
        added_at: '2026-01-01T00:00:00.000Z',
        containerConfig: { publicIngress: false },
      },
    });
    const body = Buffer.from('{"x":1}');
    // Even with a VALID signature, the skipped source's route must be unknown.
    const result = await handler.verify(
      makeReq('/hook/evil', sign(body), body),
      body,
    );
    expect(result.ok).toBe(false);
  });

  it('a non-colliding source in the same config still has a live, verifying route', async () => {
    const { handler } = build(
      [source('evil', 'whatsapp_main'), source('good', 'webhook-sandbox-good')],
      {
        'main@g.us': {
          name: 'Main',
          folder: 'whatsapp_main',
          trigger: 'always',
          added_at: '2026-01-01T00:00:00.000Z',
          containerConfig: { publicIngress: false },
        },
      },
    );
    const body = Buffer.from('{"ok":true}');
    expect(
      (await handler.verify(makeReq('/hook/good', sign(body), body), body)).ok,
    ).toBe(true);
    expect(
      (await handler.verify(makeReq('/hook/evil', sign(body), body), body)).ok,
    ).toBe(false);
  });

  it('a source whose folder is owned by a DIFFERENT webhook jid is rejected (no cross-source container sharing)', async () => {
    const folder = 'webhook-sandbox-shared';
    // An existing (e.g. stale/renamed) publicIngress group owns the folder under a different jid.
    const { handler } = build([source('newsrc', folder)], {
      'webhook:oldsrc': {
        name: 'Webhook: oldsrc',
        folder,
        trigger: '',
        added_at: '2026-01-01T00:00:00.000Z',
        containerConfig: { publicIngress: true, curatedTools: [] },
      },
    });
    const body = Buffer.from('{"x":1}');
    expect(
      (await handler.verify(makeReq('/hook/newsrc', sign(body), body), body))
        .ok,
    ).toBe(false);
  });

  it('the SAME jid re-owning its folder is allowed (idempotent restart)', async () => {
    const folder = 'webhook-sandbox-self';
    const { handler } = build([source('selfsrc', folder)], {
      'webhook:selfsrc': {
        name: 'Webhook: selfsrc',
        folder,
        trigger: '',
        added_at: '2026-01-01T00:00:00.000Z',
        containerConfig: { publicIngress: true, curatedTools: [] },
      },
    });
    const body = Buffer.from('{"x":1}');
    expect(
      (await handler.verify(makeReq('/hook/selfsrc', sign(body), body), body))
        .ok,
    ).toBe(true);
  });
});

describe('createWebhookChannel — registers the chat lazily on inbound (FK fix)', () => {
  it('does NOT call onChatMetadata at construction (no startup recency pollution)', () => {
    // Registering chats at startup would bump chats.last_message_time on every
    // restart (storeChatMetadata MAX()), making idle webhook chats look active.
    const { onChatMetadata } = build(
      [source('good', 'webhook-sandbox-good')],
      {},
    );
    expect(onChatMetadata).not.toHaveBeenCalled();
  });

  it('handle() registers the chat (onChatMetadata, channel "webhook", event timestamp) before onMessage', async () => {
    // The chats row must exist before storeMessage (messages.chat_jid FKs to
    // chats.jid) or the first event throws SQLITE_CONSTRAINT_FOREIGNKEY silently.
    const { handler, onChatMetadata, onMessage } = build(
      [source('good', 'webhook-sandbox-good')],
      {},
    );
    const body = Buffer.from('{"event":"x"}');
    const { res } = fakeRes();
    await handler.handle(makeReq('/hook/good', sign(body), body), res, body);

    expect(onChatMetadata).toHaveBeenCalledTimes(1);
    const [chatJid, ts, , channel] = onChatMetadata.mock.calls[0] as [
      string,
      string,
      string,
      string,
      boolean,
    ];
    expect(chatJid).toBe('webhook:good');
    expect(channel).toBe('webhook');
    // Same timestamp as the dispatched message (correct recency, not startup time).
    const [, msg] = (onMessage as unknown as ReturnType<typeof vi.fn>).mock
      .calls[0] as [string, { timestamp: string }];
    expect(ts).toBe(msg.timestamp);
  });
});
