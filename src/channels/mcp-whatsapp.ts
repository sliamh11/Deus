/**
 * WhatsApp channel factory — spawns deus-mcp-whatsapp as MCP server.
 * Registers with the channel registry so the host can use it.
 */

import fs from 'fs';
import path from 'path';

import {
  ASSISTANT_HAS_OWN_NUMBER,
  ASSISTANT_NAME,
  PROJECT_ROOT,
  STORE_DIR,
} from '../config.js';
import { McpChannelAdapter } from './mcp-adapter.js';
import { registerChannel } from './registry.js';

registerChannel('whatsapp', (opts) => {
  const authDir = path.join(STORE_DIR, 'auth');
  if (!fs.existsSync(path.join(authDir, 'creds.json'))) return null;

  // Resolve the MCP server entry point
  let serverPath: string;
  try {
    // When deus-mcp-whatsapp is installed as a dependency
    serverPath = import.meta
      .resolve('deus-mcp-whatsapp')
      .replace('file://', '');
  } catch {
    // Fallback: local packages directory (monorepo dev)
    serverPath = path.join(
      PROJECT_ROOT,
      'packages',
      'mcp-whatsapp',
      'dist',
      'index.js',
    );
  }

  return new McpChannelAdapter({
    name: 'whatsapp',
    command: 'node',
    args: [serverPath],
    env: {
      WHATSAPP_AUTH_DIR: path.resolve(authDir),
      ASSISTANT_NAME: ASSISTANT_NAME,
      ASSISTANT_HAS_OWN_NUMBER: ASSISTANT_HAS_OWN_NUMBER ? 'true' : 'false',
    },
    onMessage: opts.onMessage,
    onChatMetadata: opts.onChatMetadata,
    ownsJid: (jid) => jid.endsWith('@g.us') || jid.endsWith('@s.whatsapp.net'),
  });
});
