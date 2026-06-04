---
name: add-asana
description: Add Asana project management MCP integration to Deus. Gives host-side Claude Code sessions read/write access to Asana tasks, projects, sections, and tags via @roychri/mcp-server-asana.
---

# Add Asana

This skill adds Asana project management tools to host-side Claude Code sessions via the `@roychri/mcp-server-asana` MCP server. Once installed, you can create/update tasks, manage projects and sections, search your workspace, add comments, and track task fields from conversation.

This mirrors the `/add-linear` pattern: a local **stdio** server launched with `npx`, authenticated by an Asana **Personal Access Token (PAT)** sourced from `~/deus/.env`. The token never enters containers and is never copied into Claude Code's config.

> Why not Asana's official MCP server? Asana's hosted MCP (`https://mcp.asana.com/v2/mcp`) is **OAuth-only** and Claude Code has no headless OAuth path (it requires browser consent and re-auth on token expiry), which breaks unattended host processes. The PAT-based community server is the right fit here.

## Phase 1: Pre-flight

### Check if already configured

```bash
grep -q '"asana"' ~/.claude.json 2>/dev/null && echo "ALREADY_CONFIGURED" || echo "NOT_CONFIGURED"
grep -q "ASANA_ACCESS_TOKEN" ~/deus/.env 2>/dev/null && echo "TOKEN_SET" || echo "NO_TOKEN"
grep -q "READ_ONLY_MODE" ~/deus/.env 2>/dev/null && echo "READ_ONLY_ACTIVE" || echo "READ_WRITE"
```

- If both configured and token set, skip to Phase 3 (Verify).
- If configured but no token, skip to Phase 2 step 1 only.
- Otherwise, continue to Phase 2.

## Phase 2: Install and Configure

### Step 1: Get a Personal Access Token

Tell the user:

> I need an Asana Personal Access Token. This is a one-time setup:
>
> 1. Open Asana → click your profile photo (top right) → **My Settings**
> 2. Go to the **Apps** tab → **Manage Developer Apps**
> 3. Under **Personal Access Tokens**, click **Create new token**, give it a label like "Deus"
> 4. Copy the token and paste it here
>
> (Direct link: https://app.asana.com/0/my-apps)

Once the user provides the token:

Add to `~/deus/.env`:
```bash
grep -q ASANA_ACCESS_TOKEN ~/deus/.env || echo 'ASANA_ACCESS_TOKEN=<pasted-value>' >> ~/deus/.env
```

Verify it's gitignored:
```bash
cd ~/deus && git check-ignore .env || echo "WARNING: .env is NOT gitignored"
```

### Step 2: Configure MCP

Use the `claude mcp add` CLI — it merges into Claude Code's user-scope config (`~/.claude.json`) safely, without you hand-editing JSON:

```bash
claude mcp add asana --scope user -- /bin/sh -c 'set -a && . $HOME/deus/.env && npx -y @roychri/mcp-server-asana'
```

The single quotes are important: they keep `$HOME` literal so it is expanded by `/bin/sh` at launch time (sourcing the token fresh from `.env`), not baked into the config. Confirm it was stored correctly:

```bash
grep -A5 '"asana"' ~/.claude.json
```

The args should show `$HOME/deus/.env` literally — **not** an expanded `/Users/...` path. If it was expanded, use the manual fallback below instead.

**Manual fallback** — if `claude mcp add` is unavailable or expanded `$HOME`, read `~/.claude.json` and merge this entry into the existing `mcpServers` object (do not overwrite existing servers):

```json
"asana": {
  "command": "/bin/sh",
  "args": ["-c", "set -a && . $HOME/deus/.env && npx -y @roychri/mcp-server-asana"]
}
```

### Step 3: Document

If `~/deus/.env.example` does not already have `ASANA_ACCESS_TOKEN`, add it:

```
# Asana project management MCP (host-side Claude Code).
# Generate at: Asana → My Settings → Apps → Manage Developer Apps → Personal Access Token
ASANA_ACCESS_TOKEN=
```

### Optional: read-only mode

To let the agent read Asana but block all writes, add `READ_ONLY_MODE=true` to `~/deus/.env`. The server then exposes only the read/search tools.

## Phase 3: Verify

Tell the user:

> Asana MCP is configured. **Restart Claude Code** (close and reopen the session) for the tools to load.
>
> After restart, test with: "List my Asana workspaces" — you should see your workspaces returned via `mcp__asana__asana_list_workspaces`.
>
> Available tools include `asana_search_tasks`, `asana_get_task`, `asana_create_task`, `asana_update_task`, `asana_create_subtask`, `asana_search_projects`, `asana_create_project`, `asana_get_project_sections`, `asana_add_task_to_section`, `asana_create_task_story` (comments), and 30+ more.

## Notes

- **Token scope**: an Asana PAT carries **full access to the user's account** (Asana PATs are not granularly scoped). It lives only in `~/deus/.env` (gitignored) and is never mounted into containers. Use `READ_ONLY_MODE=true` if you want to limit blast radius.
- **Alternative server**: `@cristip73/mcp-server-asana` is a maintained fork that adds attachment upload/download, team, and project-hierarchy tools (same PAT + stdio + npx model). Swap the package name in Step 2 if you need those.

## Removal

1. Remove the server: `claude mcp remove asana --scope user` (or delete the `"asana"` key from `~/.claude.json`).
2. Remove `ASANA_ACCESS_TOKEN=...` (and any `READ_ONLY_MODE`) from `~/deus/.env`.
3. Restart Claude Code.
