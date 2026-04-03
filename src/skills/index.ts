/**
 * Skill auto-discovery.
 *
 * Scans .claude/skills/[name]/host.js (or host.ts via tsx) for skill IPC handlers.
 * Each host module must export a `register` function that receives
 * `registerSkillIpcHandler` and calls it to register its handler(s).
 *
 * Convention: each skill lives in .claude/skills/[name]/ with:
 *   SKILL.md       - documentation (committed, community template)
 *   host.ts        - host-side IPC handler (local-only for private skills)
 *   agent.ts       - container-side MCP tools (copied into container at build)
 *   scripts/       - subprocess scripts spawned by host.ts
 *   package.json   - skill-specific dependencies (local-only)
 *
 * Community contributors commit SKILL.md + agent.ts as templates.
 * Users apply skills locally, generating host.ts with their config.
 */

import fs from 'fs';
import path from 'path';
import { pathToFileURL } from 'url';

import { registerSkillIpcHandler } from './registry.js';
import { logger } from '../logger.js';

const SKILLS_DIR = path.join(process.cwd(), '.claude', 'skills');

export async function loadSkillIpcHandlers(): Promise<void> {
  if (!fs.existsSync(SKILLS_DIR)) return;

  const entries = fs.readdirSync(SKILLS_DIR, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;

    // Check for compiled .js first, then .ts (requires tsx loader)
    const hostJs = path.join(SKILLS_DIR, entry.name, 'host.js');
    const hostTs = path.join(SKILLS_DIR, entry.name, 'host.ts');

    const resolvedPath = fs.existsSync(hostJs)
      ? hostJs
      : fs.existsSync(hostTs)
        ? hostTs
        : null;
    if (!resolvedPath) continue;

    try {
      // Use file URL for cross-platform ESM import compatibility
      const mod = await import(pathToFileURL(resolvedPath).href);
      if (typeof mod.register === 'function') {
        mod.register(registerSkillIpcHandler);
        logger.info({ skill: entry.name }, 'Skill IPC handler registered');
      } else {
        logger.warn(
          { skill: entry.name },
          'Skill host module missing register() export',
        );
      }
    } catch (err) {
      logger.error(
        { skill: entry.name, err },
        'Failed to load skill IPC handler',
      );
    }
  }
}
