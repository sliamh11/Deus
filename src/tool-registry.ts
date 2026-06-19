/**
 * Tool registry for the host-side tool proxy.
 *
 * Reads an allowlist of CLI binaries from ~/.deus/tool-registry.json.
 * Only tools listed here may be executed via the proxy.
 *
 * Registry path: ~/.deus/tool-registry.json
 * Note: project convention uses CONFIG_DIR (~/.config/deus/) for most config,
 * but the printing-press adoption ADR specifies ~/.deus/ for this registry so
 * that it lives alongside evolution.db and memory.db.
 *
 * Example registry file:
 * {
 *   "tools": {
 *     "espn": { "binary": "/usr/local/bin/espn-pp-cli", "env": {} },
 *     "flights": {
 *       "binary": "/usr/local/bin/flight-goat-pp-cli",
 *       "env": { "KAYAK_API_KEY": "${KAYAK_API_KEY}" }
 *     }
 *   }
 * }
 */

import fs from 'fs';
import path from 'path';

import { homeDir } from './platform.js';
import { logger } from './logger.js';

/** Per-tool configuration from the registry. */
export interface ToolConfig {
  /** Absolute path to the host binary. */
  binary: string;
  /**
   * Extra environment variables to inject when executing this tool.
   * Values may contain ${VAR_NAME} placeholders resolved against process.env.
   */
  env: Record<string, string>;
  /** Maximum execution time in milliseconds. Falls back to DEFAULT_TOOL_TIMEOUT. */
  timeout?: number;
}

interface ToolRegistryFile {
  tools: Record<string, ToolConfig>;
}

const REGISTRY_PATH = path.join(homeDir, '.deus', 'tool-registry.json');

let cachedRegistry: ToolRegistryFile | null = null;

/** Load (or reload) the tool registry from disk. Errors are non-fatal. */
export function loadRegistry(): ToolRegistryFile {
  try {
    const raw = fs.readFileSync(REGISTRY_PATH, 'utf-8');
    const parsed = JSON.parse(raw) as ToolRegistryFile;
    if (
      !parsed.tools ||
      typeof parsed.tools !== 'object' ||
      Array.isArray(parsed.tools)
    ) {
      // An array satisfies `typeof === 'object'`; without the Array.isArray
      // guard its numeric index keys would leak through hasOwnProperty as
      // allowlisted tools. Fail closed on any non-plain-object shape.
      logger.warn(
        { path: REGISTRY_PATH },
        'Tool registry "tools" is missing or not a plain object',
      );
      return { tools: {} };
    }
    cachedRegistry = parsed;
    logger.debug(
      { count: Object.keys(parsed.tools).length, path: REGISTRY_PATH },
      'Tool registry loaded',
    );
    return parsed;
  } catch (err) {
    if ((err as NodeJS.ErrnoException).code !== 'ENOENT') {
      logger.warn({ err, path: REGISTRY_PATH }, 'Failed to load tool registry');
    }
    return { tools: {} };
  }
}

/**
 * Resolve ${VAR_NAME} placeholders in a string against process.env.
 * Unknown variables are substituted with an empty string.
 */
function resolveEnvPlaceholders(value: string): string {
  return value.replace(/\$\{([^}]+)\}/g, (_match, name: string) => {
    return process.env[name] ?? '';
  });
}

/** Return true if the named tool is registered in the allowlist. */
export function isAllowed(name: string): boolean {
  const registry = cachedRegistry ?? loadRegistry();
  return Object.prototype.hasOwnProperty.call(registry.tools, name);
}

/**
 * Return the resolved configuration for a registered tool, or null if not found.
 * Resolves ${VAR_NAME} placeholders in env values against process.env at call
 * time (not load time) so env changes after startup are picked up.
 */
export function getToolConfig(name: string): ToolConfig | null {
  const registry = cachedRegistry ?? loadRegistry();
  const config = registry.tools[name];
  if (!config) return null;

  const resolvedEnv: Record<string, string> = {};
  for (const [key, val] of Object.entries(config.env)) {
    resolvedEnv[key] = resolveEnvPlaceholders(val);
  }

  return { ...config, env: resolvedEnv };
}
