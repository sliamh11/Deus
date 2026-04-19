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
 *   2. ~/.claude/.credentials.json → macOS Keychain fallback
 *   3. Auto-refresh via refresh_token when token is about to expire
 */
import { execFileSync } from 'child_process';
import { readFileSync, renameSync, writeFileSync } from 'fs';
import path from 'path';

import { readEnvFile } from '../env.js';
import { homeDir, IS_LINUX, IS_MACOS, IS_WINDOWS } from '../platform.js';
import type { AuthProvider } from './types.js';
import type { AuthMode } from '../credential-proxy.js';

// ---------------------------------------------------------------------------
// Dynamic OAuth token — read from credentials file, keychain fallback,
// auto-refresh when expiring. The env-file value always takes priority.
// ---------------------------------------------------------------------------
export const CREDENTIALS_PATH = path.join(
  homeDir,
  '.claude',
  '.credentials.json',
);
const CACHE_TTL_MS = 5 * 60 * 1000;
export const EARLY_EXPIRE_WINDOW_MS = 30 * 60 * 1000; // 30 min — trigger refresh early
const REFRESH_CLIENT_ID = '9d1c250a-e61b-44d9-88ed-5944d1962f5e';
const REFRESH_SCOPES =
  'user:inference user:profile user:sessions:claude_code user:mcp_servers user:file_upload';

export interface OAuthCredentials {
  accessToken: string;
  refreshToken?: string;
  expiresAt: number;
}

interface CredentialsCache {
  token: string;
  fetchedAt: number;
  tokenExpiresAt: number;
}

let credentialsCache: CredentialsCache | null = null;
let refreshInFlight = false;

/** @internal exposed for testing only */
export function _resetCredentialsCacheForTest(): void {
  credentialsCache = null;
  refreshInFlight = false;
}

export function readCredentialsFile(): OAuthCredentials | undefined {
  try {
    const raw = readFileSync(CREDENTIALS_PATH, 'utf-8');
    const parsed = JSON.parse(raw) as {
      claudeAiOauth?: {
        accessToken?: string;
        refreshToken?: string;
        expiresAt?: number;
      };
    };
    const oauth = parsed?.claudeAiOauth;
    if (!oauth?.accessToken) return undefined;
    return {
      accessToken: oauth.accessToken,
      refreshToken: oauth.refreshToken,
      expiresAt: oauth.expiresAt ?? Infinity,
    };
  } catch {
    return undefined;
  }
}

/**
 * Read credentials from the OS credential store.
 * Claude Code stores OAuth tokens in the platform-native keychain:
 *   macOS:   Keychain (security CLI)
 *   Linux:   libsecret / GNOME Keyring (secret-tool CLI)
 *   Windows: Credential Manager (PowerShell)
 */
function readKeychainCredentials(): OAuthCredentials | undefined {
  const raw = readRawFromCredentialStore();
  if (!raw) return undefined;
  try {
    const parsed = JSON.parse(raw) as {
      claudeAiOauth?: {
        accessToken?: string;
        refreshToken?: string;
        expiresAt?: number;
      };
    };
    const oauth = parsed?.claudeAiOauth;
    if (!oauth?.accessToken) return undefined;
    return {
      accessToken: oauth.accessToken,
      refreshToken: oauth.refreshToken,
      expiresAt: oauth.expiresAt ?? Infinity,
    };
  } catch {
    return undefined;
  }
}

function readRawFromCredentialStore(): string | undefined {
  const execOpts = {
    encoding: 'utf-8' as const,
    timeout: 5000,
    stdio: ['pipe', 'pipe', 'pipe'] as ['pipe', 'pipe', 'pipe'],
  };
  try {
    if (IS_MACOS) {
      return execFileSync(
        'security',
        [
          'find-generic-password',
          '-s',
          'Claude Code-credentials',
          '-a',
          process.env.USER ?? '',
          '-w',
        ],
        execOpts,
      ).trim();
    }
    if (IS_LINUX) {
      // libsecret (GNOME Keyring / KDE Wallet via freedesktop Secret Service)
      return execFileSync(
        'secret-tool',
        [
          'lookup',
          'service',
          'Claude Code-credentials',
          'account',
          process.env.USER ?? '',
        ],
        execOpts,
      ).trim();
    }
    if (IS_WINDOWS) {
      // Windows Credential Manager via PowerShell
      const ps = [
        '-NoProfile',
        '-NonInteractive',
        '-Command',
        `$c = Get-StoredCredential -Target 'Claude Code-credentials' -ErrorAction SilentlyContinue; ` +
          `if ($c) { [System.Net.NetworkCredential]::new('', $c.Password).Password } else { '' }`,
      ];
      const result = execFileSync('powershell.exe', ps, execOpts).trim();
      return result || undefined;
    }
  } catch {
    // Credential store unavailable or entry not found
  }
  return undefined;
}

/**
 * Write credentials to disk so the file stays in sync with keychain.
 *
 * Atomic: writes to a sibling tmp file and renames into place. Never leaves
 * a half-written credentials.json that would cause a login loop if the
 * process dies mid-write.
 */
export function writeCredentialsFile(creds: OAuthCredentials): void {
  try {
    const data = {
      claudeAiOauth: {
        accessToken: creds.accessToken,
        refreshToken: creds.refreshToken,
        expiresAt: creds.expiresAt,
      },
    };
    const tmpPath = `${CREDENTIALS_PATH}.tmp-${process.pid}-${Date.now()}`;
    writeFileSync(tmpPath, JSON.stringify(data), { mode: 0o600 });
    renameSync(tmpPath, CREDENTIALS_PATH);
  } catch {
    // Best-effort — proxy still works with in-memory cache
  }
}

/** Refresh the OAuth token using the refresh_token grant. */
export async function refreshOAuthToken(
  refreshToken: string,
): Promise<OAuthCredentials | undefined> {
  try {
    const res = await fetch('https://platform.claude.com/v1/oauth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        grant_type: 'refresh_token',
        refresh_token: refreshToken,
        client_id: REFRESH_CLIENT_ID,
        scope: REFRESH_SCOPES,
      }),
    });
    if (!res.ok) return undefined;
    const body = (await res.json()) as {
      access_token?: string;
      refresh_token?: string;
      expires_in?: number;
    };
    if (!body.access_token) return undefined;
    return {
      accessToken: body.access_token,
      refreshToken: body.refresh_token ?? refreshToken,
      expiresAt: Date.now() + (body.expires_in ?? 28800) * 1000,
    };
  } catch {
    return undefined;
  }
}

/** Attempt token refresh and persist the result. Fire-and-forget. */
function triggerRefresh(refreshToken: string): void {
  if (refreshInFlight) return;
  refreshInFlight = true;
  refreshOAuthToken(refreshToken)
    .then((newCreds) => {
      if (newCreds) {
        writeCredentialsFile(newCreds);
        credentialsCache = {
          token: newCreds.accessToken,
          fetchedAt: Date.now(),
          tokenExpiresAt: newCreds.expiresAt,
        };
      }
    })
    .finally(() => {
      refreshInFlight = false;
    });
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

  // Try file first, then keychain fallback
  let creds = readCredentialsFile();
  if (!creds) {
    creds = readKeychainCredentials();
    if (creds) writeCredentialsFile(creds); // sync to disk
  }
  if (!creds) return undefined;

  // Trigger background refresh if token is expiring within the window
  const aboutToExpire =
    creds.expiresAt !== Infinity &&
    creds.expiresAt < now + EARLY_EXPIRE_WINDOW_MS;
  if (aboutToExpire && creds.refreshToken) {
    triggerRefresh(creds.refreshToken);
  }

  credentialsCache = {
    token: creds.accessToken,
    fetchedAt: now,
    tokenExpiresAt: creds.expiresAt,
  };
  return creds.accessToken;
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
