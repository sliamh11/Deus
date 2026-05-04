import fs from 'fs';
import os from 'os';
import path from 'path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { loadRegisteredContextFiles } from './context-registry.js';

let workspaceRoot: string;

function writeContextFile(relativePath: string, content: string): void {
  const filePath = path.join(workspaceRoot, relativePath);
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, content);
}

describe('context registry', () => {
  beforeEach(() => {
    delete process.env.DEUS_CONTEXT_FILE_MAX_CHARS;
    workspaceRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-context-'));
  });

  afterEach(() => {
    delete process.env.DEUS_CONTEXT_FILE_MAX_CHARS;
    fs.rmSync(workspaceRoot, { recursive: true, force: true });
  });

  it('loads AGENTS as the canonical surface before compatibility mirrors', () => {
    writeContextFile('group/CLAUDE.md', 'group claude');
    writeContextFile('group/AGENTS.md', 'group agents');
    writeContextFile('group/AI_AGENT_GUIDELINES.md', 'group guidelines');
    writeContextFile('vault/CLAUDE.md', 'vault claude');
    writeContextFile('vault/AGENTS.md', 'vault agents');
    writeContextFile('vault/AI_AGENT_GUIDELINES.md', 'vault guidelines');
    writeContextFile('vault/MEMORY_TREE.md', 'vault tree');

    expect(
      loadRegisteredContextFiles({
        isControlGroup: false,
        hasProject: false,
        workspaceRoot,
      }),
    ).toEqual([
      '=== GROUP RULES: AGENTS.md ===\ngroup agents',
      '=== GROUP RULES: CLAUDE.md ===\ngroup claude',
      '=== GROUP RULES: AI_AGENT_GUIDELINES.md ===\ngroup guidelines',
      '=== VAULT: AGENTS.md ===\nvault agents',
      '=== VAULT: CLAUDE.md ===\nvault claude',
      '=== VAULT: AI_AGENT_GUIDELINES.md ===\nvault guidelines',
      '=== VAULT: MEMORY_TREE.md ===\nvault tree',
    ]);
  });

  it('excludes global context for control groups and project context without project mounts', () => {
    writeContextFile('global/CLAUDE.md', 'global claude');
    writeContextFile('project/CLAUDE.md', 'project claude');

    expect(
      loadRegisteredContextFiles({
        isControlGroup: true,
        hasProject: false,
        workspaceRoot,
      }),
    ).toEqual([]);
  });

  it('loads global canonical surfaces before compatibility mirrors for non-control groups', () => {
    writeContextFile('global/CLAUDE.md', 'global claude');
    writeContextFile('global/AGENTS.md', 'global agents');
    writeContextFile('global/AI_AGENT_GUIDELINES.md', 'global guidelines');

    expect(
      loadRegisteredContextFiles({
        isControlGroup: false,
        hasProject: false,
        workspaceRoot,
      }),
    ).toEqual([
      '=== GLOBAL RULES: AGENTS.md ===\nglobal agents',
      '=== GLOBAL RULES: CLAUDE.md ===\nglobal claude',
      '=== GLOBAL RULES: AI_AGENT_GUIDELINES.md ===\nglobal guidelines',
    ]);
  });

  it('adds extra mounted directory rules without auto-loading deep references', () => {
    writeContextFile('extra/reference/AGENTS.md', 'extra agents');
    writeContextFile('extra/reference/CLAUDE.md', 'extra claude');
    writeContextFile(
      'extra/reference/AI_AGENT_GUIDELINES.md',
      'extra guidelines',
    );

    expect(
      loadRegisteredContextFiles({
        isControlGroup: false,
        hasProject: false,
        workspaceRoot,
      }),
    ).toEqual([
      '=== EXTRA RULES: reference/AGENTS.md ===\nextra agents',
      '=== EXTRA RULES: reference/CLAUDE.md ===\nextra claude',
      '=== EXTRA RULES: reference/AI_AGENT_GUIDELINES.md ===\nextra guidelines',
    ]);
  });

  it('does not auto-load Agent Deus 101 from project context', () => {
    writeContextFile('project/docs/AGENT_DEUS_101.md', 'deus onboarding');

    expect(
      loadRegisteredContextFiles({
        isControlGroup: false,
        hasProject: true,
        workspaceRoot,
      }),
    ).toEqual([]);
  });

  it('never auto-loads Agent Deus 101 from registered scopes or Claude append', () => {
    writeContextFile('group/docs/AGENT_DEUS_101.md', 'group onboarding');
    writeContextFile('global/docs/AGENT_DEUS_101.md', 'global onboarding');
    writeContextFile('project/docs/AGENT_DEUS_101.md', 'project onboarding');
    writeContextFile('vault/docs/AGENT_DEUS_101.md', 'vault onboarding');
    writeContextFile(
      'extra/reference/docs/AGENT_DEUS_101.md',
      'extra onboarding',
    );
    writeContextFile('group/AGENTS.md', 'group agents');
    writeContextFile('project/AGENTS.md', 'project agents');
    writeContextFile('vault/AGENTS.md', 'vault agents');
    writeContextFile(
      'extra/reference/AI_AGENT_GUIDELINES.md',
      'extra guidelines',
    );

    const allContext = loadRegisteredContextFiles({
      isControlGroup: false,
      hasProject: true,
      workspaceRoot,
    });
    const claudeAppendContext = loadRegisteredContextFiles({
      isControlGroup: false,
      hasProject: true,
      mode: 'claude-system-append',
      workspaceRoot,
    });

    expect(allContext).toEqual([
      '=== GROUP RULES: AGENTS.md ===\ngroup agents',
      '=== PROJECT RULES: AGENTS.md ===\nproject agents',
      '=== VAULT: AGENTS.md ===\nvault agents',
      '=== EXTRA RULES: reference/AI_AGENT_GUIDELINES.md ===\nextra guidelines',
    ]);
    expect(claudeAppendContext).toEqual(allContext);
  });

  it('honors DEUS_CONTEXT_FILE_MAX_CHARS for registered context surfaces', () => {
    process.env.DEUS_CONTEXT_FILE_MAX_CHARS = '8';
    writeContextFile('group/CLAUDE.md', '1234567890');

    expect(
      loadRegisteredContextFiles({
        isControlGroup: false,
        hasProject: false,
        workspaceRoot,
      }),
    ).toEqual(['=== GROUP RULES: CLAUDE.md ===\n12345678']);
  });

  it('keeps Claude system append on canonical and parity surfaces only', () => {
    writeContextFile('group/CLAUDE.md', 'group claude');
    writeContextFile('group/AGENTS.md', 'group agents');
    writeContextFile('project/CLAUDE.md', 'project claude');
    writeContextFile('project/AGENTS.md', 'project agents');
    writeContextFile('project/AI_AGENT_GUIDELINES.md', 'project guidelines');
    writeContextFile('vault/AGENTS.md', 'vault agents');
    writeContextFile('vault/CLAUDE.md', 'vault claude');
    writeContextFile('vault/MEMORY_TREE.md', 'vault tree');
    writeContextFile('extra/reference/CLAUDE.md', 'extra claude');
    writeContextFile('extra/reference/AGENTS.md', 'extra agents');
    writeContextFile(
      'extra/reference/AI_AGENT_GUIDELINES.md',
      'extra guidelines',
    );

    expect(
      loadRegisteredContextFiles({
        isControlGroup: false,
        hasProject: true,
        mode: 'claude-system-append',
        workspaceRoot,
      }),
    ).toEqual([
      '=== GROUP RULES: AGENTS.md ===\ngroup agents',
      '=== PROJECT RULES: AGENTS.md ===\nproject agents',
      '=== PROJECT RULES: AI_AGENT_GUIDELINES.md ===\nproject guidelines',
      '=== VAULT: AGENTS.md ===\nvault agents',
      '=== VAULT: CLAUDE.md ===\nvault claude',
      '=== VAULT: MEMORY_TREE.md ===\nvault tree',
      '=== EXTRA RULES: reference/AGENTS.md ===\nextra agents',
      '=== EXTRA RULES: reference/AI_AGENT_GUIDELINES.md ===\nextra guidelines',
    ]);
  });
});
