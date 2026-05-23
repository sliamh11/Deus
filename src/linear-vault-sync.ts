import fs from 'fs/promises';
import { existsSync, renameSync } from 'fs';
import path from 'path';
import { LinearClient } from '@linear/sdk';
import { logger } from './logger.js';

const STATE_PRIORITY: Record<string, number> = {
  'In Progress': 0,
  'In Review': 1,
  'Agent Working': 2,
  'Ready for Agent': 3,
  Todo: 4,
  Backlog: 5,
};

const EXCLUDED_STATE_TYPES = new Set(['completed', 'canceled']);

interface LinearIssueNode {
  title: string;
  identifier: string;
  url: string;
  state: { name: string; type: string };
}

export async function fetchActiveIssues(
  client: LinearClient,
  teamId: string,
): Promise<LinearIssueNode[]> {
  const result = await client.issues({
    filter: {
      state: { type: { nin: ['completed', 'canceled'] } },
      team: { id: { eq: teamId } },
    },
    first: 50,
  });

  const issues: LinearIssueNode[] = [];
  const stateRelations = result.nodes.map((n) => n.state);
  const states = await Promise.all(stateRelations);

  for (let i = 0; i < result.nodes.length; i++) {
    const node = result.nodes[i];
    const state = states[i];
    if (!state || EXCLUDED_STATE_TYPES.has(state.type)) continue;
    if (state.name === 'Duplicate') continue;
    issues.push({
      title: node.title,
      identifier: node.identifier,
      url: node.url,
      state: { name: state.name, type: state.type },
    });
  }

  issues.sort((a, b) => {
    const pa = STATE_PRIORITY[a.state.name] ?? 99;
    const pb = STATE_PRIORITY[b.state.name] ?? 99;
    if (pa !== pb) return pa - pb;
    const na = parseInt(a.identifier.replace(/\D/g, ''), 10) || 0;
    const nb = parseInt(b.identifier.replace(/\D/g, ''), 10) || 0;
    return na - nb;
  });

  return issues;
}

function buildPendingBlock(issues: LinearIssueNode[]): string {
  const lines = [
    'pending:',
    '  # Source of truth: Linear. Synced by /compress.',
  ];
  for (const issue of issues) {
    const title =
      issue.title.length > 80 ? issue.title.slice(0, 77) + '...' : issue.title;
    lines.push(`  - [ ] ${title} (${issue.identifier})`);
  }
  return lines.join('\n');
}

const PENDING_BLOCK_RE = /^pending:\s*\n((?:[ \t]+[^\n]*\n?)*)/m;

export async function syncVaultPending(
  client: LinearClient,
  teamId: string,
  vaultPath: string,
): Promise<void> {
  const claudeMdPath = path.join(vaultPath, 'CLAUDE.md');
  if (!existsSync(claudeMdPath)) {
    logger.warn({ path: claudeMdPath }, 'vault-sync: CLAUDE.md not found');
    return;
  }

  const issues = await fetchActiveIssues(client, teamId);
  const newBlock = buildPendingBlock(issues);

  const content = await fs.readFile(claudeMdPath, 'utf-8');

  let newContent: string;
  if (PENDING_BLOCK_RE.test(content)) {
    newContent = content.replace(PENDING_BLOCK_RE, () => newBlock + '\n');
  } else {
    newContent = content + '\n' + newBlock + '\n';
  }

  if (newContent === content) return;

  const tmpPath = claudeMdPath + '.tmp';
  await fs.writeFile(tmpPath, newContent, 'utf-8');
  renameSync(tmpPath, claudeMdPath);

  logger.info(
    { issueCount: issues.length },
    'vault-sync: updated pending block',
  );
}
