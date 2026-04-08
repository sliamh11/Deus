---
name: preferences
description: View or modify Deus user preferences (name, catch-me-up behavior, bypass permissions, persona)
user_invocable: true
---

# /preferences

Manage Deus user preferences. These are stored in `~/.config/deus/config.json` alongside the vault path.

## Config location

```bash
~/.config/deus/config.json
```

## Available preferences

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | string | `""` | User's name - Deus uses it in greetings and context |
| `catch_me_up` | boolean | `true` | Auto "Catch me up" greeting when launching in home mode |
| `bypass_permissions` | boolean | `true` | Try `--dangerously-skip-permissions` on launch (falls back to normal if declined) |
| `persona` | string | `""` | Free-text personality/behavior instructions appended to Deus identity |

## When invoked with no arguments or `show`

Read `~/.config/deus/config.json` and display current preferences:

```
Deus Preferences
  name:               <value or "(not set)">
  catch_me_up:        <on|off> (auto-greet with session summary on launch)
  bypass_permissions:  <on|off> (skip permission prompts on launch)
  persona:            <value or "(not set)">

Set with: /preferences <key> <value>
Examples:
  /preferences name "Alex"
  /preferences catch_me_up off
  /preferences persona "Always respond in a casual tone"
```

## When invoked with arguments

Parse the first argument as the key and the rest as the value.

- For boolean keys (`catch_me_up`, `bypass_permissions`): accept `on/off`, `true/false`, `yes/no`
- For string keys (`name`, `persona`): accept the remaining text as the value
- To clear a string value: `/preferences name ""` or `/preferences persona clear`

### Implementation

Use Python to update the JSON file in-place (preserves other keys like `vault_path`):

```bash
python3 -c "
import json, sys
from pathlib import Path
p = Path('~/.config/deus/config.json').expanduser()
d = json.loads(p.read_text()) if p.exists() else {}
key, val = sys.argv[1], sys.argv[2]
if key in ('catch_me_up', 'bypass_permissions'):
    d[key] = val.lower() in ('true', 'on', 'yes', '1')
else:
    d[key] = '' if val.lower() in ('clear', '\"\"', \"''\") else val
p.write_text(json.dumps(d, indent=2))
print(f'Set {key} = {d[key]}')
" "<key>" "<value>"
```

Confirm the change and note that it takes effect on next `deus` launch (not mid-session).

## Validation

- Only allow the 4 documented keys. Reject unknown keys with a helpful message.
- `persona` can be any text. Warn if it exceeds 500 characters (but still save it).
