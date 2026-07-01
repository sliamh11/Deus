// R6 (LIA-315 Phase 3): append-only, secret-scrubbed, per-event audit sink for the
// ingress gateway. Every webhook-originated event gets a durable forensic record on
// a host path that is NEVER mounted into any container (the writable-path isolation
// the threat model requires). This is the per-EVENT audit — distinct from the
// gateway's per-REQUEST scrubbed log (`gateway.ts`), which goes to the pino logger.
//
// Dormant in Phase 3: no caller yet. Phase 4 (webhook dispatch) wires the admission
// facade (`caps.ts`) to call `appendAuditEvent` before any dispatch.
//
// Cross-platform: all paths derive from `CONFIG_DIR` (src/config.ts), built via
// `path.join(HOME_DIR, '.config', 'deus')` — already OS-portable. No `platform.ts`
// routing is needed here (the construction uses only `path.join`).

import { promises as fs } from 'fs';
import path from 'path';

export type IngressAuditEventType =
  'received' | 'admitted' | 'rejected' | 'dispatched';
export type IngressAuditDecision = 'admitted' | 'rejected';

/**
 * The ONLY shape that reaches disk. The field set is a deliberate whitelist of
 * non-secret metadata — there is intentionally no `body`/`headers`/`token` field,
 * so a raw request body or credential structurally cannot be recorded.
 */
export interface IngressAuditEvent {
  ts: number; // epoch ms — also selects the day-partition file
  source: string;
  event: IngressAuditEventType;
  decision?: IngressAuditDecision;
  reason?: string;
  requestId?: string;
  path?: string;
  status?: number;
  // Index signature so callers/tests can inspect the record as a plain map. The
  // type is permissive on purpose — the SECURITY guarantee is the RUNTIME
  // structural whitelist in `scrubAuditEvent` (only the named keys above are ever
  // copied onto disk), not the static shape.
  [key: string]: unknown;
}

/** Surfaces write failure to the caller (never silently swallowed). */
export type AuditWriteResult = { ok: true } | { ok: false; error: unknown };

// Free-text string fields retained onto disk. Each passes through substring
// redaction (a secret embedded in one of these is replaced, not dropped wholesale).
// `event`/`decision` are NOT here — they are controlled enums validated separately
// (never user-free-text, so nothing to redact). Everything not named here or in the
// enum/numeric handling below is dropped (structural whitelist).
const REDACTED_FIELDS = ['source', 'reason', 'requestId', 'path'] as const;

const EVENT_TYPES: ReadonlySet<string> = new Set([
  'received',
  'admitted',
  'rejected',
  'dispatched',
]);
const DECISIONS: ReadonlySet<string> = new Set(['admitted', 'rejected']);

// Secret SHAPES redacted from retained string values (defense-in-depth on top of
// the structural whitelist — a secret embedded inside an otherwise-safe field like
// `reason` or `source` is replaced, not the whole field dropped). Order matters:
// the broad bearer/sha256 patterns run before the bare-hex catch-all so a
// `sha256=<hex>` is redacted as a unit.
const REDACTION_PATTERNS: RegExp[] = [
  /Bearer\s+[A-Za-z0-9._~+/=-]+/gi, // `Bearer <token>`
  /sha\d*=[A-Fa-f0-9]+/gi, // `sha256=<hex>` signature digests
  /[A-Fa-f0-9]{32,}/g, // bare long hex tokens (API keys, digests)
];
const REDACTED = '[REDACTED]';

function redactSecrets(value: string): string {
  let out = value;
  for (const re of REDACTION_PATTERNS) out = out.replace(re, REDACTED);
  return out;
}

/**
 * Structural whitelist + secret redaction. Pure and TOTAL — never throws, even on
 * maximally-malformed input (undefined, non-string fields, missing `ts`). A throw
 * here would propagate into the fail-closed admission gate (`caps.ts`), so the
 * no-throw contract is load-bearing.
 */
export function scrubAuditEvent(raw: unknown): IngressAuditEvent {
  const src =
    raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};

  const out: IngressAuditEvent = {
    ts:
      typeof src.ts === 'number' && Number.isFinite(src.ts)
        ? (src.ts as number)
        : 0,
    source: '',
    // Validated enum: only a known event type survives; anything else defaults.
    event:
      typeof src.event === 'string' && EVENT_TYPES.has(src.event)
        ? (src.event as IngressAuditEventType)
        : 'received',
  };

  for (const key of REDACTED_FIELDS) {
    const v = src[key];
    // Non-string values for a string field are dropped (not coerced) — keeps the
    // record clean and the scrub total.
    if (typeof v === 'string') out[key] = redactSecrets(v);
  }

  if (typeof src.decision === 'string' && DECISIONS.has(src.decision)) {
    out.decision = src.decision as IngressAuditDecision;
  }
  if (typeof src.status === 'number' && Number.isFinite(src.status)) {
    out.status = src.status;
  }

  return out;
}

function dayStamp(ts: number): string {
  // UTC day partition: YYYY-MM-DD. Bad/zero ts falls back to the epoch day rather
  // than throwing — the facade always supplies a real `now`, so this is a backstop.
  const d = new Date(Number.isFinite(ts) ? ts : 0);
  return d.toISOString().slice(0, 10);
}

/**
 * Scrub → JSON line → O_APPEND to `<auditDir>/ingress-audit-YYYY-MM-DD.jsonl`
 * (day selected by the event's UTC `ts`). Creates `auditDir` if absent.
 *
 * Append-only: `fs.appendFile` opens with flag `'a'` (O_APPEND) so concurrent
 * writes never truncate and each record lands at end-of-file.
 *
 * Surfaces failure as `{ ok: false, error }` and NEVER throws into the caller —
 * the admission facade depends on this to make the pre-dispatch audit fail-closed.
 */
export async function appendAuditEvent(
  event: IngressAuditEvent,
  opts: { auditDir: string },
): Promise<AuditWriteResult> {
  try {
    const scrubbed = scrubAuditEvent(event);
    const file = path.join(
      opts.auditDir,
      `ingress-audit-${dayStamp(scrubbed.ts)}.jsonl`,
    );
    await fs.mkdir(opts.auditDir, { recursive: true });
    await fs.appendFile(file, `${JSON.stringify(scrubbed)}\n`, { flag: 'a' });
    return { ok: true };
  } catch (error) {
    return { ok: false, error };
  }
}
