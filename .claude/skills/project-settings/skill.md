---
name: project-settings
description: View or modify Deus external project settings (memory level, session summaries, description)
user_invocable: true
---

# /project-settings

Manage Deus data handling settings for the current external project.

## Config location

Project configs are stored at `~/.config/deus/projects/<hash>.json`. To find the config for the current directory, compute the MD5 hash of the absolute path of the current working directory and look for that file.

```bash
# macOS
dir_hash=$(echo -n "$(pwd)" | md5 -q)
# Linux
dir_hash=$(echo -n "$(pwd)" | md5sum | cut -d' ' -f1)
config_file="$HOME/.config/deus/projects/${dir_hash}.json"
```

## When invoked with no arguments or `show`

Read the config file and display current settings. Also detect the project type by scanning for marker files (Cargo.toml → rust, go.mod → go, package.json → node/typescript, pyproject.toml/requirements.txt → python, Gemfile → ruby, pom.xml → java).

Display in this format:

```
Project: <name> (<path>)
Description: <description, or "(none — set with /project-settings description <text>)">
Type: <detected type, e.g. "typescript / next.js" or "python / fastapi", or "unknown">
Memory level: <full|standard|restricted>
  full       — Remember everything. Sessions saved to vault with full details.
  standard   — Remember decisions & architecture, redact code details.
  restricted — Nothing persists. Each session starts fresh.
Session summaries: <on|off>
Created: <date>
Last accessed: <date>
```

Then show available commands:
- `/project-settings memory full|standard|restricted` — change memory level
- `/project-settings summaries on|off` — toggle session summaries
- `/project-settings description <text>` — set a short project description Deus uses as context
- `/project-settings delete` — delete all Deus data for this project

## When invoked with arguments

Parse the argument and update the config JSON file accordingly using Python to read/write the JSON safely. Always preserve all existing fields when updating.

### `memory full|standard|restricted`

Update the `memory_level` field. If changing to `restricted`, also set `save_summaries` to false and inform the user.

Memory level descriptions:
- **full**: Remember everything. Claude auto-memory enabled. Session summaries saved to vault with full code details.
- **standard**: Remember decisions and architecture, skip code details. Auto-memory enabled with guidance. Summaries saved but code-redacted.
- **restricted**: Nothing persists. Auto-memory disabled. No summaries. Best for NDA/client work.

### `summaries on|off`

Update the `save_summaries` field. If memory level is `restricted` and user tries to enable summaries, warn that restricted mode doesn't support summaries and don't make the change.

### `description <text>`

Update the `description` field in the config JSON. This text is used by Deus as project context in future sessions. Keep it concise (1–2 sentences). Example: `/project-settings description "E-commerce backend for ACME Corp — Django REST API with PostgreSQL"`

Use Python to update the field:
```python
import json, sys
with open(sys.argv[1], 'r+') as f:
    d = json.load(f)
    d['description'] = sys.argv[2]
    f.seek(0); json.dump(d, f, indent=2); f.truncate()
```

### `delete`

First ask for explicit confirmation: "This will delete all Deus tracking data for this project (memory level, summary settings, description). Claude Code's own session data at ~/.claude/projects/ is NOT affected — that's managed by Claude Code itself. Type 'yes' to confirm."

Only proceed if the user responds with 'yes' (case-insensitive). Then delete the config file.

## Important

- The config file uses the MD5 hash of the current working directory's absolute path as filename
- Always use `umask 077` when writing config files (they may contain path information)
- Use Python to update JSON fields — never rewrite the whole file from scratch (would reset created_at)
- After modifying settings, confirm the change and remind the user the new settings take effect on the next session start
