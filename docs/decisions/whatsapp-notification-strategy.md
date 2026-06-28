# ADR: WhatsApp Notification Strategy — Stay Offline on Connect, No Reply-Alert Relay

**Date:** 2026-06-18
**Status:** Accepted
**Scope:** `packages/mcp-whatsapp/src/whatsapp.ts`; WhatsApp channel notification behavior
**Deciders:** Deus (user + agent)
**Relates to:** PR #879; RETRO-2026-06-17-01 (re-issue of -06-15-05); `Research/whatsapp-reply-notification-options.md` (vault)

## Context

Deus runs on the user's own WhatsApp number as a Baileys companion (linked)
device (`ASSISTANT_HAS_OWN_NUMBER=false`, the default). Two distinct phone-push
gaps were reported and investigated. They are independent problems with
independent fixes — conflating them is the trap this ADR exists to prevent.

1. **Inbound suppression.** Baileys defaults `markOnlineOnConnect: true`, which
   broadcasts `'available'` presence on connect. A linked device that announces
   itself online tells WhatsApp the user is actively reading there, so the
   server suppresses push notifications to the phone for messages **other people
   send** while Deus is connected.

2. **Reply suppression.** Every message Deus *sends* is flagged `fromMe=true`.
   WhatsApp's multi-device protocol syncs your own sent messages to linked
   devices as encrypted history; push (APNs/FCM) only fires on the server
   INBOUND path. So the user gets **no phone push when Deus replies to them**.
   This is a server-side / protocol rule, reproduced across multiple libraries
   (whatsmeow #605, whatsapp-web.js, WPPConnect, venom, open-wa) — not a library
   limitation, and NOT fixable by a library swap or by any presence change.

## Decision

### 1. Stay offline on connect (fixes inbound suppression) — SHIPPED in PR #879

Two coordinated changes in the `WhatsAppProvider`:

- Set `markOnlineOnConnect: false` on `makeWASocket`.
- Replace the explicit `sendPresenceUpdate('available')` in the
  `connection === 'open'` handler with `sendPresenceUpdate('unavailable')`. The
  prior `'available'` call fired on every (re)connect and would otherwise
  override the flag's intent and re-mark the device online; the new call
  reinforces the offline posture on each (re)connect.

Both are needed because the explicit call takes effect regardless of the flag.
Baileys' own on-connect presence honours the flag as well — it sends
`'available'` when `markOnlineOnConnect` is true and `'unavailable'` when false
(verified against the pinned Baileys 7.x source, `Socket/chats.js`) — so after
this change the explicit call and the flag agree.

Result: Deus no longer claims to be reading on the linked device, so the
phone's native push for inbound messages is restored. Request-scoped typing
presence (`setTyping`, `'composing'` / `'paused'`) is unchanged. On-device push
verification is still required and is not guaranteed on every iOS build
(Baileys #607).

### 2. No reply-alert relay (reply suppression) — DROP, accept the limitation

We will **not** add any mechanism to alert the user when Deus replies. The
option space was fully researched:

| Option | Real alert? | Verdict |
|---|---|---|
| CallMeBot relay ping | Yes | **Rejected** — borrows a third-party WhatsApp sender that learns the user's number + reply timing even with a content-free body; fails the self-hosted/privacy bar. |
| Self-mention (Deus @mentions own JID) | No | **Refuted on-device 2026-06-15** — `fromMe` suppression beats mention-elevation; two live runs, zero notifications. |
| Self-hosted ntfy / Telegram notifier | Yes | Most private real-alert, but cross-channel (out-of-WhatsApp); declined to keep notifications in-domain. |
| Unread badge (`chatModify markRead:false`) | Visual only | Unreliable (Baileys #1406), auto-clears on open. |
| Second / dedicated number | Yes | Off the table — no second number available. |

The only in-WhatsApp real-alert (CallMeBot) is unacceptable on security/privacy
grounds; the only non-third-party in-WhatsApp option (self-mention) is
empirically dead; cross-channel is out of scope by user preference. Therefore
we accept that Deus's own replies do not push to the phone.

## Consequences

### Positive
- Inbound messages from others again trigger native phone push (no more silent
  suppression while Deus is connected).
- No third-party relay in the notification path; nothing learns the user's
  number or messaging cadence.
- Closes a decision that drifted across three retro cycles, so it stops being
  re-researched.

### Negative / Trade-offs
- The user still receives no phone push when Deus itself replies (`fromMe`).
  Accepted for now; the user sees replies when they next open WhatsApp.
- iOS reliability of the inbound-presence fix is imperfect (Baileys #607) —
  on-device verification is required.

## Revisit if
- A dedicated/second WhatsApp number becomes available
  (`ASSISTANT_HAS_OWN_NUMBER=true` removes the `fromMe` problem entirely).
- The user later accepts a self-hosted ntfy / Telegram notifier despite it being
  cross-channel.
- New evidence surfaces a `fromMe` push path (none found 2024–2026).

## Alternatives Considered

See the options table above. The full ranked research, sources, and the
on-device refutation of self-mention live in the vault at
`Research/whatsapp-reply-notification-options.md`.

## References
- PR #879 — `markOnlineOnConnect: false` + on-connect `'unavailable'`.
- `Research/whatsapp-reply-notification-options.md` (vault) — full option
  research + on-device refutation of self-mention.
- RETRO-2026-06-17-01 — the recommendation this closes.
- Baileys `Socket/chats.js:1060` (on-connect presence honours the flag);
  `Defaults/index.js:62` (default `true`).
- whatsmeow #605, Baileys #1406, Baileys #607, Meta Engineering multi-device
  (2021).
