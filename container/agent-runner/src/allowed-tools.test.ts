import { describe, it, expect } from 'vitest';
import {
  buildAllowedTools,
  computeTeamsNeeded,
  SWARM_SIGNALS,
} from './allowed-tools.js';

describe('buildAllowedTools', () => {
  const base = { hasGcalMcp: false, hasLinearMcp: false };

  it('always includes SendMessage — it pairs with the always-present Task tool (LIA-307)', () => {
    expect(buildAllowedTools({ ...base, teamsNeeded: false })).toContain(
      'SendMessage',
    );
    expect(buildAllowedTools({ ...base, teamsNeeded: true })).toContain(
      'SendMessage',
    );
  });

  it('always includes the Task tools regardless of teamsNeeded', () => {
    for (const teamsNeeded of [false, true]) {
      const tools = buildAllowedTools({ ...base, teamsNeeded });
      expect(tools).toEqual(
        expect.arrayContaining(['Task', 'TaskOutput', 'TaskStop']),
      );
    }
  });

  it('gates TeamCreate/TeamDelete behind teamsNeeded', () => {
    const off = buildAllowedTools({ ...base, teamsNeeded: false });
    expect(off).not.toContain('TeamCreate');
    expect(off).not.toContain('TeamDelete');

    const on = buildAllowedTools({ ...base, teamsNeeded: true });
    expect(on).toContain('TeamCreate');
    expect(on).toContain('TeamDelete');
  });

  it('gates the gcal/linear MCP wildcards by their flags', () => {
    const none = buildAllowedTools({ ...base, teamsNeeded: false });
    expect(none).not.toContain('mcp__gcal__*');
    expect(none).not.toContain('mcp__linear__*');

    const both = buildAllowedTools({
      teamsNeeded: false,
      hasGcalMcp: true,
      hasLinearMcp: true,
    });
    expect(both).toContain('mcp__gcal__*');
    expect(both).toContain('mcp__linear__*');
  });

  it('always includes the core + deus MCP tools', () => {
    const tools = buildAllowedTools({ ...base, teamsNeeded: false });
    expect(tools).toEqual(
      expect.arrayContaining([
        'Bash',
        'Read',
        'Write',
        'Edit',
        'Glob',
        'Grep',
        'WebSearch',
        'WebFetch',
        'TodoWrite',
        'ToolSearch',
        'Skill',
        'NotebookEdit',
        'mcp__deus__*',
      ]),
    );
  });
});

describe('computeTeamsNeeded', () => {
  it('is true when an external project is mounted, even with a plain prompt', () => {
    expect(computeTeamsNeeded('what time is it?', true)).toBe(true);
  });

  it('is true when the prompt contains any swarm signal (case-insensitive)', () => {
    for (const kw of SWARM_SIGNALS) {
      expect(computeTeamsNeeded(`please ${kw.toUpperCase()} this`, false)).toBe(
        true,
      );
    }
  });

  it('is false for a plain query with no project and no swarm signal', () => {
    expect(computeTeamsNeeded('remind me to buy milk', false)).toBe(false);
  });
});
