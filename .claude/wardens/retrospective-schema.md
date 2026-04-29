# Retrospective Schema -- Wardens/session-retrospective

> Output schema and configuration for the session-retrospective generator warden.
> vault_path is resolved at runtime. All fields below are optional overrides.

## vault_path (runtime-resolved)

The root of the Deus vault. Session logs are expected at `<vault_path>/Session-Logs/`.
Retrospectives are saved to `<vault_path>/Retrospectives/`.

vault_path is resolved at runtime, NOT hardcoded here. Resolution chain (highest priority first):
1. Invocation-arg `SESSION_LOG_ROOT=<path>` in the prompt (used by /compress auto-trigger)
2. `$SESSION_LOG_ROOT` environment variable
3. `~/.config/deus/config.json` → `vault_path` key (or `DEUS_VAULT_PATH` env var)
4. `$REPO_ROOT/Session-Logs/` if it exists
5. Fail loud

## session_window

Number of most-recently-modified session log files to include.
Use file count, not day count -- a single active day may produce 15+ files.

```
session_window: 20
```

Override per-invocation: include `window=N` in the prompt.

## project_filter

Basename used to match `project_path:` frontmatter in session logs.
Sessions whose `project_path:` contains this string are on-project; others are
counted but de-weighted. Leave blank to include all sessions equally.

```
project_filter: deus
```

## save_location

Dated files enable trend tracking and recommendation follow-up via unique IDs.

```
save_path: <vault_path>/Retrospectives/YYYY-MM-DD-retrospective.md
```

## reading_hints

- `Session-Logs/` contains both flat files and subdirectory-per-day layouts.
- Files named `*compact-only*` are near-empty maintenance sessions -- include in
  window count but they rarely carry pattern signal.
- MEMORY.md lives at `$HOME/.claude/projects/*<repo-name>*/memory/MEMORY.md`.
  The agent discovers it automatically.
- Only read CRITICAL-tagged feedback entries for behavioral drift (not all 80+).

## required_sections

- Recurring Themes (Required: yes)
- Root Cause Hypotheses (Required: yes -- "Insufficient data" is valid per theme)
- Deferred Tasks Ledger (Required: yes -- "None" is valid)
- Decision Reversals (Required: yes -- "None observed" is valid)
- Behavioral Drift (Required: yes -- "Memory index not found" is valid)
- Trend vs Prior Retrospective (Required: yes -- "First run" is valid)
- Prior Recommendation Follow-up (Required: when prior retrospective exists)
- Recommendations (Required: yes -- minimum 3, maximum 7)
- Scope (Required: yes)

## completeness_criteria

Retrospective is complete if:
- All required sections are present and non-empty
- Every Recurring Theme cites at least one session log by filename
- Every Root Cause Hypothesis states a confidence level with evidence basis
- Every Recommendation has all 5 fields: action, confidence, evidence basis,
  testable prediction, and why-it-matters
- Every Recommendation has a unique RETRO-YYYY-MM-DD-NN ID
- Deferred Tasks include first-seen date and times-deferred count
- Scope section lists what was NOT read
- Dated file is saved to Retrospectives/
