import crypto from 'crypto';
import fs from 'fs';

import { SENDER_ALLOWLIST_PATH } from './config.js';
import { logger } from './logger.js';

export interface ChatAllowlistEntry {
  allow: '*' | string[];
  mode: 'trigger' | 'drop';
}

export interface SenderAllowlistConfig {
  default: ChatAllowlistEntry;
  chats: Record<string, ChatAllowlistEntry>;
  logDenied: boolean;
}

/**
 * Returned when NO config file exists — the feature is simply not configured,
 * so every sender is allowed. Absent must stay distinct from corrupt.
 */
const DEFAULT_CONFIG: SenderAllowlistConfig = {
  default: { allow: '*', mode: 'trigger' },
  chats: {},
  logDenied: true,
};

/**
 * Returned when a config file EXISTS but cannot be trusted. Deny rather than
 * widen to allow-all: the operator wrote a restriction and a typo must not
 * discard it.
 */
const DENY_ALL_CONFIG: SenderAllowlistConfig = {
  // mode is 'trigger' and NEVER 'drop': a true shouldDropMessage() routes to a
  // branch in src/index.ts that returns BEFORE storing, so a deny-all in drop
  // mode would silently destroy inbound messages. Deny the privileged action
  // (agent invocation), never retention. Safe for the operator because every
  // trigger-gating call site short-circuits on isControlGroup / is_from_me
  // first — see src/message-orchestrator.ts.
  default: { allow: [], mode: 'trigger' },
  chats: {},
  logDenied: true,
};

/** Substituted for a single per-chat entry that failed validation. */
const DENY_ALL_ENTRY: ChatAllowlistEntry = { allow: [], mode: 'trigger' };

/**
 * The single most-recently-logged config-file state — path plus either a
 * content hash or the read-error code — so a broken file is reported once
 * rather than on every read. The path is part of the key because the key
 * identifies a FILE's state, not merely a failure kind.
 *
 * One slot, not a seen-set: alternating between two broken files would re-log
 * each time. Harmless in production, where the path is a single constant.
 */
// Deliberately a log-suppression key, NOT a config cache: the loader stays
// uncached so fixing the file takes effect on the next message with no restart.
// Unlike mount-security.ts's loadMountAllowlist, which caches for the process
// lifetime — do not "align" them without preserving that self-healing property.
let lastReportedState: string | null = null;

/**
 * True at most once per distinct config-file state. Call once per load and
 * gate every problem log for that load on the result, so one broken file
 * produces one complete report rather than a partial one per read.
 */
function shouldReport(stateKey: string): boolean {
  if (stateKey === lastReportedState) return false;
  lastReportedState = stateKey;
  return true;
}

function cloneEntry(entry: ChatAllowlistEntry): ChatAllowlistEntry {
  return {
    allow: Array.isArray(entry.allow) ? [...entry.allow] : entry.allow,
    mode: entry.mode,
  };
}

/** Defensive copy so callers can never mutate a shared module-level config. */
function cloneConfig(cfg: SenderAllowlistConfig): SenderAllowlistConfig {
  const chats: Record<string, ChatAllowlistEntry> = {};
  for (const [jid, entry] of Object.entries(cfg.chats)) {
    chats[jid] = cloneEntry(entry);
  }
  return { default: cloneEntry(cfg.default), chats, logDenied: cfg.logDenied };
}

/** A non-null, non-array object — the only shape a config or chats map may take. */
function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isValidEntry(entry: unknown): entry is ChatAllowlistEntry {
  if (!entry || typeof entry !== 'object') return false;
  const e = entry as Record<string, unknown>;
  const validAllow =
    e.allow === '*' ||
    (Array.isArray(e.allow) && e.allow.every((v) => typeof v === 'string'));
  const validMode = e.mode === 'trigger' || e.mode === 'drop';
  return validAllow && validMode;
}

export function loadSenderAllowlist(
  pathOverride?: string,
): SenderAllowlistConfig {
  const filePath = pathOverride ?? SENDER_ALLOWLIST_PATH;

  let raw: string;
  try {
    raw = fs.readFileSync(filePath, 'utf-8');
  } catch (err: unknown) {
    const code = (err as NodeJS.ErrnoException).code;
    // No file at all: the feature is not configured, so allow everyone. This is
    // the ONLY path that may widen access on failure.
    if (code === 'ENOENT') {
      lastReportedState = null;
      return cloneConfig(DEFAULT_CONFIG);
    }
    // The file exists but we cannot read it (EACCES, EISDIR, I/O error). We do
    // not know what it says, so we must not assume it said "allow everyone".
    if (shouldReport(`${filePath}:read:${code ?? 'unknown'}`)) {
      logger.error(
        { err, path: filePath },
        'sender-allowlist: config exists but cannot be read — denying all senders until it is readable',
      );
    }
    return cloneConfig(DENY_ALL_CONFIG);
  }

  // Key the log suppression on the file's exact contents, so a broken file is
  // reported once and re-reported the moment it actually changes.
  const report = shouldReport(
    `${filePath}:${crypto.createHash('sha1').update(raw).digest('hex')}`,
  );

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (err: unknown) {
    if (report) {
      logger.error(
        { err, path: filePath },
        'sender-allowlist: invalid JSON — denying all senders until it is fixed',
      );
    }
    return cloneConfig(DENY_ALL_CONFIG);
  }

  // `null`, an array and a scalar are all valid JSON but not a config object.
  // Guard before any property access: reading `.default` off null would throw
  // out of this function, on the per-message path.
  if (!isPlainObject(parsed)) {
    if (report) {
      logger.error(
        { path: filePath },
        'sender-allowlist: config root is not an object — denying all senders until it is fixed',
      );
    }
    return cloneConfig(DENY_ALL_CONFIG);
  }

  const obj = parsed;

  if (!isValidEntry(obj.default)) {
    if (report) {
      logger.error(
        { path: filePath },
        'sender-allowlist: invalid or missing default entry — denying all senders until it is fixed',
      );
    }
    return cloneConfig(DENY_ALL_CONFIG);
  }

  // A present-but-malformed `chats` container means the operator's per-chat
  // restrictions are unreadable. Collapsing it to an empty map would silently
  // delete them and fall through to `default`, so treat it as untrustworthy.
  // Absent is fine — it simply means no per-chat overrides.
  if (obj.chats !== undefined && !isPlainObject(obj.chats)) {
    if (report) {
      logger.error(
        { path: filePath },
        'sender-allowlist: chats is not an object — denying all senders until it is fixed',
      );
    }
    return cloneConfig(DENY_ALL_CONFIG);
  }

  const chats: Record<string, ChatAllowlistEntry> = {};
  if (isPlainObject(obj.chats)) {
    for (const [jid, entry] of Object.entries(obj.chats)) {
      if (isValidEntry(entry)) {
        chats[jid] = cloneEntry(entry);
      } else {
        // Deny this chat rather than dropping the entry. A dropped entry falls
        // through to `cfg.default` in getEntry(), which would WIDEN access past
        // what the operator explicitly wrote for this chat.
        chats[jid] = cloneEntry(DENY_ALL_ENTRY);
        if (report) {
          logger.error(
            { jid, path: filePath },
            'sender-allowlist: invalid chat entry — denying that chat until it is fixed',
          );
        }
      }
    }
  }

  return {
    default: cloneEntry(obj.default),
    chats,
    logDenied: obj.logDenied !== false,
  };
}

function getEntry(
  chatJid: string,
  cfg: SenderAllowlistConfig,
): ChatAllowlistEntry {
  return cfg.chats[chatJid] ?? cfg.default;
}

export function isSenderAllowed(
  chatJid: string,
  sender: string,
  cfg: SenderAllowlistConfig,
): boolean {
  const entry = getEntry(chatJid, cfg);
  if (entry.allow === '*') return true;
  return entry.allow.includes(sender);
}

export function shouldDropMessage(
  chatJid: string,
  cfg: SenderAllowlistConfig,
): boolean {
  return getEntry(chatJid, cfg).mode === 'drop';
}

export function isTriggerAllowed(
  chatJid: string,
  sender: string,
  cfg: SenderAllowlistConfig,
): boolean {
  const allowed = isSenderAllowed(chatJid, sender, cfg);
  if (!allowed && cfg.logDenied) {
    logger.debug(
      { chatJid, sender },
      'sender-allowlist: trigger denied for sender',
    );
  }
  return allowed;
}
