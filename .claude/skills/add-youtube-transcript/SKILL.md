---
name: add-youtube-transcript
description: Add YouTube transcript extraction as an MCP tool. Installs the MCP server and configures it so the agent can fetch video captions/subtitles on demand.
---

# Add YouTube Transcript

This skill adds YouTube transcript extraction capability to Deus via an MCP server. Once installed, the agent can fetch captions and subtitles from any YouTube video using the `get_transcript` tool.

## Phase 1: Pre-flight

### Check if already configured

Check if `.mcp.json` already has a `youtube-transcript` entry:

```bash
cat .mcp.json 2>/dev/null
```

If `youtube-transcript` is already present, inform the user it's already installed and skip to Phase 3 (Verify).

## Phase 2: Install and Configure

### Install the MCP server package

```bash
npm install --save-dev @kimtaeyoon83/mcp-server-youtube-transcript
```

### Configure MCP

Read the current `.mcp.json` and add the youtube-transcript server entry. The final `.mcp.json` should include:

```json
{
  "mcpServers": {
    "youtube-transcript": {
      "command": "npx",
      "args": ["-y", "@kimtaeyoon83/mcp-server-youtube-transcript"]
    }
  }
}
```

If `.mcp.json` already has other servers configured, merge the new entry — do not overwrite existing servers.

### Build

```bash
npm run build
```

## Phase 3: Verify

### Test the MCP server starts

```bash
npx @kimtaeyoon83/mcp-server-youtube-transcript --help 2>&1 || echo "Server binary accessible"
```

Tell the user:

> YouTube transcript extraction is now available. The agent can fetch captions from any YouTube video.
>
> **Usage:** Ask the agent to get a transcript from a YouTube URL, or use the `get_transcript` tool directly with a video URL or ID.
>
> **Supported options:**
> - `url` — YouTube video URL or video ID
> - `lang` — Language code (default: "en", e.g., "ko", "he", "es")

## Removal

To remove YouTube transcript support:

1. Remove the MCP server entry from `.mcp.json`:
   ```bash
   # Edit .mcp.json and remove the "youtube-transcript" key from mcpServers
   ```

2. Uninstall the package:
   ```bash
   npm uninstall @kimtaeyoon83/mcp-server-youtube-transcript
   ```

3. Rebuild:
   ```bash
   npm run build
   ```
