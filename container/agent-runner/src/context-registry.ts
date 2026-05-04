import fs from 'fs';
import path from 'path';

type ContextEntry = {
  label: string;
  path: string;
  skipForControlGroup?: boolean;
  projectOnly?: boolean;
  claudeSystemAppend?: boolean;
};

const WORKSPACE_ROOT = '/workspace';

function workspacePath(root: string, ...parts: string[]): string {
  return path.join(root, ...parts);
}

function baseContextEntries(root: string): ContextEntry[] {
  return [
    {
      label: 'GROUP RULES: AGENTS.md',
      path: workspacePath(root, 'group', 'AGENTS.md'),
      claudeSystemAppend: true,
    },
    {
      label: 'GROUP RULES: CLAUDE.md',
      path: workspacePath(root, 'group', 'CLAUDE.md'),
    },
    {
      label: 'GROUP RULES: AI_AGENT_GUIDELINES.md',
      path: workspacePath(root, 'group', 'AI_AGENT_GUIDELINES.md'),
      claudeSystemAppend: true,
    },
    {
      label: 'GLOBAL RULES: AGENTS.md',
      path: workspacePath(root, 'global', 'AGENTS.md'),
      skipForControlGroup: true,
      claudeSystemAppend: true,
    },
    {
      label: 'GLOBAL RULES: CLAUDE.md',
      path: workspacePath(root, 'global', 'CLAUDE.md'),
      skipForControlGroup: true,
      claudeSystemAppend: true,
    },
    {
      label: 'GLOBAL RULES: AI_AGENT_GUIDELINES.md',
      path: workspacePath(root, 'global', 'AI_AGENT_GUIDELINES.md'),
      skipForControlGroup: true,
      claudeSystemAppend: true,
    },
    {
      label: 'PROJECT RULES: AGENTS.md',
      path: workspacePath(root, 'project', 'AGENTS.md'),
      projectOnly: true,
      claudeSystemAppend: true,
    },
    {
      label: 'PROJECT RULES: CLAUDE.md',
      path: workspacePath(root, 'project', 'CLAUDE.md'),
      projectOnly: true,
    },
    {
      label: 'PROJECT RULES: AI_AGENT_GUIDELINES.md',
      path: workspacePath(root, 'project', 'AI_AGENT_GUIDELINES.md'),
      projectOnly: true,
      claudeSystemAppend: true,
    },
    {
      label: 'VAULT: AGENTS.md',
      path: workspacePath(root, 'vault', 'AGENTS.md'),
      claudeSystemAppend: true,
    },
    {
      label: 'VAULT: CLAUDE.md',
      path: workspacePath(root, 'vault', 'CLAUDE.md'),
      claudeSystemAppend: true,
    },
    {
      label: 'VAULT: AI_AGENT_GUIDELINES.md',
      path: workspacePath(root, 'vault', 'AI_AGENT_GUIDELINES.md'),
      claudeSystemAppend: true,
    },
    {
      label: 'VAULT: MEMORY_TREE.md',
      path: workspacePath(root, 'vault', 'MEMORY_TREE.md'),
      claudeSystemAppend: true,
    },
  ];
}

function extraContextEntries(root: string): ContextEntry[] {
  const extraRoot = workspacePath(root, 'extra');
  if (!fs.existsSync(extraRoot)) return [];

  return fs
    .readdirSync(extraRoot, { withFileTypes: true })
    .filter((entry) => entry.isDirectory())
    .flatMap((entry) => {
      const dir = workspacePath(extraRoot, entry.name);
      return [
        {
          label: `EXTRA RULES: ${entry.name}/AGENTS.md`,
          path: path.join(dir, 'AGENTS.md'),
          claudeSystemAppend: true,
        },
        {
          label: `EXTRA RULES: ${entry.name}/CLAUDE.md`,
          path: path.join(dir, 'CLAUDE.md'),
        },
        {
          label: `EXTRA RULES: ${entry.name}/AI_AGENT_GUIDELINES.md`,
          path: path.join(dir, 'AI_AGENT_GUIDELINES.md'),
          claudeSystemAppend: true,
        },
      ];
    });
}

const DEFAULT_CONTEXT_FILE_MAX_CHARS = 20_000;

function contextFileMaxChars(): number {
  const parsed = Number.parseInt(
    process.env.DEUS_CONTEXT_FILE_MAX_CHARS || '',
    10,
  );
  return Number.isFinite(parsed) && parsed > 0
    ? parsed
    : DEFAULT_CONTEXT_FILE_MAX_CHARS;
}

function readOptionalFile(
  filePath: string,
  maxChars = contextFileMaxChars(),
): string {
  try {
    if (!fs.existsSync(filePath)) return '';
    return fs.readFileSync(filePath, 'utf-8').slice(0, maxChars);
  } catch {
    return '';
  }
}

function formatContextFile(label: string, filePath: string): string {
  const content = readOptionalFile(filePath);
  return content ? `=== ${label} ===\n${content}` : '';
}

export function loadRegisteredContextFiles(options: {
  isControlGroup: boolean;
  hasProject: boolean;
  mode?: 'all' | 'claude-system-append';
  workspaceRoot?: string;
}): string[] {
  const root = options.workspaceRoot || WORKSPACE_ROOT;
  const entries = [...baseContextEntries(root), ...extraContextEntries(root)];
  return entries.flatMap((entry) => {
    if (options.mode === 'claude-system-append' && !entry.claudeSystemAppend)
      return [];
    if (entry.skipForControlGroup && options.isControlGroup) return [];
    if (entry.projectOnly && !options.hasProject) return [];
    const block = formatContextFile(entry.label, entry.path);
    return block ? [block] : [];
  });
}
