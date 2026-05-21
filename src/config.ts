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
