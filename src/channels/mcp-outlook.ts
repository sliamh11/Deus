/**
 * Outlook channel factory — spawns @deus-ai/outlook-mcp as MCP server.
 * Registers with the channel registry so the host can use it.
 *
 * Mirrors the Gmail channel: credentials live in a directory (default
 * ~/.outlook-mcp/) and the channel auto-enables when they are present.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';

import { PROJECT_ROOT } from '../config.js';
import { McpChannelAdapter } from './mcp-adapter.js';
import { registerChannel } from './registry.js';

registerChannel('outlook', (opts) => {
  const credDir =
    process.env.OUTLOOK_CREDENTIALS_DIR ||
    path.join(os.homedir(), '.outlook-mcp');

  const hasCredentials =
    fs.existsSync(path.join(credDir, 'app-credentials.json')) &&
    fs.existsSync(path.join(credDir, 'token.json'));

  if (!hasCredentials) return null;

  let serverPath: string;
  try {
    serverPath = fileURLToPath(import.meta.resolve('@deus-ai/outlook-mcp'));
  } catch {
    serverPath = path.join(
      PROJECT_ROOT,
      'packages',
      'mcp-outlook',
      'dist',
      'index.js',
    );
  }

  return new McpChannelAdapter({
    name: 'outlook',
    command: 'node',
    args: [serverPath],
    env: {
      OUTLOOK_CREDENTIALS_DIR: credDir,
    },
    onMessage: opts.onMessage,
    onChatMetadata: opts.onChatMetadata,
    ownsJid: (jid) => jid.startsWith('outlook:'),
  });
});
