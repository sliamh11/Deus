import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import fs from 'fs';
import path from 'path';
import os from 'os';
import { syncVaultPending, fetchActiveIssues } from './linear-vault-sync.js';
import { logger } from './logger.js';

function issueNode(id: string, name = 'Todo', type = 'unstarted') {
  return {
    title: id,
    identifier: id,
    url: '',
    state: Promise.resolve({ name, type }),
  };
}

function mockLinearClient(
  issues: Array<{
    title: string;
    identifier: string;
    url: string;
    stateName: string;
    stateType: string;
    priority?: number;
  }>,
) {
  // Only .issues() is exercised; full LinearClient has ~40 methods
  return {
    issues: vi.fn().mockResolvedValue({
      nodes: issues.map((i) => ({
        title: i.title,
        identifier: i.identifier,
        url: i.url,
        priority: i.priority,
        state: Promise.resolve({ name: i.stateName, type: i.stateType }),
      })),
    }),
  } as any;
}

describe('fetchActiveIssues', () => {
  it('filters out completed and canceled issues', async () => {
    const client = mockLinearClient([
      {
        title: 'Active',
        identifier: 'LIA-1',
        url: 'https://linear.app/t/issue/LIA-1/',
        stateName: 'Todo',
        stateType: 'unstarted',
      },
      {
        title: 'Done',
        identifier: 'LIA-2',
        url: 'https://linear.app/t/issue/LIA-2/',
        stateName: 'Done',
        stateType: 'completed',
      },
      {
        title: 'Canceled',
        identifier: 'LIA-3',
        url: 'https://linear.app/t/issue/LIA-3/',
        stateName: 'Canceled',
        stateType: 'canceled',
      },
    ]);
    const result = await fetchActiveIssues(client, 'team-1');
    expect(result).toHaveLength(1);
    expect(result[0].identifier).toBe('LIA-1');
  });

  it('filters out Duplicate state', async () => {
    const client = mockLinearClient([
      {
        title: 'Dup',
        identifier: 'LIA-4',
        url: 'https://linear.app/t/issue/LIA-4/',
        stateName: 'Duplicate',
        stateType: 'canceled',
      },
    ]);
    const result = await fetchActiveIssues(client, 'team-1');
    expect(result).toHaveLength(0);
  });

  it('sorts by state priority then identifier number', async () => {
    const client = mockLinearClient([
      {
        title: 'Backlog item',
        identifier: 'LIA-10',
        url: '',
        stateName: 'Backlog',
        stateType: 'backlog',
      },
      {
        title: 'In progress',
        identifier: 'LIA-5',
        url: '',
        stateName: 'In Progress',
        stateType: 'started',
      },
      {
        title: 'Todo second',
        identifier: 'LIA-8',
        url: '',
        stateName: 'Todo',
        stateType: 'unstarted',
      },
      {
        title: 'Todo first',
        identifier: 'LIA-3',
        url: '',
        stateName: 'Todo',
        stateType: 'unstarted',
      },
    ]);
    const result = await fetchActiveIssues(client, 'team-1');
    expect(result.map((i) => i.identifier)).toEqual([
      'LIA-5',
      'LIA-3',
      'LIA-8',
      'LIA-10',
    ]);
  });

  it('sorts by issue priority first: Urgent before High before None', async () => {
    // Same state (Backlog) so only priority distinguishes them.
    // Linear priority: 0=None, 1=Urgent, 2=High, 3=Medium, 4=Low.
    const client = mockLinearClient([
      {
        title: 'No priority',
        identifier: 'LIA-1',
        url: '',
        stateName: 'Backlog',
        stateType: 'backlog',
        priority: 0,
      },
      {
        title: 'High',
        identifier: 'LIA-2',
        url: '',
        stateName: 'Backlog',
        stateType: 'backlog',
        priority: 2,
      },
      {
        title: 'Urgent',
        identifier: 'LIA-3',
        url: '',
        stateName: 'Backlog',
        stateType: 'backlog',
        priority: 1,
      },
    ]);
    const result = await fetchActiveIssues(client, 'team-1');
    expect(result.map((i) => i.identifier)).toEqual([
      'LIA-3', // Urgent
      'LIA-2', // High
      'LIA-1', // None (last)
    ]);
  });

  it('priority outranks state and identifier (frozen LIA-9/LIA-10 case)', async () => {
    // Urgent LIA-9 must sort before Medium LIA-10 even though both are Backlog
    // and LIA-9 has the higher number.
    const client = mockLinearClient([
      {
        title: 'Medium task',
        identifier: 'LIA-10',
        url: '',
        stateName: 'Backlog',
        stateType: 'backlog',
        priority: 3,
      },
      {
        title: 'Urgent task',
        identifier: 'LIA-9',
        url: '',
        stateName: 'Backlog',
        stateType: 'backlog',
        priority: 1,
      },
    ]);
    const result = await fetchActiveIssues(client, 'team-1');
    expect(result.map((i) => i.identifier)).toEqual(['LIA-9', 'LIA-10']);
  });

  it('priority dominates STATE_PRIORITY (Urgent+Backlog before None+In Progress)', async () => {
    // Priority is prepended to the sort key, so an Urgent issue in a low-ranked
    // state must still outrank a No-priority issue in a high-ranked state.
    const client = mockLinearClient([
      {
        title: 'No priority, In Progress',
        identifier: 'LIA-1',
        url: '',
        stateName: 'In Progress',
        stateType: 'started',
        priority: 0,
      },
      {
        title: 'Urgent, Backlog',
        identifier: 'LIA-2',
        url: '',
        stateName: 'Backlog',
        stateType: 'backlog',
        priority: 1,
      },
    ]);
    const result = await fetchActiveIssues(client, 'team-1');
    expect(result.map((i) => i.identifier)).toEqual(['LIA-2', 'LIA-1']);
  });

  it('treats missing priority as No priority (sorts last)', async () => {
    const client = mockLinearClient([
      {
        title: 'Unset priority',
        identifier: 'LIA-1',
        url: '',
        stateName: 'Backlog',
        stateType: 'backlog',
        // priority omitted -> undefined -> 0 -> rank last
      },
      {
        title: 'Urgent',
        identifier: 'LIA-2',
        url: '',
        stateName: 'Backlog',
        stateType: 'backlog',
        priority: 1,
      },
    ]);
    const result = await fetchActiveIssues(client, 'team-1');
    expect(result.map((i) => i.identifier)).toEqual(['LIA-2', 'LIA-1']);
  });

  it('paginates across multiple pages via fetchNext', async () => {
    // Page 1 has a next page; fetchNext appends page 2 and clears hasNextPage
    // (the @linear/sdk contract: fetchNext mutates connection.nodes in place).
    const connection: any = {
      nodes: [issueNode('LIA-1')],
      pageInfo: { hasNextPage: true },
      fetchNext: vi.fn(async () => {
        connection.nodes.push(issueNode('LIA-2'));
        connection.pageInfo = { hasNextPage: false };
        return connection;
      }),
    };
    const client = { issues: vi.fn().mockResolvedValue(connection) } as any;

    const result = await fetchActiveIssues(client, 'team-1');
    // Both pages present (both Todo → sorted by identifier number).
    expect(result.map((i) => i.identifier)).toEqual(['LIA-1', 'LIA-2']);
    expect(connection.fetchNext).toHaveBeenCalledTimes(1);
  });

  it('stops at the page cap and warns instead of looping forever', async () => {
    const warnSpy = vi
      .spyOn(logger, 'warn')
      .mockImplementation(() => undefined as any);
    // fetchNext never clears hasNextPage → would loop forever without the cap.
    const connection: any = {
      nodes: [issueNode('LIA-1')],
      pageInfo: { hasNextPage: true },
      fetchNext: vi.fn(async () => {
        connection.nodes.push(issueNode(`LIA-${connection.nodes.length + 1}`));
        return connection;
      }),
    };
    const client = { issues: vi.fn().mockResolvedValue(connection) } as any;

    await fetchActiveIssues(client, 'team-1');
    // MAX_PAGES = 40, loop starts at pages=1 → fetchNext called 39 times.
    expect(connection.fetchNext).toHaveBeenCalledTimes(39);
    expect(warnSpy).toHaveBeenCalledTimes(1);
    warnSpy.mockRestore();
  });
});

describe('syncVaultPending', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'vault-sync-test-'));
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('replaces existing pending block', async () => {
    const claudeMd = path.join(tmpDir, 'CLAUDE.md');
    fs.writeFileSync(
      claudeMd,
      [
        '---',
        'name: Test',
        '---',
        'name: Test',
        'pending:',
        '  - [ ] Old task (LIA-99)',
        'index:',
        '  infra: INFRA.md',
      ].join('\n'),
    );

    const client = mockLinearClient([
      {
        title: 'New task',
        identifier: 'LIA-1',
        url: '',
        stateName: 'Todo',
        stateType: 'unstarted',
      },
    ]);

    await syncVaultPending(client, 'team-1', tmpDir);

    const result = fs.readFileSync(claudeMd, 'utf-8');
    expect(result).toContain('New task (LIA-1)');
    expect(result).not.toContain('Old task (LIA-99)');
    expect(result).toContain('index:');
    expect(result).toContain('Source of truth: Linear');
  });

  it('appends pending block when none exists', async () => {
    const claudeMd = path.join(tmpDir, 'CLAUDE.md');
    fs.writeFileSync(claudeMd, '---\nname: Test\n---\nname: Test\n');

    const client = mockLinearClient([
      {
        title: 'First task',
        identifier: 'LIA-1',
        url: '',
        stateName: 'Todo',
        stateType: 'unstarted',
      },
    ]);

    await syncVaultPending(client, 'team-1', tmpDir);

    const result = fs.readFileSync(claudeMd, 'utf-8');
    expect(result).toContain('pending:');
    expect(result).toContain('First task (LIA-1)');
  });

  it('truncates long titles', async () => {
    const claudeMd = path.join(tmpDir, 'CLAUDE.md');
    fs.writeFileSync(claudeMd, '---\nname: Test\n---\npending:\n  - [ ] old\n');

    const longTitle = 'A'.repeat(100);
    const client = mockLinearClient([
      {
        title: longTitle,
        identifier: 'LIA-1',
        url: '',
        stateName: 'Todo',
        stateType: 'unstarted',
      },
    ]);

    await syncVaultPending(client, 'team-1', tmpDir);

    const result = fs.readFileSync(claudeMd, 'utf-8');
    const line = result.split('\n').find((l: string) => l.includes('LIA-1'))!;
    expect(line.length).toBeLessThan(120);
    expect(line).toContain('...');
  });

  it('skips write when content unchanged', async () => {
    const claudeMd = path.join(tmpDir, 'CLAUDE.md');
    const content =
      'pending:\n  # Source of truth: Linear. Synced by /compress.\n  - [ ] Task (LIA-1)\n';
    fs.writeFileSync(claudeMd, content);
    const mtime = fs.statSync(claudeMd).mtimeMs;

    const client = mockLinearClient([
      {
        title: 'Task',
        identifier: 'LIA-1',
        url: '',
        stateName: 'Todo',
        stateType: 'unstarted',
      },
    ]);

    await syncVaultPending(client, 'team-1', tmpDir);

    const newMtime = fs.statSync(claudeMd).mtimeMs;
    expect(newMtime).toBe(mtime);
  });
});
