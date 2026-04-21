# Architecture Schema -- Wardens/architecture-snapshot

> Output schema for the architecture-snapshot generator warden.
> Defines required sections, reading hints, and completeness criteria.

## save-location
**Path:** `docs/ARCHITECTURE.md`
**Action:** Update in-place (preserve existing section structure, regenerate diagrams and tables).

## reading-hints

Project-specific hints for the agent's scan phase:

- `src/private/` -- skip (gitignored, personal overrides)
- `src/skills/` -- read index files only; each skill follows the same interface
- `container/agent-runner/src/` -- read index.ts and ipc-mcp-stdio.ts; skip helpers
- `packages/mcp-channel-core/` -- read server-base.ts (common tools registration)
- `packages/mcp-*/` -- read index.ts only per channel; they follow the same pattern
- `evolution/` -- read cli.py + judge/provider.py + reflexion/store.py; skip internals
- `scripts/` -- scan filenames for purpose; read only memory_indexer.py and memory_tree.py
- `node_modules/`, `dist/`, `.coverage/` -- skip entirely

## required-sections
- System Overview (Required: yes -- must include a Mermaid diagram)
- Entry Points (Required: yes)
- Key Abstractions (Required: yes -- minimum 3 entries)
- Data Flow (Required: yes -- Mermaid sequence diagram or narrative)
- Notable Patterns (Required: yes -- minimum 2)
- Drift Observations (Required: yes -- "None observed" is valid)
- What This Snapshot Doesn't Cover (Required: yes)

## completeness-criteria
Snapshot is complete if:
- All required sections are present and non-empty
- Each Key Abstraction maps to a real file path (no invented paths)
- At least one Mermaid diagram renders without syntax errors
- "What This Snapshot Doesn't Cover" is honest -- not empty unless entire codebase was read
- Git commit hash is included
