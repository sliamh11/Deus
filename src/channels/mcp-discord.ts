/**
 * Discord channel factory — spawns @deus-ai/discord-mcp as MCP server.
 * Registers with the channel registry so the host can use it.
 */

import path from 'path';
import { fileURLToPath } from 'url';

import { ASSISTANT_NAME, PROJECT_ROOT } from '../config.js';
import { readEnvFile } from '../env.js';
import { McpChannelAdapter } from './mcp-adapter.js';
import { registerChannel } from './registry.js';

registerChannel('discord', (opts) => {
  const envVars = readEnvFile(['DISCORD_BOT_TOKEN']);
  const token =
    process.env.DISCORD_BOT_TOKEN || envVars.DISCORD_BOT_TOKEN || '';
  if (!token) return null;

  let serverPath: string;
  try {
    serverPath = fileURLToPath(import.meta.resolve('@deus-ai/discord-mcp'));
  } catch {
    serverPath = path.join(
      PROJECT_ROOT,
      'packages',
      'mcp-discord',
      'dist',
      'index.js',
    );
  }

  return new McpChannelAdapter({
    name: 'discord',
    command: 'node',
    args: [serverPath],
    env: {
      DISCORD_BOT_TOKEN: token,
      ASSISTANT_NAME: ASSISTANT_NAME,
    },
    onMessage: opts.onMessage,
    onChatMetadata: opts.onChatMetadata,
    ownsJid: (jid) => jid.startsWith('dc:'),
  });
});
