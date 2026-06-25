/**
 * Microsoft Teams channel factory — spawns @deus-ai/teams-mcp as MCP server.
 * Registers with the channel registry so the host can use it.
 *
 * Mirrors the Slack channel: credentials are env-var tokens (read from .env via
 * readEnvFile, since the host does not auto-load .env into process.env) and the
 * channel auto-enables when they are present.
 */

import path from 'path';
import { fileURLToPath } from 'url';

import { PROJECT_ROOT } from '../config.js';
import { readEnvFile } from '../env.js';
import { McpChannelAdapter } from './mcp-adapter.js';
import { registerChannel } from './registry.js';

registerChannel('teams', (opts) => {
  const envVars = readEnvFile([
    'TEAMS_APP_ID',
    'TEAMS_APP_PASSWORD',
    'TEAMS_APP_TENANT_ID',
    'TEAMS_PORT',
    'TEAMS_CREDENTIALS_DIR',
  ]);
  const appId = process.env.TEAMS_APP_ID || envVars.TEAMS_APP_ID || '';
  const appPassword =
    process.env.TEAMS_APP_PASSWORD || envVars.TEAMS_APP_PASSWORD || '';
  const tenantId =
    process.env.TEAMS_APP_TENANT_ID || envVars.TEAMS_APP_TENANT_ID || '';
  // Optional overrides — forward to the subprocess so a .env-configured value
  // takes effect (the host does not auto-load .env into process.env).
  const port = process.env.TEAMS_PORT || envVars.TEAMS_PORT || '';
  const credentialsDir =
    process.env.TEAMS_CREDENTIALS_DIR || envVars.TEAMS_CREDENTIALS_DIR || '';
  if (!appId || !appPassword) return null;

  let serverPath: string;
  try {
    serverPath = fileURLToPath(import.meta.resolve('@deus-ai/teams-mcp'));
  } catch {
    serverPath = path.join(
      PROJECT_ROOT,
      'packages',
      'mcp-teams',
      'dist',
      'index.js',
    );
  }

  return new McpChannelAdapter({
    name: 'teams',
    command: 'node',
    args: [serverPath],
    env: {
      TEAMS_APP_ID: appId,
      TEAMS_APP_PASSWORD: appPassword,
      ...(tenantId ? { TEAMS_APP_TENANT_ID: tenantId } : {}),
      ...(port ? { TEAMS_PORT: port } : {}),
      ...(credentialsDir ? { TEAMS_CREDENTIALS_DIR: credentialsDir } : {}),
    },
    onMessage: opts.onMessage,
    onChatMetadata: opts.onChatMetadata,
    ownsJid: (jid) => jid.startsWith('teams:'),
  });
});
