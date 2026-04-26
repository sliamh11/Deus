import { readFileSync, renameSync, writeFileSync } from 'fs';
import path from 'path';

import { readEnvFile } from '../env.js';
import { homeDir } from '../platform.js';
import type { AuthProvider } from './types.js';

// ---------------------------------------------------------------------------
// Codex OAuth — dual-mode: API key (priority) + Codex OAuth fallback
// ---------------------------------------------------------------------------

export const CODEX_AUTH_PATH = path.join(homeDir, '.codex', 'auth.json');
const CACHE_TTL_MS = 5 * 60 * 1000;
const EARLY_EXPIRE_WINDOW_MS = 30 * 60 * 1000;

export interface CodexAuthFile {
  auth_mode?: string;
  OPENAI_API_KEY?: string | null;
  tokens?: {
    id_token?: string;
    access_token?: string;
    refresh_token?: string;
    account_id?: string;
  };
  last_refresh?: string;
}

export interface CodexOAuthCredentials {
  accessToken: string;
  refreshToken?: string;
  clientId?: string;
  expiresAt: number;
}

interface CodexCache {
  token: string;
  fetchedAt: number;
  tokenExpiresAt: number;
}

let codexCache: CodexCache | null = null;
let codexRefreshInFlight = false;

/** @internal exposed for testing only */
export function _resetCodexCacheForTest(): void {
  codexCache = null;
  codexRefreshInFlight = false;
}

/** Decode the payload segment of a JWT (no verification). */
function decodeJwtPayload(
  token: string,
): { exp?: number; client_id?: string; [key: string]: unknown } | undefined {
  try {
    const parts = token.split('.');
    if (parts.length < 2) return undefined;
    const b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    const json = Buffer.from(b64, 'base64').toString('utf-8');
    return JSON.parse(json);
  } catch {
    return undefined;
  }
}

export function readCodexAuthFile(): CodexAuthFile | undefined {
  try {
    const raw = readFileSync(CODEX_AUTH_PATH, 'utf-8');
    return JSON.parse(raw) as CodexAuthFile;
  } catch {
    return undefined;
  }
}

/**
 * Atomic write of the Codex auth file. Read-modify-write: only updates
 * token fields and last_refresh, preserving auth_mode, account_id, etc.
 */
export function writeCodexAuthFile(
  authFile: CodexAuthFile,
  updatedTokens: { access_token: string; refresh_token?: string },
): void {
  try {
    const updated: CodexAuthFile = {
      ...authFile,
      tokens: {
        ...authFile.tokens,
        access_token: updatedTokens.access_token,
        ...(updatedTokens.refresh_token
          ? { refresh_token: updatedTokens.refresh_token }
          : {}),
      },
      last_refresh: new Date().toISOString(),
    };
    const tmpPath = `${CODEX_AUTH_PATH}.tmp-${process.pid}-${Date.now()}`;
    writeFileSync(tmpPath, JSON.stringify(updated, null, 2), { mode: 0o600 });
    renameSync(tmpPath, CODEX_AUTH_PATH);
  } catch {
    // Best-effort — proxy still works with in-memory cache
  }
}

/** Refresh the Codex OAuth token using Auth0's refresh_token grant. */
export async function refreshCodexToken(
  refreshToken: string,
  clientId: string,
): Promise<
  | { access_token: string; refresh_token?: string; expires_in?: number }
  | undefined
> {
  try {
    const body = new URLSearchParams({
      grant_type: 'refresh_token',
      refresh_token: refreshToken,
      client_id: clientId,
    });
    const res = await fetch('https://auth.openai.com/oauth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: body.toString(),
    });
    if (!res.ok) return undefined;
    const json = (await res.json()) as {
      access_token?: string;
      refresh_token?: string;
      expires_in?: number;
    };
    if (!json.access_token) return undefined;
    return {
      access_token: json.access_token,
      refresh_token: json.refresh_token,
      expires_in: json.expires_in,
    };
  } catch {
    return undefined;
  }
}

function triggerCodexRefresh(
  authFile: CodexAuthFile,
  refreshToken: string,
  clientId: string,
): void {
  if (codexRefreshInFlight) return;
  codexRefreshInFlight = true;
  refreshCodexToken(refreshToken, clientId)
    .then((result) => {
      if (result) {
        writeCodexAuthFile(authFile, {
          access_token: result.access_token,
          refresh_token: result.refresh_token ?? refreshToken,
        });
        const payload = decodeJwtPayload(result.access_token);
        codexCache = {
          token: result.access_token,
          fetchedAt: Date.now(),
          tokenExpiresAt: payload?.exp ? payload.exp * 1000 : Infinity,
        };
      }
    })
    .finally(() => {
      codexRefreshInFlight = false;
    });
}

function getCodexOAuthToken(): string | undefined {
  const now = Date.now();
  if (codexCache) {
    const cacheAge = now - codexCache.fetchedAt;
    const aboutToExpire =
      codexCache.tokenExpiresAt !== Infinity &&
      codexCache.tokenExpiresAt < now + EARLY_EXPIRE_WINDOW_MS;
    if (cacheAge < CACHE_TTL_MS && !aboutToExpire) return codexCache.token;
  }

  const authFile = readCodexAuthFile();
  if (!authFile?.tokens?.access_token) return undefined;

  const token = authFile.tokens.access_token;
  const payload = decodeJwtPayload(token);
  const expiresAt = payload?.exp ? payload.exp * 1000 : Infinity;

  if (
    expiresAt !== Infinity &&
    expiresAt < now + EARLY_EXPIRE_WINDOW_MS &&
    authFile.tokens.refresh_token &&
    payload?.client_id
  ) {
    triggerCodexRefresh(
      authFile,
      authFile.tokens.refresh_token,
      payload.client_id,
    );
  }

  codexCache = { token, fetchedAt: now, tokenExpiresAt: expiresAt };
  return token;
}

export class OpenAIAuthProvider implements AuthProvider {
  readonly name = 'openai';
  readonly priority = 20;
  readonly envKeys = ['OPENAI_API_KEY', 'OPENAI_BASE_URL'];

  private readonly secrets: Record<string, string>;
  private readonly authMode: 'api-key' | 'oauth';

  constructor() {
    this.secrets = readEnvFile(this.envKeys);
    this.authMode = this.secrets.OPENAI_API_KEY ? 'api-key' : 'oauth';
  }

  getAuthMode(): 'api-key' | 'oauth' {
    return this.authMode;
  }

  isAvailable(): boolean {
    if (this.secrets.OPENAI_API_KEY) return true;
    return getCodexOAuthToken() !== undefined;
  }

  getUpstreamUrl(): string {
    return this.secrets.OPENAI_BASE_URL || 'https://api.openai.com';
  }

  injectAuth(headers: Record<string, string | string[] | undefined>): void {
    delete headers['x-api-key'];
    delete headers.authorization;
    if (this.authMode === 'api-key') {
      if (this.secrets.OPENAI_API_KEY) {
        headers.authorization = `Bearer ${this.secrets.OPENAI_API_KEY}`;
      }
    } else {
      const token = getCodexOAuthToken();
      if (token) {
        headers.authorization = `Bearer ${token}`;
      }
    }
  }
}
