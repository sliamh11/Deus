import { existsSync, readFileSync } from 'fs';
import { join } from 'path';
import { homedir } from 'os';

export interface DeusConfig {
  vault_path?: string;
  agent_backend?: string;
  agent_backend_model?: string;
  chrome_default?: string;
  name?: string;
  catch_me_up?: boolean;
  bypass_permissions?: boolean;
  persona?: string;
  max_parallel_agents?: number;
  [key: string]: unknown;
}

export function loadDeusConfig(): DeusConfig {
  const configPath = join(homedir(), '.config', 'deus', 'config.json');
  if (!existsSync(configPath)) return {};
  try {
    const data = JSON.parse(readFileSync(configPath, 'utf-8'));
    return typeof data === 'object' && data !== null ? data : {};
  } catch {
    return {};
  }
}
