import { existsSync, readFileSync } from 'fs';
import { join, resolve } from 'path';
import { homedir } from 'os';
import { fileURLToPath } from 'url';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const REPO_ROOT = resolve(__dirname, '..', '..', '..');

export interface ChannelInfo {
  name: string;
  configured: boolean;
}

function envHas(key: string): boolean {
  if (process.env[key]) return true;
  const envPath = join(REPO_ROOT, '.env');
  if (!existsSync(envPath)) return false;
  try {
    const content = readFileSync(envPath, 'utf-8');
    return content
      .split('\n')
      .some(
        (line) => line.startsWith(`${key}=`) && line.length > key.length + 1,
      );
  } catch {
    return false;
  }
}

export function getChannelStatuses(): ChannelInfo[] {
  return [
    {
      name: 'WhatsApp',
      configured: existsSync(join(REPO_ROOT, 'store', 'auth', 'creds.json')),
    },
    { name: 'Telegram', configured: envHas('TELEGRAM_BOT_TOKEN') },
    { name: 'Discord', configured: envHas('DISCORD_BOT_TOKEN') },
    { name: 'Slack', configured: envHas('SLACK_BOT_TOKEN') },
    {
      name: 'Gmail',
      configured: existsSync(
        join(homedir(), '.config', 'deus', 'gmail-tokens.json'),
      ),
    },
    { name: 'X (Twitter)', configured: envHas('X_API_KEY') },
  ];
}
