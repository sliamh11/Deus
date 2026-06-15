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
// Excluded by state NAME (these survive the type filter): Duplicate, plus
// Icebox — someday/maybe ideas kept in Linear but out of the pending block.
const EXCLUDED_STATE_NAMES = new Set(['Duplicate', 'Icebox']);

// Linear issue priority: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low.
// Remap so Urgent sorts first and "No priority" sorts last. Mirrors
// PRIORITY_RANK in scripts/sync_linear_pending.py — keep the two in lockstep.
const PRIORITY_RANK: Record<number, number> = { 1: 0, 2: 1, 3: 2, 4: 3, 0: 4 };

// Linear's max page size is 250; we paginate so a team with more open issues
// is never silently truncated.
const PAGE_SIZE = 250;
// Defensive bound far above any real workspace; hitting it logs a warning.
const MAX_PAGES = 40;

interface LinearIssueNode {
  title: string;
  identifier: string;
  url: string;
  priority: number;
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
    first: PAGE_SIZE,
  });

  // Follow pagination: fetchNext() appends the next page to result.nodes
  // (cumulative) and updates result.pageInfo, so the consumer below reads the
  // full set.
  let pages = 1;
  while (result.pageInfo?.hasNextPage && pages < MAX_PAGES) {
    await result.fetchNext();
    pages++;
  }
  if (result.pageInfo?.hasNextPage) {
    logger.warn(
      { teamId, pages },
      'vault-sync: hit page cap; pending block may be truncated',
    );
  }

  const issues: LinearIssueNode[] = [];
  const stateRelations = result.nodes.map((n) => n.state);
  const states = await Promise.all(stateRelations);

  for (let i = 0; i < result.nodes.length; i++) {
    const node = result.nodes[i];
    const state = states[i];
    if (!state || EXCLUDED_STATE_TYPES.has(state.type)) continue;
    if (EXCLUDED_STATE_NAMES.has(state.name)) continue;
    issues.push({
      title: node.title,
      identifier: node.identifier,
      url: node.url,
      priority: node.priority ?? 0,
      state: { name: state.name, type: state.type },
    });
  }

  issues.sort((a, b) => {
    const ra = PRIORITY_RANK[a.priority] ?? 4;
    const rb = PRIORITY_RANK[b.priority] ?? 4;
    if (ra !== rb) return ra - rb;
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
