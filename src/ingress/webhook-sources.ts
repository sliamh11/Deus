// LIA-315 Phase 4: operator-owned per-source webhook config loader.
//
// Reads WEBHOOK_SOURCES_PATH (under CONFIG_DIR, never mounted into a container)
// and returns a validated, typed source list. Validation is FAIL-CLOSED — a
// malformed config throws rather than silently dropping a source (a dropped
// source is a silent security/availability change). The R3 "1:1 source↔sandbox"
// invariant is enforced here at load time (duplicate name OR duplicate
// targetGroupFolder → throw); the channel enforces the rest of R3 (no targeting
// an existing non-publicIngress group) at connect().
//
// Secrets are never stored in the file: an `hmacSecret` of the form `<env:NAME>`
// is resolved from the environment (process.env wins over the .env file, matching
// index.ts precedence). A literal secret is allowed too (e.g. for tests).

import { existsSync, readFileSync } from 'fs';
import { readEnvFile } from '../env.js';
import { isValidGroupFolder } from '../group-folder.js';
import type { ReplayStrategy } from './hmac.js';

/** Source `name` is used as the `/hook/<name>` path segment, the rate-limiter
 *  Map key, AND interpolated into the trusted region of the agent prompt, so it
 *  MUST be a tight charset (no path traversal, no spaces, no prompt-breaking
 *  characters) — this regex is a security boundary, not cosmetics. Exported so
 *  `buildWebhookPrompt` can re-assert it at the interpolation site (co-located
 *  invariant) rather than trusting the load-time check from afar. */
export const NAME_RE = /^[a-z0-9-]+$/;
const VALID_STRATEGIES: ReadonlySet<string> = new Set([
  'delivery-id',
  'timestamp-nonce',
  'none',
]);
/** `<env:VAR>` interpolation marker for the hmacSecret field. */
const ENV_REF_RE = /^<env:([A-Za-z_][A-Za-z0-9_]*)>$/;

export interface WebhookSource {
  /** Lowercase slug; the `/hook/<name>` segment + rate-limiter key. */
  name: string;
  /** Header carrying the HMAC signature (e.g. `X-Hub-Signature-256`). */
  hmacHeader: string;
  /** Resolved shared secret (never the `<env:…>` template). */
  hmacSecret: string;
  /** Replay-protection strategy (per-source). */
  replayStrategy: ReplayStrategy;
  /** Sandbox group folder this source dispatches into (R3: dedicated 1:1). */
  targetGroupFolder: string;
  /** Header carrying the unique delivery id (strategy 'delivery-id'). */
  idHeader?: string;
  /** Header carrying the epoch-ms timestamp (strategy 'timestamp-nonce'). */
  tsHeader?: string;
  /** Header carrying the nonce (strategy 'timestamp-nonce'). */
  nonceHeader?: string;
  /** Host-brokered curated tools (∩ SAFE_CURATED at run time); [] = notify-only. */
  curatedTools?: string[];
  // NOTE: per-source peer-IP filtering is intentionally NOT a field here. Behind
  // the ngrok tunnel the socket peer is always the tunnel's loopback, so a
  // peer-IP allowlist cannot see the real client — real-client IP policy belongs
  // at the ngrok edge (see ingress/gateway.ts:45-48). Shipping an unenforced
  // `allowedIps` would be a false security control.
}

function resolveSecret(raw: unknown, sourceName: string): string {
  if (typeof raw !== 'string' || raw.length === 0) {
    throw new Error(
      `webhook-sources: source "${sourceName}" missing hmacSecret`,
    );
  }
  const m = ENV_REF_RE.exec(raw);
  if (!m) return raw; // literal secret
  const key = m[1]!;
  const val = process.env[key] ?? readEnvFile([key])[key];
  if (!val) {
    throw new Error(
      `webhook-sources: source "${sourceName}" hmacSecret env ${key} is not set`,
    );
  }
  return val;
}

function strField(
  obj: Record<string, unknown>,
  key: string,
): string | undefined {
  const v = obj[key];
  return typeof v === 'string' ? v : undefined;
}

function strArray(
  obj: Record<string, unknown>,
  key: string,
): string[] | undefined {
  const v = obj[key];
  if (!Array.isArray(v)) return undefined;
  return v.filter((x): x is string => typeof x === 'string');
}

/**
 * Load + validate the webhook source config. Returns `[]` when the file is
 * absent (no config = no sources, not an error). Throws on any structural or
 * uniqueness violation (fail-closed).
 *
 * Accepts either a bare JSON array of sources or a `{ "sources": [...] }` object.
 */
export function loadWebhookSources(path: string): WebhookSource[] {
  if (!existsSync(path)) return [];

  let parsed: unknown;
  try {
    parsed = JSON.parse(readFileSync(path, 'utf8'));
  } catch (err) {
    throw new Error(`webhook-sources: invalid JSON at ${path}`, { cause: err });
  }

  const rawList: unknown = Array.isArray(parsed)
    ? parsed
    : parsed &&
        typeof parsed === 'object' &&
        Array.isArray((parsed as { sources?: unknown }).sources)
      ? (parsed as { sources: unknown[] }).sources
      : null;
  if (!rawList || !Array.isArray(rawList)) {
    throw new Error(
      'webhook-sources: expected a JSON array or { "sources": [...] }',
    );
  }

  const names = new Set<string>();
  const folders = new Set<string>();
  const out: WebhookSource[] = [];

  for (const entry of rawList) {
    if (!entry || typeof entry !== 'object') {
      throw new Error('webhook-sources: each source must be an object');
    }
    const s = entry as Record<string, unknown>;

    const name = s.name;
    if (typeof name !== 'string' || !NAME_RE.test(name)) {
      throw new Error(
        `webhook-sources: invalid source name ${JSON.stringify(
          name,
        )} (must match /^[a-z0-9-]+$/)`,
      );
    }
    if (names.has(name)) {
      throw new Error(`webhook-sources: duplicate source name "${name}"`);
    }

    const targetGroupFolder = strField(s, 'targetGroupFolder');
    if (!targetGroupFolder) {
      throw new Error(
        `webhook-sources: source "${name}" missing targetGroupFolder`,
      );
    }
    // Fail closed on an invalid folder HERE (load time) — the same contract
    // RouterState.registerGroup enforces. Otherwise an invalid folder would still
    // register a live /hook route (accepting signed requests → 202) while
    // registerGroup later rejects the folder, so no sandbox ever processes them.
    if (!isValidGroupFolder(targetGroupFolder)) {
      throw new Error(
        `webhook-sources: source "${name}" invalid targetGroupFolder ${JSON.stringify(
          targetGroupFolder,
        )}`,
      );
    }
    if (folders.has(targetGroupFolder)) {
      throw new Error(
        `webhook-sources: duplicate targetGroupFolder "${targetGroupFolder}" ` +
          '(R3 requires a dedicated sandbox folder per source)',
      );
    }

    const hmacHeader = strField(s, 'hmacHeader');
    if (!hmacHeader) {
      throw new Error(`webhook-sources: source "${name}" missing hmacHeader`);
    }

    const strategy = strField(s, 'replayStrategy') ?? 'none';
    if (!VALID_STRATEGIES.has(strategy)) {
      throw new Error(
        `webhook-sources: source "${name}" invalid replayStrategy ${JSON.stringify(
          strategy,
        )}`,
      );
    }

    const hmacSecret = resolveSecret(s.hmacSecret, name);

    names.add(name);
    folders.add(targetGroupFolder);
    out.push({
      name,
      hmacHeader,
      hmacSecret,
      replayStrategy: strategy as ReplayStrategy,
      targetGroupFolder,
      idHeader: strField(s, 'idHeader'),
      tsHeader: strField(s, 'tsHeader'),
      nonceHeader: strField(s, 'nonceHeader'),
      curatedTools: strArray(s, 'curatedTools'),
    });
  }

  return out;
}
