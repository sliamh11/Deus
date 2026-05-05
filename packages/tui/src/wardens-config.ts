import {
  existsSync,
  readFileSync,
  writeFileSync,
  renameSync,
  mkdirSync,
} from 'fs';
import { join, resolve } from 'path';
import { tmpdir } from 'os';
import { fileURLToPath } from 'url';

const __dirname = fileURLToPath(new URL('.', import.meta.url));
const REPO_ROOT = resolve(__dirname, '..', '..', '..');
const CONFIG_PATH = join(REPO_ROOT, '.claude', 'wardens', 'config.json');
const EXAMPLE_PATH = join(
  REPO_ROOT,
  '.claude',
  'wardens',
  'config.json.example',
);

export interface WardenEntry {
  enabled: boolean;
  tools?: string[];
  auto_threshold?: number;
  custom_instructions?: string | null;
  [key: string]: unknown;
}

export type WardensConfig = Record<string, WardenEntry>;

export const WARDEN_DESCRIPTIONS: Record<string, string> = {
  'plan-reviewer':
    'Reviews plans against Deus-specific rules before source edits',
  'code-reviewer':
    'Reviews code changes for quality and security before commits',
  'threat-modeler':
    'STRIDE/OWASP threat review for auth, data, and trust boundaries',
  'architecture-snapshot':
    'Generates architecture overview with Mermaid diagrams',
  'session-retrospective':
    'Cross-session pattern analysis and retrospective reports',
  'data-quality': 'Reviews auto-memory files for retrieval quality',
};

export const WARDEN_TYPES: Record<string, string> = {
  'plan-reviewer': 'Validator (blocking)',
  'code-reviewer': 'Validator (blocking)',
  'threat-modeler': 'Validator (warning)',
  'architecture-snapshot': 'Generator',
  'session-retrospective': 'Generator',
  'data-quality': 'Validator (manual)',
};

export const BLOCKING_WARDENS = new Set(['plan-reviewer', 'code-reviewer']);

export function loadWardensConfig(): WardensConfig {
  if (!existsSync(CONFIG_PATH)) {
    if (existsSync(EXAMPLE_PATH)) {
      const dir = join(REPO_ROOT, '.claude', 'wardens');
      mkdirSync(dir, { recursive: true });
      writeFileSync(CONFIG_PATH, readFileSync(EXAMPLE_PATH, 'utf-8'));
    } else {
      return {};
    }
  }
  try {
    const data = JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'));
    return typeof data === 'object' && data !== null ? data : {};
  } catch {
    return {};
  }
}

export function saveWardensConfig(config: WardensConfig): void {
  const content = JSON.stringify(config, null, 2) + '\n';
  const tmpFile = join(
    tmpdir(),
    `wardens-config-${process.pid}-${Date.now()}.tmp`,
  );
  writeFileSync(tmpFile, content, 'utf-8');
  renameSync(tmpFile, CONFIG_PATH);
}

export function triggersLabel(warden: WardenEntry, name: string): string {
  if (name === 'session-retrospective') {
    const threshold = warden.auto_threshold ?? 20;
    return `auto (threshold: ${threshold} sessions), manual`;
  }
  const tools = warden.tools;
  if (!tools || tools.length === 0) return 'manual';
  return tools.join(', ');
}
