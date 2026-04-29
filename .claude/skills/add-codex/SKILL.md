---
name: add-codex
description: Add OpenAI/Codex as a backend. Guides through API key setup, service backend configuration, optional CLI setup, and verification. Can run alongside Claude (default) or replace it.
---

# Add OpenAI/Codex Backend

This skill configures OpenAI/Codex as a backend for Deus. Two independent modes:

- **Service backend** (`DEUS_AGENT_BACKEND=openai`) — container agents use the OpenAI Responses API for background message handling
- **CLI sessions** (`deus codex`) — foreground interactive sessions using the `codex` CLI binary

You can enable one or both. The Claude backend remains the default until explicitly switched.

**IMPORTANT:** This skill writes environment variables to `.env` and syncs them to `data/env/env` at runtime. It does not modify source code or require a rebuild.

## Phase 1: Pre-flight

### Check if already configured

Check if OpenAI/Codex is already set up:

```bash
grep -q 'OPENAI_API_KEY=.' .env 2>/dev/null && echo "API key found" || echo "No API key"
deus backend
```

If both show valid config, skip to Phase 5 (Verify).

### Ask scope

AskUserQuestion: How do you want to use OpenAI/Codex?
1. **Service backend** — container agents use OpenAI for message handling (replaces Claude as default backend)
2. **CLI only** — use `deus codex` for interactive sessions (Claude stays as service backend)
3. **Both** — service backend + CLI sessions

> **Recovery:** If you're unsure, start with CLI only (option 2). You can switch the service backend later with `deus backend set codex`.

## Phase 2: API Key

### Check for existing key

```bash
grep 'OPENAI_API_KEY' .env 2>/dev/null
```

If a key is already set and valid, skip to Phase 3.

### Collect the API key

AskUserQuestion: Do you have an OpenAI API key? If not, create one at https://platform.openai.com/api-keys — you need a key with API access (not just ChatGPT Plus).

Wait for the user to provide their key.

### Write to .env

Append or update `OPENAI_API_KEY` in `.env`:

```bash
# Update or append OPENAI_API_KEY:
if grep -q '^OPENAI_API_KEY=' .env; then
  tmpf=$(mktemp) && sed 's/^OPENAI_API_KEY=.*/OPENAI_API_KEY=<their-key>/' .env > "$tmpf" && mv "$tmpf" .env
else
  echo 'OPENAI_API_KEY=<their-key>' >> .env
fi
```

### Sync to container environment

```bash
mkdir -p data/env && cp .env data/env/env
```

The container reads environment from `data/env/env`, not `.env` directly.

> **Recovery:** If you see `hasApiCredentials() failed` in logs after setup, the key wasn't synced. Re-run the `cp` command above.

## Phase 3: Backend Configuration

Skip this phase if the user chose CLI only (option 2) in Phase 1.

### Set as default backend

```bash
deus backend set codex
```

This writes `agent_backend: openai` to `~/.config/deus/config.json` and updates `.env`.

### Model selection

AskUserQuestion: Which model do you want to use? Common options:
- `gpt-4o` (default, recommended)
- `gpt-4o-mini` (faster, cheaper)
- `o3` (reasoning)
- Or any model available on your OpenAI account

```bash
deus backend model <their-choice>
```

### Parity warnings

**Surface these warnings to the user before proceeding:**

> **Backend parity notice:** OpenAI/Codex is functional but not yet at full parity with the Claude default. Current gaps:
>
> | Feature | Claude | OpenAI/Codex |
> |---------|--------|--------------|
> | Tool streaming | Live output | Batch response (no streaming) |
> | Session protocol | Claude Code SDK | OpenAI Responses API |
> | MCP tools | Native | Bridged via tool-broker |
> | Live container verification | Yes | Not yet |
> | Sessions | Not portable — a session started on one backend cannot be resumed on the other |
>
> These gaps are tracked and being closed. The system behavior (routing, tools, container environment) is identical regardless of backend — only the LLM interface differs.
>
> To switch back to Claude at any time: `deus backend set claude`

> **Recovery:** If messages stop being handled after switching backends, verify the key is valid and the model is available on your account. Revert with `deus backend set claude`.

## Phase 4: CLI Setup (Optional)

Skip this phase if the user chose service backend only (option 1) in Phase 1.

**The `codex` CLI binary is entirely separate from the service backend.** Users who only want `DEUS_AGENT_BACKEND=openai` do not need the `codex` binary installed. The service backend uses the OpenAI Responses API directly.

### Check if codex CLI is installed

```bash
command -v codex && codex --version || echo "codex CLI not found"
```

### Install if needed

If not installed, tell the user:

> The `codex` CLI is an OpenAI tool for interactive coding sessions. Install it:
>
> ```bash
> npm install -g @openai/codex
> ```
>
> After installation, verify: `codex --version`

### Configure CLI model (optional)

If the user wants a different model for CLI sessions than the service backend:

```bash
# Set CLI-specific model (overrides DEUS_OPENAI_MODEL for deus codex only)
# Update or append DEUS_CODEX_MODEL:
if grep -q '^DEUS_CODEX_MODEL=' .env; then
  tmpf=$(mktemp) && sed 's/^DEUS_CODEX_MODEL=.*/DEUS_CODEX_MODEL=<model>/' .env > "$tmpf" && mv "$tmpf" .env
else
  echo 'DEUS_CODEX_MODEL=<model>' >> .env
fi
```

Sync: `mkdir -p data/env && cp .env data/env/env`

### Test CLI

```bash
deus codex
```

This should launch an interactive Codex session. Exit with Ctrl+C.

> **Recovery:** If `deus codex` says "codex CLI not found", the binary isn't in PATH. Check `which codex` or reinstall.

### Register Deus memory MCP

For direct Codex CLI sessions outside the `deus` launcher, register the memory
MCP through the repo launcher. Do not point Codex at `python3` directly; the
launcher selects a Python environment that has the `mcp` package installed and
prints setup commands if dependencies are missing.

```bash
codex mcp add deus-memory -- "$(pwd)/scripts/deus-memory-mcp"
codex mcp get deus-memory
```

## Phase 5: Verify

### Restart the service

If the service backend was changed (options 1 or 3):

- macOS: `launchctl kickstart -k gui/$(id -u)/com.deus`
- Linux: `systemctl --user restart deus`
- Windows: `nssm restart deus` or `servy-cli restart --name=deus`

### Confirm configuration

```bash
deus backend
```

Expected output should show the selected backend and model.

### Check logs

```bash
tail -20 logs/deus.log
```

Look for:
- `Backend: openai` or similar initialization line
- No `hasApiCredentials() failed` errors
- No model-not-found errors

### Test message (service backend only)

If the service backend was set to OpenAI, send a test message via a registered channel (WhatsApp, Telegram, etc.) and verify the agent responds.

## Troubleshooting

### `hasApiCredentials() failed` at startup

The `OPENAI_API_KEY` is missing or not synced to the container:

```bash
grep OPENAI_API_KEY .env          # Check .env
grep OPENAI_API_KEY data/env/env  # Check container env
```

If missing from `data/env/env`: `mkdir -p data/env && cp .env data/env/env`

### `codex` CLI not found

The `codex` binary is not installed or not in PATH:

```bash
npm install -g @openai/codex
# Or check if it's installed elsewhere:
npm list -g @openai/codex
```

### Model not available

OpenAI returned a model-not-found error. Check available models on your account:

```bash
curl -s -H "Authorization: Bearer $(grep OPENAI_API_KEY .env | cut -d= -f2)" \
  https://api.openai.com/v1/models | jq '.data[].id' | grep -i gpt
```

Set a valid model: `deus backend model <valid-model>`

### Revert to Claude

```bash
deus backend set claude
launchctl kickstart -k gui/$(id -u)/com.deus  # macOS
# systemctl --user restart deus                # Linux
# nssm restart deus                            # Windows
```

## Removal

To fully remove the OpenAI/Codex backend:

1. Switch back to Claude: `deus backend set claude`
2. Remove or clear the API key from `.env`: set `OPENAI_API_KEY=`
3. Clear CLI-specific vars if set: `DEUS_CODEX_MODEL=`, `DEUS_CLI_AGENT=`
4. Sync: `mkdir -p data/env && cp .env data/env/env`
5. Restart the service
6. Optionally uninstall the CLI: `npm uninstall -g @openai/codex`
