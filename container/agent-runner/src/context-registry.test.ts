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
    workspaceRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'deus-context-'));
  });

  afterEach(() => {
    fs.rmSync(workspaceRoot, { recursive: true, force: true });
  });

  it('loads Claude and AGENTS surfaces in stable legacy-first order', () => {
    writeContextFile('group/CLAUDE.md', 'group claude');
    writeContextFile('group/AGENTS.md', 'group agents');
    writeContextFile('group/AI_AGENT_GUIDELINES.md', 'group guidelines');
    writeContextFile('vault/CLAUDE.md', 'vault claude');
    writeContextFile('vault/AGENTS.md', 'vault agents');
    writeContextFile('vault/AI_AGENT_GUIDELINES.md', 'vault guidelines');
    writeContextFile('vault/STATE.md', 'vault state');
    writeContextFile('vault/MEMORY_TREE.md', 'vault tree');

    expect(
      loadRegisteredContextFiles({
        isControlGroup: false,
        hasProject: false,
        workspaceRoot,
      }),
    ).toEqual([
      '=== GROUP RULES: CLAUDE.md ===\ngroup claude',
      '=== GROUP RULES: AGENTS.md ===\ngroup agents',
      '=== GROUP RULES: AI_AGENT_GUIDELINES.md ===\ngroup guidelines',
      '=== VAULT: CLAUDE.md ===\nvault claude',
      '=== VAULT: AGENTS.md ===\nvault agents',
      '=== VAULT: AI_AGENT_GUIDELINES.md ===\nvault guidelines',
      '=== VAULT: STATE.md ===\nvault state',
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

  it('adds extra mounted directory rules for OpenAI parity with Claude additionalDirectories', () => {
    writeContextFile('extra/reference/CLAUDE.md', 'extra claude');
    writeContextFile('extra/reference/AGENTS.md', 'extra agents');
    writeContextFile(
      'extra/reference/AI_AGENT_GUIDELINES.md',
      'extra guidelines',
    );
    writeContextFile('extra/reference/docs/AGENT_DEUS_101.md', 'extra 101');

    expect(
      loadRegisteredContextFiles({
        isControlGroup: false,
        hasProject: false,
        workspaceRoot,
      }),
    ).toEqual([
      '=== EXTRA RULES: reference/CLAUDE.md ===\nextra claude',
      '=== EXTRA RULES: reference/AGENTS.md ===\nextra agents',
      '=== EXTRA RULES: reference/AI_AGENT_GUIDELINES.md ===\nextra guidelines',
      '=== EXTRA RULES: reference/docs/AGENT_DEUS_101.md ===\nextra 101',
    ]);
  });

  it('loads Agent Deus 101 from project context when available', () => {
    writeContextFile('project/docs/AGENT_DEUS_101.md', 'deus onboarding');

    expect(
      loadRegisteredContextFiles({
        isControlGroup: false,
        hasProject: true,
        workspaceRoot,
      }),
    ).toEqual([
      '=== PROJECT RULES: docs/AGENT_DEUS_101.md ===\ndeus onboarding',
    ]);
  });

  it('keeps Claude system append to non-native surfaces only', () => {
    writeContextFile('group/CLAUDE.md', 'group claude');
    writeContextFile('group/AGENTS.md', 'group agents');
    writeContextFile('project/CLAUDE.md', 'project claude');
    writeContextFile('project/AGENTS.md', 'project agents');
    writeContextFile('project/AI_AGENT_GUIDELINES.md', 'project guidelines');
    writeContextFile('vault/CLAUDE.md', 'vault claude');
    writeContextFile('vault/STATE.md', 'vault state');
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
      '=== VAULT: CLAUDE.md ===\nvault claude',
      '=== VAULT: STATE.md ===\nvault state',
      '=== VAULT: MEMORY_TREE.md ===\nvault tree',
      '=== EXTRA RULES: reference/AGENTS.md ===\nextra agents',
      '=== EXTRA RULES: reference/AI_AGENT_GUIDELINES.md ===\nextra guidelines',
    ]);
  });
});
