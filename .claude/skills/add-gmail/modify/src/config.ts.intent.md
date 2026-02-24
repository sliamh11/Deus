# Intent: src/config.ts modifications

## What changed
Added configuration exports for Gmail channel support.

## Key sections
- **readEnvFile call**: Must include `GMAIL_CHANNEL_ENABLED` in the keys array. NanoClaw does NOT load `.env` into `process.env` — all `.env` values must be explicitly requested via `readEnvFile()`.
- **GMAIL_CHANNEL_ENABLED**: Boolean feature flag — when `true`, the Gmail channel is connected and polls for inbound emails. When `false` (default), Gmail is available as a tool only (agent can read/send emails when asked from other channels).

## Invariants
- All existing config exports remain unchanged
- New Gmail keys are added to the `readEnvFile` call alongside existing keys
- New exports are appended at the end of the file
- No existing behavior is modified — Gmail config is additive and minimal
- Both `process.env` and `envConfig` are checked (same pattern as `ASSISTANT_NAME`)

## Must-keep
- All existing exports (`ASSISTANT_NAME`, `POLL_INTERVAL`, `TRIGGER_PATTERN`, etc.)
- The `readEnvFile` pattern — ALL config read from `.env` must go through this function
- The `escapeRegex` helper and `TRIGGER_PATTERN` construction
