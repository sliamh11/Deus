# ADR: Agent-Native CLI and MCP Protocol (Printing-Press Adoption)

**Status:** Accepted
**Date:** 2026-05-16
**Scope:** `scripts/_exit_codes.py`, `scripts/_agent_io.py`, `scripts/memory_tree.py`, `scripts/memory_indexer.py`, `packages/mcp-channel-core/src/response.ts`, `packages/mcp-gcal/`, `packages/mcp-gmail/`, `packages/mcp-x/` (follow-on PR)
**Related:** [error-discipline.md](error-discipline.md) (TS error taxonomy), [eval-ipc-file-output.md](eval-ipc-file-output.md) (file-based IPC)

## Context

Subagents calling Deus Python CLIs receive human-readable text output and must regex-parse it. MCP tools return pretty-printed JSON with no field filtering or compact mode. Neither surface has typed exit codes for programmatic callers.

The [cli-printing-press](https://github.com/mvanhorn/cli-printing-press) project demonstrated an agent-native protocol: typed exit codes (0/2/3/4/5/7), auto-JSON when piped, `--compact` for 60-80% token reduction, and `--select` for field filtering. These patterns are language-agnostic and portable.

## Decision

### Phase 1: Agent-Native Protocol (this PR series)

**Python CLIs** (`memory_tree.py`, `memory_indexer.py`):
- Typed exit codes via `scripts/_exit_codes.py`: SUCCESS(0), ABSTAIN(1), USAGE_ERROR(2), NOT_FOUND(3), AUTH_ERROR(4), INTERNAL_ERROR(5)
- `--json` flag for structured output; auto-JSON via `DEUS_AGENT_NATIVE=1` env var
- `--compact` strips None values, truncates long fields for token efficiency
- `--select` applies field projection (comma-separated dot-paths)
- Helper functions in `scripts/_agent_io.py`

**MCP servers** (`mcp-channel-core`, `mcp-gcal`, `mcp-gmail`, `mcp-x`):
- `mcpResponse(data, {compact?, select?, truncateAt?})` replaces inline `JSON.stringify`
- `mcpError(code, message, resource?)` with typed `McpErrorCode` enum
- All tools accept optional `compact` and `select` input parameters
- Error codes surfaced in JSON payload: `{ error_code: N, message, resource? }`

**Exit code to TS error class mapping** (extends `error-discipline.md`):

| Python Exit Code | Value | TS Error Class | Caller Action |
|------------------|-------|----------------|---------------|
| SUCCESS | 0 | (success) | Proceed |
| ABSTAIN | 1 | (CLI-only) | Skip / fallback |
| USAGE_ERROR | 2 | UserError | Surface to user |
| NOT_FOUND | 3 | UserError | Surface to user |
| AUTH_ERROR | 4 | FatalError | Log + stop |
| INTERNAL_ERROR | 5 | FatalError | Log + stop |

### Phase 2: Host-Side Tool Proxy (future)

Extend `credential-proxy.ts` pattern to proxy CLI binary execution. Containers call `POST /tool/{cli-name}` with args; the host spawns the binary, injects credentials, returns `{ exit, stdout, stderr }`. Unlocks pre-built CLIs from the printing-press-library (ESPN, flights, etc.) without putting Go binaries in containers.

**Trigger:** When a concrete need arises for a printing-press library CLI.

### Phase 3: Shared SQLite-FTS5 Cache Layer (future)

Host-side sync daemon writes API data into per-service SQLite files with FTS5 indexes. Mounted read-only into containers via `container-mounter.ts`. Enables offline search, sub-millisecond compound queries, and incremental sync with cursor tracking.

**Trigger:** When MCP call volume causes measurable latency or rate-limit pressure.

### Phase 4: Codegen (future)

Use the printing-press as a scaffold generator for new MCP server integrations. Generate Zod schemas and TypeScript stubs from API specs.

**Trigger:** When adding a new API integration that has an OpenAPI spec.

## Constraints

- `DEUS_AGENT_NATIVE=1` env var gates auto-JSON. TTY auto-detection deferred because `memory_benchmark.py` parses human-readable stdout from piped subprocesses. When TTY detection is added, guard `os.isatty()` with try/except for OSError on platforms where fileno() is unavailable.
- Module-scope `sys.exit()` calls in `memory_indexer.py` (vault config, API key check) cannot become return values. They stay as `sys.exit(AUTH_ERROR)`.
- `McpErrorCode` enum values are stable and match the Python exit codes. They are part of the protocol contract.
- Per `evolution-db-split.md`: any future SQLite cache files get their own DB per service, never shared with memory.db or evolution.db.

## Empirical findings (2026-05-16)

Measured during the `--select` orchestrator-leverage PR (`feat/pp-select-orchestrator`). Two findings reshaped the rollout plan:

### CLI text formats are already token-optimal for LLM context injection

The hot paths `deus-cmd.sh` invokes on every container session are already minimal in their human-readable text form. Adding `--json [--select ...]` would *increase* payload bytes:

| Path                                                                                    | Bytes |
|-----------------------------------------------------------------------------------------|-------|
| `memory_indexer --recent 3` (text)                                                      | 1517  |
| `memory_indexer --recent 3 --compact` (text)                                            |  890  |
| `memory_indexer --query "recent work" --top 2 --recency-boost` (text)                   |  728  |
| `memory_indexer --query "recent work" --top 2 --recency-boost --json` (no select)       | 4210  |
| `memory_indexer --query "recent work" --top 2 --recency-boost --json --select <fields>` |  944  |
| `memory_tree query "recent work"` (text)                                                |  376  |
| `memory_tree query "recent work" --json --compact --select results.path,results.score`  |  420  |

Existing text formatters were hand-tuned for this use case. JSON adds structural overhead; even projected JSON loses to prose for compact-list shapes.

### `--select` wins on the MCP wire format

MCP responses MUST be JSON per protocol. Wide structured records (calendar events, email threads) carry many fields the caller does not need. Measured against synthetic-but-representative fixtures via `packages/mcp-channel-core/bench/pp-response-bench.ts`:

| Fixture                  | raw   | compact only | select only | compact + select |
|--------------------------|-------|--------------|-------------|------------------|
| gcal single event        |   556 |          556 |         164 |              164 |
| gcal list (10 events)    |  5602 |         5602 |        1152 |             1152 |
| gmail single message     |  2883 |          686 |         287 |              287 |
| gmail list (10 messages) | 28841 |         6871 |        2541 |             2541 |

`--select` cuts MCP list payloads by 79–91% versus raw. `--compact` helps independently when records have long strings or nulls (gmail), but is a no-op for clean structured records (gcal). Once `select` has projected to a small field set on a clean record, `compact` is also redundant — the bench shows identical bytes for "select only" and "compact + select" on gcal.

### Activation strategy

1. **CLI paths (deus-cmd.sh, skills, commands)**: keep text format. Do not switch to `--json [--select]` — it regresses.
2. **MCP tools**: teach the LLM caller via enriched tool-description strings ("Pass `select="..."` + `compact=true` ..."). The agent reads descriptions when deciding tool args; the savings compound across every list-style call.
3. **Future programmatic callers** of CLI tools (e.g., scripts ingesting `memory_indexer --query --json`): use `--select` from day one. `cmd_recent` and `cmd_learnings` would need extension to accept `--select` + emit JSON — deferred until a programmatic caller exists.

### Drift enforcement

Both `check_agent_native_mcp` and `check_mcp_description_hints` in `scripts/drift_check.py` are blocking: any `server.tool()` registration that lacks `compact`/`select` schema params (without a recognized action-marker) — or that has the schema params but no `select`/`compact`/`payload` hint in its description — fails CI. Both gates started informational and were activated after PR #448 cleared the baseline across mcp-channel-core, mcp-gcal, mcp-gmail, mcp-x, and mcp-whatsapp. See `git log --follow scripts/drift_check.py` for the activation commit.
