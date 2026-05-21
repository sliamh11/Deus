---
name: add-linear
description: Add Linear project management MCP integration to Deus. Gives host-side Claude Code sessions read/write access to Linear issues, projects, cycles, and workflow states via @tacticlaunch/mcp-linear.
---

# Add Linear

This skill adds Linear project management tools to host-side Claude Code sessions via the `@tacticlaunch/mcp-linear` MCP server. Once installed, you can create/update issues, manage projects and cycles, search your backlog, and track workflow states from conversation.

## Phase 1: Pre-flight

### Check if already configured

```bash
grep -q '"linear"' ~/.claude/mcp.json 2>/dev/null && echo "ALREADY_CONFIGURED" || echo "NOT_CONFIGURED"
grep -q "LINEAR_API_TOKEN" ~/deus/.env 2>/dev/null && echo "TOKEN_SET" || echo "NO_TOKEN"
```

- If both configured and token set, skip to Phase 3 (Verify).
- If configured but no token, skip to Phase 2 step 1 only.
- Otherwise, continue to Phase 2.

## Phase 2: Install and Configure

### Step 1: Get API token

Tell the user:

> I need a Linear Personal API key. This is a one-time setup:
>
> 1. Open Linear → Settings → API → Personal API keys
> 2. Click "Create key", give it a label like "Deus"
> 3. Copy the token (format: `lin_api_...`) and paste it here

Once the user provides the token:

Add to `~/deus/.env`:
```bash
grep -q LINEAR_API_TOKEN ~/deus/.env || echo 'LINEAR_API_TOKEN=<pasted-value>' >> ~/deus/.env
```

Verify it's gitignored:
```bash
cd ~/deus && git check-ignore .env || echo "WARNING: .env is NOT gitignored"
```

### Step 2: Configure MCP

Read `~/.claude/mcp.json`, merge the new entry into the existing `mcpServers` object:

```json
"linear": {
  "command": "/bin/sh",
  "args": ["-c", "set -a && . $HOME/deus/.env && npx -y @tacticlaunch/mcp-linear"]
}
```

Do not overwrite existing servers.

### Step 3: Document

If `~/deus/.env.example` does not already have `LINEAR_API_TOKEN`, add it:

```
# === Integrations ===
# Linear project management MCP (host-side Claude Code).
# Generate at: Linear Settings → API → Personal API keys
LINEAR_API_TOKEN=
```

## Phase 3: Verify

Tell the user:

> Linear MCP is configured. **Restart Claude Code** (close and reopen the session) for the tools to load.
>
> After restart, test with: "List my Linear teams" — you should see your workspace teams returned via `mcp__linear__linear_getTeams`.
>
> Available tools include `linear_getIssues`, `linear_createIssue`, `linear_updateIssue`, `linear_searchIssues`, `linear_getCycles`, `linear_getProjects`, and 130+ more.

## Removal

1. Remove the `"linear"` key from `~/.claude/mcp.json`
2. Remove `LINEAR_API_TOKEN=...` from `~/deus/.env`
3. Restart Claude Code
