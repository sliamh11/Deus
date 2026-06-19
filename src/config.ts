import path from 'path';

import {
  parseAgentBackend,
  type AgentRuntimeId,
} from './agent-runtimes/types.js';
import { readEnvFile } from './env.js';
import type { InjectionScannerConfig } from './guardrails/injection-scanner.js';
import { homeDir } from './platform.js';

// Read config values from .env (falls back to process.env).
// Secrets (API keys, tokens) are NOT read here — they are loaded only
// by the credential proxy (credential-proxy.ts), never exposed to containers.
const envConfig = readEnvFile([
  'ASSISTANT_NAME',
  'ASSISTANT_HAS_OWN_NUMBER',
  'DEUS_AGENT_BACKEND',
  'DEUS_CONTEXT_FILE_MAX_CHARS',
  'DEUS_OPENAI_MODEL',
  'LLAMA_CPP_BASE_URL',
  'LLAMA_CPP_PORT',
  'LLAMA_CPP_MODEL',
  'LLAMA_CPP_AGENT_MODEL',
  'LLAMA_CPP_GEN_MODEL',
  'LLAMA_CPP_JUDGE_MODEL',
  'LLAMA_CPP_EMBED_MODEL',
  'WEBHOOK_MAX_RETRIES',
  'WEBHOOK_BASE_DELAY_MS',
]);

export const ASSISTANT_NAME =
  process.env.ASSISTANT_NAME || envConfig.ASSISTANT_NAME || 'Deus';
export const ASSISTANT_HAS_OWN_NUMBER =
  (process.env.ASSISTANT_HAS_OWN_NUMBER ||
    envConfig.ASSISTANT_HAS_OWN_NUMBER) === 'true';
export const POLL_INTERVAL = 2000;
export const SCHEDULER_POLL_INTERVAL = 60000;

// Absolute paths needed for container mounts
export const PROJECT_ROOT = path.resolve(process.cwd());
export const HOME_DIR = homeDir;
export const CONFIG_DIR = path.join(HOME_DIR, '.config', 'deus');

// Mount security: allowlist stored OUTSIDE project root, never mounted into containers
export const MOUNT_ALLOWLIST_PATH = path.join(
  CONFIG_DIR,
  'mount-allowlist.json',
);
export const SENDER_ALLOWLIST_PATH = path.join(
  CONFIG_DIR,
  'sender-allowlist.json',
);
export const STORE_DIR = path.resolve(PROJECT_ROOT, 'store');
export const GROUPS_DIR = path.resolve(PROJECT_ROOT, 'groups');
export const DATA_DIR = path.resolve(PROJECT_ROOT, 'data');

export const CONTAINER_IMAGE =
  process.env.CONTAINER_IMAGE || 'deus-agent:latest';
export const CONTAINER_TIMEOUT = parseInt(
  process.env.CONTAINER_TIMEOUT || '1800000',
  10,
);
export const CONTAINER_MAX_OUTPUT_SIZE = parseInt(
  process.env.CONTAINER_MAX_OUTPUT_SIZE || '10485760',
  10,
); // 10MB default
export const CREDENTIAL_PROXY_PORT = parseInt(
  process.env.CREDENTIAL_PROXY_PORT || '3001',
  10,
);
export const TOOL_PROXY_PORT = parseInt(
  process.env.TOOL_PROXY_PORT || '3003',
  10,
);
// Odysseus `/v1/chat/completions` web channel (path-(a) GUI). Off by default.
// The secret ODYSSEUS_HTTP_TOKEN is NOT read here — it is loaded by
// odysseus-server.ts (readEnvFile), matching the "secrets not in config.ts" rule.
export const ODYSSEUS_HTTP_ENABLED =
  process.env.ODYSSEUS_HTTP_ENABLED === '1' ||
  process.env.ODYSSEUS_HTTP_ENABLED === 'true';
export const ODYSSEUS_HTTP_PORT = parseInt(
  process.env.ODYSSEUS_HTTP_PORT || '3005',
  10,
);

// ── Ingress gateway (centralized public inbound) ─────────────────────────────
// The single public-facing HTTP server (src/ingress/gateway.ts), fronted by the
// ngrok tunnel (src/ingress/tunnel.ts). Off by default. Secrets (NGROK_AUTHTOKEN,
// per-source HMAC secrets) are NOT read here — loaded in-module via readEnvFile,
// matching the "secrets not in config.ts" rule.
export const INGRESS_GATEWAY_ENABLED =
  process.env.INGRESS_GATEWAY_ENABLED === '1' ||
  process.env.INGRESS_GATEWAY_ENABLED === 'true';
export const INGRESS_GATEWAY_HOST =
  process.env.INGRESS_GATEWAY_HOST || '127.0.0.1';
// Default 3009: collision-free against every other service default
// (cred-proxy 3001, tool-proxy 3003, Odysseus 3005) and against the common
// Odysseus deployment port 3007. A shared port would EADDRINUSE deep in startup
// — detectPortCollision() (checks.ts) guards against it as a fatal startup check.
export const INGRESS_GATEWAY_PORT = parseInt(
  process.env.INGRESS_GATEWAY_PORT || '3009',
  10,
);
export const INGRESS_MAX_BODY_BYTES = parseInt(
  process.env.INGRESS_MAX_BODY_BYTES || String(256 * 1024),
  10,
);
export const INGRESS_RATE_LIMIT_MAX = parseInt(
  process.env.INGRESS_RATE_LIMIT_MAX || '60',
  10,
);
export const INGRESS_RATE_LIMIT_WINDOW_MS = parseInt(
  process.env.INGRESS_RATE_LIMIT_WINDOW_MS || '60000',
  10,
);
export const INGRESS_IP_ALLOWLIST = (process.env.INGRESS_IP_ALLOWLIST || '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);
export const INGRESS_TUNNEL_ENABLED =
  process.env.INGRESS_TUNNEL_ENABLED === '1' ||
  process.env.INGRESS_TUNNEL_ENABLED === 'true';
export const NGROK_STATIC_DOMAIN = process.env.NGROK_STATIC_DOMAIN || '';
// Route Linear webhooks through the centralized ingress gateway (/linear) instead of the
// standalone :3005 server. Off by default; requires INGRESS_GATEWAY_ENABLED. When off the
// standalone Linear webhook server keeps running unchanged (rollback path). LIA-315 Phase 5.
export const INGRESS_LINEAR_VIA_GATEWAY =
  process.env.INGRESS_LINEAR_VIA_GATEWAY === '1' ||
  process.env.INGRESS_LINEAR_VIA_GATEWAY === 'true';
// Route GitHub CI/PR webhooks through the gateway (/github) to drive merge-on-green +
// done-on-merge by push instead of polling. Off by default; requires INGRESS_GATEWAY_ENABLED.
// The signing secret (GITHUB_WEBHOOK_SECRET) is read in-module, not here (secrets-not-in-config).
// LIA-315 Phase 4 (GitHub source 0).
export const INGRESS_GITHUB_ENABLED =
  process.env.INGRESS_GITHUB_ENABLED === '1' ||
  process.env.INGRESS_GITHUB_ENABLED === 'true';
// Per-source webhook config, OUTSIDE project root, never mounted into containers
// (same pattern as SENDER_ALLOWLIST_PATH). Consumed in Phase 4 by the webhook channel.
export const WEBHOOK_SOURCES_PATH = path.join(
  CONFIG_DIR,
  'webhook-sources.json',
);

// LIA-315 Phase 3: R5 DoS/spend caps + R6 audit. Dormant — consumed by the Phase-4
// webhook dispatch facade (src/ingress/caps.ts + audit.ts). Guard every numeric
// flag against NaN/0/negative so a bad env value falls back to the safe default
// (an unguarded `Number('') === 0` would disable a cap; see CLAUDE.md eval-diagnostics).
function ingressPositive(raw: string | undefined, def: number): number {
  const n = Number(raw);
  return Number.isFinite(n) && n > 0 ? n : def;
}
// Global ceiling on concurrent in-flight webhook runs (shared across all sources).
export const INGRESS_MAX_INFLIGHT = ingressPositive(
  process.env.INGRESS_MAX_INFLIGHT,
  3,
);
// Per-source token-bucket: burst capacity + ms-to-refill-one-token (~10/min default).
export const INGRESS_SOURCE_RATE_CAPACITY = ingressPositive(
  process.env.INGRESS_SOURCE_RATE_CAPACITY,
  10,
);
export const INGRESS_SOURCE_RATE_REFILL_MS = ingressPositive(
  process.env.INGRESS_SOURCE_RATE_REFILL_MS,
  6000,
);
// Hard daily spend ceiling. Unit = tokens-per-day as fed by Phase 4 `recordSpend`;
// ~1M tokens/day is a deliberately conservative cap for an anonymous webhook source.
export const INGRESS_DAILY_SPEND_LIMIT = ingressPositive(
  process.env.INGRESS_DAILY_SPEND_LIMIT,
  1_000_000,
);
// Append-only per-event audit sink. Under CONFIG_DIR (operator-owned, NEVER mounted
// into a container — R6 "off the container's writable path").
export const INGRESS_AUDIT_DIR =
  process.env.INGRESS_AUDIT_DIR || path.join(CONFIG_DIR, 'ingress-audit');

export const IPC_POLL_INTERVAL = 1000;
export const IDLE_TIMEOUT = parseInt(process.env.IDLE_TIMEOUT || '1800000', 10); // 30min default — how long to keep container alive after last result
// Sessions older than this many hours are reset to a fresh start.
// Set to 0 to disable idle session reset.
export const SESSION_IDLE_RESET_HOURS = parseInt(
  process.env.SESSION_IDLE_RESET_HOURS || '8',
  10,
);
export const MAX_CONCURRENT_CONTAINERS = Math.max(
  1,
  parseInt(process.env.MAX_CONCURRENT_CONTAINERS || '5', 10) || 5,
);

export function isAutoMergeEnabled(): boolean {
  return (
    process.env.LINEAR_AUTO_MERGE === '1' ||
    process.env.LINEAR_AUTO_MERGE === 'true'
  );
}

function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

export const MAX_MESSAGE_LENGTH = parseInt(
  process.env.MAX_MESSAGE_LENGTH || '50000',
  10,
);

export const TRIGGER_PATTERN = new RegExp(
  `^@${escapeRegex(ASSISTANT_NAME)}\\b`,
  'i',
);

// Timezone for scheduled tasks (cron expressions, etc.)
// Uses system timezone by default
export const TIMEZONE =
  process.env.TZ || Intl.DateTimeFormat().resolvedOptions().timeZone;

const rawAgentBackend = (
  process.env.DEUS_AGENT_BACKEND ||
  envConfig.DEUS_AGENT_BACKEND ||
  'claude'
).toLowerCase();
// Use `parseAgentBackend` from agent-runtimes/types.ts (host SoT) as the
// canonical accepted-value gate. Avoids the circular `ipc.ts` import path
// AND eliminates the prior silent-coercion ternary for new backend IDs.
export const DEFAULT_AGENT_RUNTIME: AgentRuntimeId =
  parseAgentBackend(rawAgentBackend) ?? 'claude';

export const DEUS_OPENAI_MODEL =
  process.env.DEUS_OPENAI_MODEL || envConfig.DEUS_OPENAI_MODEL || '';

// llama.cpp local-server endpoint configuration. Host-side values only —
// the container receives translated values via OPENAI_BASE_URL-style env
// var injection in container-runner.ts. See docs/MULTI_BACKEND.md.
export const LLAMA_CPP_BASE_URL =
  process.env.LLAMA_CPP_BASE_URL || envConfig.LLAMA_CPP_BASE_URL || '';
export const LLAMA_CPP_PORT =
  process.env.LLAMA_CPP_PORT || envConfig.LLAMA_CPP_PORT || '8080';
export const LLAMA_CPP_MODEL =
  process.env.LLAMA_CPP_MODEL || envConfig.LLAMA_CPP_MODEL || '';

// Per-surface model overrides. Each falls back to LLAMA_CPP_MODEL (catch-all),
// then to empty string (router-mode auto-pick from --models-dir).
// Per Phase 3 (PR-after-#461): supports `llama-server --models-dir ... --models-max 4`
// where each surface POSTs with its own "model" field and the server hot-loads.
export const LLAMA_CPP_AGENT_MODEL =
  process.env.LLAMA_CPP_AGENT_MODEL ||
  envConfig.LLAMA_CPP_AGENT_MODEL ||
  LLAMA_CPP_MODEL;
export const LLAMA_CPP_GEN_MODEL =
  process.env.LLAMA_CPP_GEN_MODEL ||
  envConfig.LLAMA_CPP_GEN_MODEL ||
  LLAMA_CPP_MODEL;
export const LLAMA_CPP_JUDGE_MODEL =
  process.env.LLAMA_CPP_JUDGE_MODEL ||
  envConfig.LLAMA_CPP_JUDGE_MODEL ||
  LLAMA_CPP_MODEL;
export const LLAMA_CPP_EMBED_MODEL =
  process.env.LLAMA_CPP_EMBED_MODEL ||
  envConfig.LLAMA_CPP_EMBED_MODEL ||
  LLAMA_CPP_MODEL;

export const DEUS_CONTEXT_FILE_MAX_CHARS =
  process.env.DEUS_CONTEXT_FILE_MAX_CHARS ||
  envConfig.DEUS_CONTEXT_FILE_MAX_CHARS ||
  '';

// ── Context compaction thresholds ────────────────────────────────────────────
// At WARN_PCT the user sees an advisory; at AUTO_COMPACT_PCT auto-compaction fires.
// Matches TUI defaults (context_alert_shown / auto_compact_threshold). Override for testing:
// DEUS_CONTEXT_WARN_PCT=5 DEUS_CONTEXT_AUTO_COMPACT_PCT=10
export const CONTEXT_WARN_PCT = parseInt(
  process.env.DEUS_CONTEXT_WARN_PCT || '70',
  10,
);
export const CONTEXT_AUTO_COMPACT_PCT = parseInt(
  process.env.DEUS_CONTEXT_AUTO_COMPACT_PCT || '75',
  10,
);

// Opt-in context notifications (70% warning, auto-compact notice).
// Off by default for a smooth, silent experience. DEUS_CONTEXT_NOTIFY=1 enables.
export const CONTEXT_NOTIFY =
  process.env.DEUS_CONTEXT_NOTIFY === '1' ||
  process.env.DEUS_CONTEXT_NOTIFY === 'true';

// Credential proxy authentication.
// Per-group tokens generated in group-tokens.ts (process-lifetime).
// Set DEUS_PROXY_AUTH=0 to disable enforcement (ignored in production).
export const DEUS_PROXY_AUTH_ENABLED =
  process.env.NODE_ENV === 'production' || process.env.DEUS_PROXY_AUTH !== '0';

// ── Injection scanner guardrail ──────────────────────────────────────────────
// Disabled by default. Enable via DEUS_INJECTION_SCANNER=1.
// Ships with logOnly=true so operators gain confidence before blocking.
export const INJECTION_SCANNER_CONFIG: InjectionScannerConfig = {
  enabled: process.env.DEUS_INJECTION_SCANNER === '1',
  threshold: parseFloat(process.env.DEUS_INJECTION_SCANNER_THRESHOLD || '0.7'),
  logOnly: process.env.DEUS_INJECTION_SCANNER_LOG_ONLY !== '0', // true unless explicitly set to 0
};

// ── Webhook retry configuration ───────────────────────────────────────────────
// Max number of retry attempts for webhook dispatch (excludes first attempt).
export const WEBHOOK_MAX_RETRIES = parseInt(
  process.env.WEBHOOK_MAX_RETRIES || envConfig.WEBHOOK_MAX_RETRIES || '3',
  10,
);
// Base delay in ms for exponential backoff: min(base * 2^attempt + jitter, 30_000).
export const WEBHOOK_BASE_DELAY_MS = parseInt(
  process.env.WEBHOOK_BASE_DELAY_MS ||
    envConfig.WEBHOOK_BASE_DELAY_MS ||
    '1000',
  10,
);
