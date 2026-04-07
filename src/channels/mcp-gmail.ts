/**
 * Gmail channel factory — spawns @deus-ai/gmail-mcp as MCP server.
 * Registers with the channel registry so the host can use it.
 */

import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';

import { PROJECT_ROOT } from '../config.js';
import { McpChannelAdapter } from './mcp-adapter.js';
import { registerChannel } from './registry.js';

registerChannel('gmail', (opts) => {
  const credDir =
    process.env.GMAIL_CREDENTIALS_DIR || path.join(os.homedir(), '.gmail-mcp');

  const hasCredentials =
    fs.existsSync(path.join(credDir, 'gcp-oauth.keys.json')) &&
    fs.existsSync(path.join(credDir, 'credentials.json'));

  if (!hasCredentials) return null;

  let serverPath: string;
  try {
    serverPath = fileURLToPath(import.meta.resolve('@deus-ai/gmail-mcp'));
  } catch {
    serverPath = path.join(
      PROJECT_ROOT,
      'packages',
      'mcp-gmail',
      'dist',
      'index.js',
    );
  }

  return new McpChannelAdapter({
    name: 'gmail',
    command: 'node',
    args: [serverPath],
    env: {
      GMAIL_CREDENTIALS_DIR: credDir,
    },
    onMessage: opts.onMessage,
    onChatMetadata: opts.onChatMetadata,
    ownsJid: (jid) => jid.startsWith('gmail:'),
  });
});
