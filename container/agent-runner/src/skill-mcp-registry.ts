/**
 * Container-side skill MCP tool registry.
 *
 * At startup, scans /app/skills/{name}/agent.js for skill MCP tool definitions.
 * Each agent.js must export a `registerTools` function that receives the
 * MCP server instance and a context object.
 *
 * Skill agent files are copied into the container at build time from
 * .claude/skills/{name}/agent.ts. They are compiled alongside the agent-runner
 * source by the container entrypoint's tsc step.
 *
 * This enables community-contributed MCP tool templates that extend the
 * agent's capabilities without modifying ipc-mcp-stdio.ts.
 */

import { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js';
import fs from 'fs';
import path from 'path';

export interface SkillMcpContext {
  groupFolder: string;
  chatJid: string;
  isMain: boolean;
  ipcDir: string;
}

export type RegisterToolsFn = (server: McpServer, ctx: SkillMcpContext) => void;

const SKILLS_DIR = '/app/skills';

export async function loadSkillMcpTools(
  server: McpServer,
  ctx: SkillMcpContext,
): Promise<void> {
  if (!fs.existsSync(SKILLS_DIR)) return;

  let entries: fs.Dirent[];
  try {
    entries = fs.readdirSync(SKILLS_DIR, { withFileTypes: true });
  } catch {
    return;
  }

  for (const entry of entries) {
    if (!entry.isDirectory()) continue;

    const agentPath = path.join(SKILLS_DIR, entry.name, 'agent.js');
    if (!fs.existsSync(agentPath)) continue;

    try {
      const mod = await import(agentPath);
      if (typeof mod.registerTools === 'function') {
        mod.registerTools(server, ctx);
        console.error(`[skill-mcp] Loaded tools from skill: ${entry.name}`);
      }
    } catch (err) {
      console.error(`[skill-mcp] Failed to load skill ${entry.name}: ${err}`);
    }
  }
}
