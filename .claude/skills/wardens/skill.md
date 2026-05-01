---
name: wardens
description: View, toggle, and configure wardens — the quality gates that review plans, code, and security
user_invocable: true
---

# /wardens

Manage the warden system. Wardens are specialized review agents that guard the codebase. Each can be enabled/disabled, have its triggers configured, and receive custom instructions.

## Config location

```
~/deus/.claude/wardens/config.json
```

If this file does not exist, create it from defaults before proceeding:

```bash
python3 -c "
import json, shutil
from pathlib import Path
example = Path('$HOME/deus/.claude/wardens/config.json.example')
target = Path('$HOME/deus/.claude/wardens/config.json')
if not target.exists() and example.exists():
    shutil.copy2(example, target)
    print('Created config.json from defaults')
elif not target.exists():
    print('ERROR: config.json.example not found')
else:
    print('config.json already exists')
"
```

## Warden registry

Each warden has a one-liner description. Use these exact descriptions in the display.

| Warden | Type | Description |
|--------|------|-------------|
| `plan-reviewer` | Validator (blocking) | Reviews plans against Deus-specific rules before source edits |
| `code-reviewer` | Validator (blocking) | Reviews code changes for quality and security before commits |
| `threat-modeler` | Validator (warning) | STRIDE/OWASP threat review for auth, data, and trust boundaries |
| `architecture-snapshot` | Generator | Generates architecture overview with Mermaid diagrams |
| `session-retrospective` | Generator | Cross-session pattern analysis and retrospective reports |
| `data-quality` | Validator (manual) | Reviews auto-memory files for retrieval quality |

## When invoked with no arguments or `show`

Read `~/deus/.claude/wardens/config.json` and display the status of all wardens:

```
Wardens

  plan-reviewer          [ON]   Reviews plans against Deus-specific rules before source edits
                                Triggers: Edit, Write, MultiEdit
  code-reviewer          [ON]   Reviews code changes for quality and security before commits
                                Triggers: Bash
  threat-modeler         [ON]   STRIDE/OWASP threat review for auth, data, and trust boundaries
                                Triggers: Edit, Write, MultiEdit
  architecture-snapshot  [ON]   Generates architecture overview with Mermaid diagrams
                                Triggers: manual
  session-retrospective  [ON]   Cross-session pattern analysis and retrospective reports
                                Triggers: auto (threshold: 20 sessions), manual
  data-quality           [ON]   Reviews auto-memory files for retrieval quality
                                Triggers: manual

Commands:
  /wardens enable <name>                   Enable a warden
  /wardens disable <name>                  Disable a warden
  /wardens triggers <name>                 View current triggers
  /wardens triggers <name> add <tool>      Add a trigger tool
  /wardens triggers <name> remove <tool>   Remove a trigger tool
  /wardens instructions <name>             View custom instructions
  /wardens instructions <name> set <text>  Set custom instructions
  /wardens instructions <name> clear       Clear custom instructions
  /wardens reset <name>                    Reset a warden to defaults
```

For wardens with `tools` arrays, show the tool names joined by `, ` on the Triggers line.
For `session-retrospective`, show `auto (threshold: N sessions), manual`.
For wardens with no `tools` key, show `manual`.
Show `[ON]` or `[OFF]` based on the `enabled` field.
If `custom_instructions` is not null, append `(custom instructions set)` to the description line.

## `enable <name>` / `disable <name>`

Toggle the `enabled` field for the named warden.

```bash
python3 -c "
import json, sys
from pathlib import Path
p = Path('$HOME/deus/.claude/wardens/config.json')
d = json.loads(p.read_text())
name = sys.argv[1]
if name not in d:
    print(f'Unknown warden: {name}')
    print(f'Available: {', '.join(d.keys())}')
    sys.exit(1)
d[name]['enabled'] = sys.argv[2] == 'true'
p.write_text(json.dumps(d, indent=2) + '\n')
print(f'{name}: {'enabled' if d[name]['enabled'] else 'disabled'}')
" "<name>" "true|false"
```

**Warning for blocking wardens:** When disabling `plan-reviewer` or `code-reviewer`, warn:
"Warning: Disabling <name> removes a safety gate. Source edits/commits will proceed without warden review until re-enabled."

Still allow the toggle — it is the user's choice.

## `triggers <name>`

Show the current trigger configuration for the named warden.

If called with `add <tool>` or `remove <tool>`, modify the `tools` array:

```bash
python3 -c "
import json, sys
from pathlib import Path
p = Path('$HOME/deus/.claude/wardens/config.json')
d = json.loads(p.read_text())
name, action, tool = sys.argv[1], sys.argv[2], sys.argv[3]
if name not in d:
    print(f'Unknown warden: {name}')
    sys.exit(1)
if 'tools' not in d[name]:
    print(f'{name} uses manual/auto triggers, not tool-based triggers.')
    sys.exit(1)
if action == 'add':
    if tool not in d[name]['tools']:
        d[name]['tools'].append(tool)
elif action == 'remove':
    if tool in d[name]['tools']:
        d[name]['tools'].remove(tool)
    else:
        print(f'{tool} not in triggers for {name}')
        sys.exit(1)
p.write_text(json.dumps(d, indent=2) + '\n')
print(f'{name} triggers: {', '.join(d[name]['tools'])}')
" "<name>" "<add|remove>" "<tool>"
```

For `session-retrospective`, the trigger is `auto_threshold` (integer). Allow setting it:
`/wardens triggers session-retrospective threshold <N>` — updates `auto_threshold` in config.

## `instructions <name>`

View or set custom instructions for a warden.

- No subcommand: show current custom_instructions (or "No custom instructions set").
- `set <text>`: set `custom_instructions` to the provided text.
- `clear`: set `custom_instructions` to null.

```bash
python3 -c "
import json, sys
from pathlib import Path
p = Path('$HOME/deus/.claude/wardens/config.json')
d = json.loads(p.read_text())
name = sys.argv[1]
action = sys.argv[2] if len(sys.argv) > 2 else 'show'
if name not in d:
    print(f'Unknown warden: {name}')
    sys.exit(1)
if action == 'show':
    ci = d[name].get('custom_instructions')
    print(ci if ci else 'No custom instructions set.')
elif action == 'clear':
    d[name]['custom_instructions'] = None
    p.write_text(json.dumps(d, indent=2) + '\n')
    print(f'Cleared custom instructions for {name}.')
elif action == 'set':
    text = ' '.join(sys.argv[3:])
    d[name]['custom_instructions'] = text
    p.write_text(json.dumps(d, indent=2) + '\n')
    print(f'Set custom instructions for {name}.')
" "<name>" "<action>" "<text...>"
```

When a warden has custom instructions set, include them in the prompt when invoking that warden. For example, when invoking plan-reviewer:
```
Agent(subagent_type="plan-reviewer", prompt="<plan>

Custom instructions from user config:
<custom_instructions text>")
```

## `reset <name>`

Reset a warden to its defaults. Read the defaults from `config.json.example`:

```bash
python3 -c "
import json, sys
from pathlib import Path
config = Path('$HOME/deus/.claude/wardens/config.json')
example = Path('$HOME/deus/.claude/wardens/config.json.example')
d = json.loads(config.read_text())
defaults = json.loads(example.read_text())
name = sys.argv[1]
if name not in defaults:
    print(f'Unknown warden: {name}')
    sys.exit(1)
d[name] = defaults[name]
config.write_text(json.dumps(d, indent=2) + '\n')
print(f'Reset {name} to defaults.')
" "<name>"
```

## Enforcing enabled state for manual wardens

For wardens without hook enforcement (architecture-snapshot, session-retrospective, data-quality), check the config before invoking. When about to run a manual warden, read `config.json` and check `enabled`. If `false`, say:
"<name> is currently disabled. Use `/wardens enable <name>` to re-enable it."
Do not invoke the warden agent.

## Validation

- Only accept warden names that exist in config.json. Reject unknown names with the list of valid names.
- `tools` array entries should be valid Claude Code tool names (Edit, Write, MultiEdit, Bash, Read, etc.).
- `auto_threshold` must be a positive integer.
- `custom_instructions` can be any text. Warn if it exceeds 1000 characters (but still save).
