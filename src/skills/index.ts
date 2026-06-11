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

    const hasJs = fs.existsSync(hostJs);
    const hasTs = fs.existsSync(hostTs);
    if (!hasJs && hasTs) {
      // The compiled production service (node dist/) has no TS loader, so a
      // direct import() of host.ts throws ERR_UNKNOWN_FILE_EXTENSION and the
      // handler silently never registers. Make that misconfig LOUD. Run
      // `npm run build` (tsconfig.skills.json) to emit host.js; the .ts
      // fallback below only works under tsx/dev.
      logger.warn(
        { skill: entry.name, hostTs },
        'Skill ships host.ts but no compiled host.js — it will NOT load in the ' +
          'compiled production service. Run `npm run build` to generate host.js.',
      );
    }

    const resolvedPath = hasJs ? hostJs : hasTs ? hostTs : null;
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
