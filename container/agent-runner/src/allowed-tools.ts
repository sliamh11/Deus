// Tool-allowlist construction for the CLAUDE-backend container run.
//
// Extracted from index.ts so the gating logic is unit-testable: index.ts runs
// `bootstrap(main, ...)` at module load, so importing helpers from it would
// execute the agent during tests. Pure functions only — no side effects — which
// also matches the existing helper-module pattern (doom-loop-detector.ts,
// context-registry.ts).

// Keywords that signal the prompt requires multi-agent *team* orchestration.
// When none are present and no external project is mounted, the team tools
// (TeamCreate, TeamDelete) are excluded from allowedTools to save ~200 tokens
// per call on personal-assistant queries.
//
// NOTE: SendMessage is deliberately NOT gated here. It pairs with the Task tool
// (always allowed) — the Task tool's own description advertises SendMessage as
// the way to continue a previously-spawned subagent (SDK sdk-tools.d.ts:297,
// "addressable via SendMessage({to: name})"). Gating it while Task stays
// unconditional made plain queries that spawned a Task subagent hit
// "SendMessage tool isn't available" (LIA-307).
export const SWARM_SIGNALS = [
  'parallel agent',
  'subagent',
  'agent team',
  'agent swarm',
  'orchestrate',
  'in parallel',
  'multiple agents',
  'spawn agent',
];

/**
 * Whether multi-agent *team* tools (TeamCreate/TeamDelete) should be offered.
 * True when an external project is mounted (engineering context) or the prompt
 * explicitly signals multi-agent intent via a SWARM_SIGNALS keyword.
 */
export function computeTeamsNeeded(
  prompt: string,
  hasProject: boolean,
): boolean {
  return (
    hasProject || SWARM_SIGNALS.some((kw) => prompt.toLowerCase().includes(kw))
  );
}

export interface AllowedToolsOpts {
  /** Result of computeTeamsNeeded — gates TeamCreate/TeamDelete only. */
  teamsNeeded: boolean;
  /** Google Calendar MCP server is available for this run. */
  hasGcalMcp: boolean;
  /** Linear MCP server is available for this run. */
  hasLinearMcp: boolean;
}

/**
 * Build the allowedTools manifest for a CLAUDE-backend run.
 *
 * SendMessage is unconditional (pairs with the always-present Task tool).
 * Only TeamCreate/TeamDelete are gated behind `teamsNeeded`.
 */
export function buildAllowedTools(opts: AllowedToolsOpts): string[] {
  const { teamsNeeded, hasGcalMcp, hasLinearMcp } = opts;
  return [
    'Bash',
    'Read',
    'Write',
    'Edit',
    'Glob',
    'Grep',
    'WebSearch',
    'WebFetch',
    'Task',
    'TaskOutput',
    'TaskStop',
    'SendMessage',
    ...(teamsNeeded ? ['TeamCreate', 'TeamDelete'] : []),
    'TodoWrite',
    'ToolSearch',
    'Skill',
    'NotebookEdit',
    'mcp__deus__*',
    ...(hasGcalMcp ? ['mcp__gcal__*'] : []),
    ...(hasLinearMcp ? ['mcp__linear__*'] : []),
  ];
}
