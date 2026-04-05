/**
 * Slack channel factory — spawns @deus-ai/slack-mcp as MCP server.
 * Registers with the channel registry so the host can use it.
 */

import path from 'path';

import { ASSISTANT_NAME, PROJECT_ROOT } from '../config.js';
import { readEnvFile } from '../env.js';
import { McpChannelAdapter } from './mcp-adapter.js';
import { registerChannel } from './registry.js';

registerChannel('slack', (opts) => {
  const envVars = readEnvFile(['SLACK_BOT_TOKEN', 'SLACK_APP_TOKEN']);
  const botToken = process.env.SLACK_BOT_TOKEN || envVars.SLACK_BOT_TOKEN || '';
  const appToken = process.env.SLACK_APP_TOKEN || envVars.SLACK_APP_TOKEN || '';
  if (!botToken || !appToken) return null;

  let serverPath: string;
  try {
    serverPath = import.meta
      .resolve('@deus-ai/slack-mcp')
      .replace('file://', '');
  } catch {
    serverPath = path.join(
      PROJECT_ROOT,
      'packages',
      'mcp-slack',
      'dist',
      'index.js',
    );
  }

  return new McpChannelAdapter({
    name: 'slack',
    command: 'node',
    args: [serverPath],
    env: {
      SLACK_BOT_TOKEN: botToken,
      SLACK_APP_TOKEN: appToken,
      ASSISTANT_NAME: ASSISTANT_NAME,
    },
    onMessage: opts.onMessage,
    onChatMetadata: opts.onChatMetadata,
    ownsJid: (jid) => jid.startsWith('slack:'),
  });
});
