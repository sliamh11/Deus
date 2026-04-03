/**
 * Telegram channel factory — spawns deus-mcp-telegram as MCP server.
 * Registers with the channel registry so the host can use it.
 */

import path from 'path';

import { ASSISTANT_NAME } from '../config.js';
import { readEnvFile } from '../env.js';
import { McpChannelAdapter } from './mcp-adapter.js';
import { registerChannel } from './registry.js';

registerChannel('telegram', (opts) => {
  const envVars = readEnvFile(['TELEGRAM_BOT_TOKEN']);
  const token =
    process.env.TELEGRAM_BOT_TOKEN || envVars.TELEGRAM_BOT_TOKEN || '';
  if (!token) return null;

  let serverPath: string;
  try {
    serverPath = import.meta
      .resolve('deus-mcp-telegram')
      .replace('file://', '');
  } catch {
    serverPath = path.resolve('packages', 'mcp-telegram', 'dist', 'index.js');
  }

  return new McpChannelAdapter({
    name: 'telegram',
    command: 'node',
    args: [serverPath],
    env: {
      TELEGRAM_BOT_TOKEN: token,
      ASSISTANT_NAME: ASSISTANT_NAME,
    },
    onMessage: opts.onMessage,
    onChatMetadata: opts.onChatMetadata,
    ownsJid: (jid) => jid.startsWith('tg:'),
  });
});
