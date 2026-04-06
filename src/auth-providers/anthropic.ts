/**
 * Anthropic auth provider for the credential proxy.
 *
 * Extracts all Anthropic-specific auth logic (API key + OAuth modes)
 * from credential-proxy.ts into a self-contained AuthProvider.
 *
 * Two auth modes:
 *   API key:  Injects x-api-key on every request.
 *   OAuth:    Replaces placeholder Bearer token with the real one
 *             only when the container sends an Authorization header.
 *
 * OAuth token resolution order (per-request, with 5-min cache):
 *   1. CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_AUTH_TOKEN from env file
 *   2. ~/.claude/.credentials.json (auto-refreshed by Claude Code CLI)
 */
import { readFileSync } from 'fs';
import os from 'os';
import path from 'path';

import { readEnvFile } from '../env.js';
import type { AuthProvider } from './types.js';
import type { AuthMode } from '../credential-proxy.js';

// ---------------------------------------------------------------------------
// Dynamic OAuth token — read from ~/.claude/.credentials.json with a 5-min TTL.
// The env-file value always takes priority when set.
// ---------------------------------------------------------------------------
const CREDENTIALS_PATH = path.join(
  process.env.HOME || os.homedir(),
  '.claude',
  '.credentials.json',
);
const CACHE_TTL_MS = 5 * 60 * 1000;
const EARLY_EXPIRE_WINDOW_MS = 5 * 60 * 1000;

interface CredentialsCache {
  token: string;
  fetchedAt: number;
  tokenExpiresAt: number;
}

let credentialsCache: CredentialsCache | null = null;

/** @internal exposed for testing only */
export function _resetCredentialsCacheForTest(): void {
  credentialsCache = null;
}

function readCredentialsFile():
  | { token: string; expiresAt: number }
  | undefined {
  try {
    const raw = readFileSync(CREDENTIALS_PATH, 'utf-8');
    const parsed = JSON.parse(raw) as {
      claudeAiOauth?: { accessToken?: string; expiresAt?: number };
    };
    const oauth = parsed?.claudeAiOauth;
    if (!oauth?.accessToken) return undefined;
    return { token: oauth.accessToken, expiresAt: oauth.expiresAt ?? Infinity };
  } catch {
    return undefined;
  }
}

function getDynamicOAuthToken(): string | undefined {
  const now = Date.now();
  if (credentialsCache) {
    const cacheAge = now - credentialsCache.fetchedAt;
    const aboutToExpire =
      credentialsCache.tokenExpiresAt !== Infinity &&
      credentialsCache.tokenExpiresAt < now + EARLY_EXPIRE_WINDOW_MS;
    if (cacheAge < CACHE_TTL_MS && !aboutToExpire)
      return credentialsCache.token;
  }
  const creds = readCredentialsFile();
  if (!creds) return undefined;
  credentialsCache = {
    token: creds.token,
    fetchedAt: now,
    tokenExpiresAt: creds.expiresAt,
  };
  return creds.token;
}

/**
 * Anthropic auth provider.
 *
 * Supports both API key and OAuth modes. The mode is determined at
 * construction time from the env file and is immutable for the lifetime
 * of the proxy server instance.
 */
export class AnthropicAuthProvider implements AuthProvider {
  readonly name = 'anthropic';
  readonly priority = 10;
  readonly envKeys = [
    'ANTHROPIC_API_KEY',
    'CLAUDE_CODE_OAUTH_TOKEN',
    'ANTHROPIC_AUTH_TOKEN',
    'ANTHROPIC_BASE_URL',
  ];

  private readonly secrets: Record<string, string>;
  private readonly authMode: AuthMode;
  private readonly envOauthToken: string | undefined;

  constructor() {
    this.secrets = readEnvFile(this.envKeys);
    this.authMode = this.secrets.ANTHROPIC_API_KEY ? 'api-key' : 'oauth';
    this.envOauthToken =
      this.secrets.CLAUDE_CODE_OAUTH_TOKEN || this.secrets.ANTHROPIC_AUTH_TOKEN;
  }

  /** Get the auth mode for external consumers (e.g. container-runner). */
  getAuthMode(): AuthMode {
    return this.authMode;
  }

  isAvailable(): boolean {
    if (this.secrets.ANTHROPIC_API_KEY) return true;
    if (this.envOauthToken) return true;
    // Check dynamic credentials file
    return getDynamicOAuthToken() !== undefined;
  }

  getUpstreamUrl(): string {
    return this.secrets.ANTHROPIC_BASE_URL || 'https://api.anthropic.com';
  }

  injectAuth(headers: Record<string, string | string[] | undefined>): void {
    if (this.authMode === 'api-key') {
      // API key mode: inject x-api-key on every request
      delete headers['x-api-key'];
      headers['x-api-key'] = this.secrets.ANTHROPIC_API_KEY;
    } else {
      // OAuth mode: replace placeholder Bearer token with the real one
      // only when the container actually sends an Authorization header
      // (exchange request + auth probes). Post-exchange requests use
      // x-api-key only, so they pass through without token injection.
      if (headers['authorization']) {
        delete headers['authorization'];
        const token = this.envOauthToken || getDynamicOAuthToken();
        if (token) {
          headers['authorization'] = `Bearer ${token}`;
        }
      }
    }
  }
}
