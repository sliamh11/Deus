import crypto from 'crypto';
import path from 'path';

import type { AgentBackendName } from './agent-backends/types.js';
import { readEnvFile } from './env.js';
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
export const DEFAULT_AGENT_BACKEND: AgentBackendName =
  rawAgentBackend === 'openai' ? 'openai' : 'claude';

export const DEUS_OPENAI_MODEL =
  process.env.DEUS_OPENAI_MODEL || envConfig.DEUS_OPENAI_MODEL || '';

export const DEUS_CONTEXT_FILE_MAX_CHARS =
  process.env.DEUS_CONTEXT_FILE_MAX_CHARS ||
  envConfig.DEUS_CONTEXT_FILE_MAX_CHARS ||
  '';

// Shared secret for credential proxy authentication.
// Generated once per process lifetime; injected into containers via env.
// Set DEUS_PROXY_AUTH=0 to disable enforcement (rollout kill-switch).
export const DEUS_PROXY_TOKEN = crypto.randomBytes(32).toString('hex');
export const DEUS_PROXY_AUTH_ENABLED = process.env.DEUS_PROXY_AUTH !== '0';
