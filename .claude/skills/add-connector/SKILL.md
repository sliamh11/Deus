---
name: add-connector
description: Onboard a `deus connect` connector — a way to route Claude Code sessions to non-Claude models (e.g. GPT via CLIProxyAPI) alongside normal Claude access. Generic orchestrator over whichever connector is selected.
disable-model-invocation: true
---

# Add a `deus connect` Connector

`deus connect` launches the real, unmodified `claude` binary with which
upstream model answers its requests redirected via `ANTHROPIC_BASE_URL` —
identity, vault/memory context, preferences, and portable skills stay
intact, exactly like a normal `deus`/`deus home` session. This is separate
from Deus's own container-agent backend adapters (`deus codex`,
`DEUS_AGENT_BACKEND`) — see
`docs/decisions/backend-neutral-agent-runtime.md`'s note near the Parity
Matrix.

This skill is generic over any registered connector (currently ships with
`cliproxy-oauth` only) — adding a second connector in the future changes
`connectors/providers/`, not this skill.

## Phase 1: Pre-flight

```bash
python3 scripts/connectors_cli.py list
```

Shows every registered connector and whether it's already configured. If
the target connector shows `configured` (e.g. a repeat run, or a machine
inherited from another user), still walk through Phase 2 (select) and
Phase 3 (risk acknowledgment) — Phase 3 is required every time, regardless
of configuration state, per its own rule; skipping it here would let an
already-configured install proceed straight to a real connector launch
without ever showing the five risks. Only Phases 4-9 (install/authenticate/
write-config) are skippable on an already-configured connector — jump from
Phase 3 straight to Phase 10 (Verify) unless the user explicitly wants to
reconfigure.

## Phase 2: Which connector

AskUserQuestion: Which connector do you want to set up? (ships with
`cliproxy-oauth` only today — CLIProxyAPI, OAuth-login, reuses your
ChatGPT/Codex subscription for GPT models alongside Claude).

## Phase 3: Required risk acknowledgment

**Not a footnote — block progression on explicit confirmation.** Show all
five of these before proceeding, every time, for every future user:

> **`deus connect` risk disclosure:**
>
> 1. **OAuth-subscription reuse is a real, non-zero, documented account-ban
>    risk class.** This connector extracts an OAuth token from your
>    ChatGPT/Codex subscription and reuses it for a separate HTTP client —
>    see `examples/multi-model-cliproxyapi/README.md`'s "Known open risks"
>    for the full account, including real dated ban reports in CLIProxyAPI's
>    own issue tracker for other providers. Unlike a config mistake, there
>    is no rollback if this fires.
> 2. **Anthropic explicitly disclaims support** for routing Claude Code to
>    non-Claude models through any gateway
>    (https://code.claude.com/docs/en/llm-gateway) — a support-scope
>    statement independent of whether this is configured correctly.
> 3. **Connector sessions are discoverable via a bare `claude`'s resume
>    picker.** `--name` tags the session (`connect:<id> (non-Claude)`) so
>    it's identifiable, not invisible or inaccessible — native Claude Code
>    session-resume is not isolated between a connector session and a bare
>    one.
> 4. **A nested `claude`/`deus claude` launched from inside a connector
>    session inherits that session's model redirection.** Once
>    `ANTHROPIC_*` env vars are set for the launched session, any subprocess
>    it spawns (e.g. a `Bash` tool call running `claude` again) inherits
>    them — a plain Unix process-inheritance property, no mechanism exists
>    to prevent it. Narrow in practice (requires deliberately launching
>    another interactive session mid-connector-session).
> 5. **A connector session gets the same no-prompt tool execution as a
>    trusted Claude session, driven by a model outside Anthropic's support
>    scope.** `deus connect` launches through the same `launch_claude`
>    every other session uses; if `bypass_permissions` is `true` in your
>    Deus preferences (the default), a connector session runs with
>    `--dangerously-skip-permissions` like any other Deus session — full,
>    unprompted tool execution, just with a non-Claude, unsupported model
>    driving it instead of Claude. The three inline subagents are scoped to
>    `Read`/`Grep`/`Glob`/`Bash`/`WebSearch`/`WebFetch` (not the session's
>    full tool set), but the top-level connector session itself is not
>    additionally restricted beyond your normal Deus bypass setting.

AskUserQuestion: Confirm you understand and accept all five risks above
before continuing?
- Yes, continue
- No, cancel setup

Stop here if the user declines.

## Phase 4: Install the engine

```bash
python3 scripts/connectors_cli.py install-check cliproxy-oauth
```

If `not installed`, tell the user:

> The `cli-proxy-api` binary isn't on your PATH. Build or download it from
> https://github.com/router-for-me/CLIProxyAPI (see the upstream repo's
> releases/instructions).
>
> After installation, verify: `cli-proxy-api --version`

**Require explicit confirmation before proceeding once installed — never
silently auto-fetch a binary that's about to hold real OAuth tokens.**

Re-run the install-check after the user confirms installation is done.

## Phase 5: Collect config values

Ask the user (do not invent values):

- **Inbound key** — generate one rather than asking the user to invent it:
  `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`. This is
  what Claude Code authenticates to the local proxy with.
- **Route Opus/Claude through the same proxy too?** Optional. If yes,
  collect a real, official Anthropic API key (not OAuth) for the
  `claude-api-key` leg.
- **Confirm the real upstream Codex/GPT model id strings.** The tracked
  placeholder (`connectors/cliproxy/config.yaml`) ships with illustrative
  names (`gpt-5.6-sol`/`gpt-5.6-terra`/`gpt-5.6-luna`) — **not** confirmed
  real IDs for the user's account/plan. These get hand-edited into
  `oauth-model-alias.codex[].name` in the written config (Phase 7) — the
  `alias` values (`sol`/`terra`/`luna-max`) stay fixed; only the `name`
  values (real upstream ids) vary per account.
- **Default model** for the session itself when no `/model` switch has been
  made — recommend `luna-max` (max reasoning effort) unless the user
  prefers otherwise.

## Phase 6: Authenticate

```bash
python3 scripts/connectors_cli.py authenticate cliproxy-oauth
```

Runs `cli-proxy-api --codex-login --config <local config>` — opens a
browser for OAuth. Running headless/remote instead of a local desktop? The
underlying engine supports `--no-browser` with a printed URL; if that's
needed, tell the user to run the login step manually with that flag instead
of through this script, then continue to Phase 7.

## Phase 7: Write config + launchd daemon

```bash
echo '<json values>' | python3 scripts/connectors_cli.py write-config cliproxy-oauth
```

Where `<json values>` is a JSON object:
```json
{
  "inbound_key": "<generated key from Phase 5>",
  "anthropic_api_key": "<optional, only if the user opted in>",
  "model_map": {"deus-gpt-sol": "sol", "deus-gpt-terra": "terra", "deus-gpt-luna": "luna-max"},
  "default_model_alias": "luna-max",
  "binary_path": "<absolute path from: command -v cli-proxy-api>"
}
```

This writes the real config to
`~/.config/deus/connectors/cliproxy/config.local.yaml` — **outside any
project root a container agent could ever have mounted** (confirmed
against `src/project-registry.ts:155-174` +
`src/container-mounter.ts:76-102`: the tracked placeholder at
`connectors/cliproxy/config.yaml` stays in-repo and safe to commit; the
real values never do) — and the launchd plist
(`~/Library/LaunchAgents/com.deus.connectors.cliproxy-oauth.plist`,
`RunAtLoad`+`KeepAlive`, macOS-only for now) with a fully home-expanded
absolute `--config` path (launchd execs directly with literal argv
strings, never shell-expanding `~`).

**If `write-config` errors with "A launchd job already exists at ... with
different settings"**: something else — possibly the user's own,
unrelated launchd job — already occupies that exact path. This is a
refusal-to-overwrite safety check (`_write_launchd_plist`), not a bug.
Show the user the error's own `ProgramArguments` detail and ask them to
move or remove that file themselves before retrying — never delete or
overwrite it on their behalf.

**Then hand-edit the real upstream model id strings** collected in Phase 5
into `~/.config/deus/connectors/cliproxy/config.local.yaml`'s
`oauth-model-alias.codex[].name` fields (the `write-config` call above does
not touch these — only `deus-model-map`, `api-keys`, `claude-api-key`, and
`default-model-alias`).

Load the daemon:

```bash
launchctl load ~/Library/LaunchAgents/com.deus.connectors.cliproxy-oauth.plist
launchctl kickstart -k gui/$(id -u)/com.deus.connectors.cliproxy-oauth
```

## Phase 8: `~/.claude.json` pre-approval

Read-merge the connector's inbound key into
`~/.claude.json`'s `customApiKeyResponses.approved` array — back up the
file first (`cp ~/.claude.json ~/.claude.json.bak-<date>`), then merge
(never overwrite the whole array — other entries may already exist).

## Phase 9: Validate subagent definitions

```bash
python3 scripts/connectors_cli.py agents-json cliproxy-oauth
```

Confirm it prints valid, non-empty JSON — no file templates to install,
`deus connect`'s launch mechanism passes these inline via `claude --agents`.

## Phase 10: Verify

```bash
python3 scripts/connectors_cli.py verify-setup cliproxy-oauth
```

Engine health + a real functional probe. Then run one real launch and
confirm, end to end:

```bash
deus connect cliproxy-oauth
```

- Model resolution: check `/model` shows the redirected model.
- Inline subagents resolve: dispatch the `Agent` tool with
  `deus-gpt-sol`/`deus-gpt-terra`/`deus-gpt-luna`.
- Normal Deus context is present: ask it to recall a recent checkpoint —
  the round 11/12 fix's whole point was that a connector session gets the
  same identity/vault/memory/preferences pipeline as a bare `deus` launch,
  not a redirected-but-otherwise-bare Claude Code process.
- A bare `claude` in a separate terminal never sees `deus-gpt-*` — confirms
  subagent isolation (the one fully structural guarantee this feature
  makes; env-var inheritance by nested processes and session-resume
  visibility are both disclosed tradeoffs, not guarantees — see Phase 3).

## Phase 11: Set as default (optional)

By default, every future session still needs `deus connect <id>` typed
explicitly — a bare `deus`/`deus home` is never affected. If you want this
connector to be your default everywhere instead (every bare `deus`/`deus
home` session, in any project, routes through it automatically), ask:

AskUserQuestion: Set `<id>` as your default connector for every session?
- Yes — run `deus connect default <id>` interactively and walk through its
  own confirmation prompt (a materially bigger blast radius than one-off
  `deus connect <id>` use, so it carries its own disclosure, not just
  Phase 3's).
- No — skip; `deus connect <id>` remains available on demand.

If yes, `deus connect default <id>` requires an interactive terminal (fails
closed, not silently, when piped/non-interactive) and prints its own
five-point-adjacent disclosure before writing anything — most importantly:
`deus claude` (typed explicitly) is the only guaranteed way to force plain
Claude once a default is set; a persistent `deus backend set claude` choice
or `DEUS_CLI_AGENT`/`DEUS_AGENT_BACKEND` env vars are NOT enough on their
own to block it. Reversible any time with `deus connect default off`
(`clear` is an accepted synonym).

## Troubleshooting

### `deus connect cliproxy-oauth` says "connector is unknown or not configured"

Config didn't write correctly, or the daemon isn't running:

```bash
python3 scripts/connectors_cli.py status cliproxy-oauth
launchctl list | grep com.deus.connectors.cliproxy-oauth
```

### Model doesn't resolve / 401s from the proxy

```bash
curl -s http://localhost:8317/healthz
cat ~/.config/deus/connectors/cliproxy/config.local.yaml   # confirm api-keys, oauth-model-alias
```

### Subagents don't appear via the `Agent` tool

Confirm you launched through `deus connect cliproxy-oauth`, not a bare
`claude` — subagent definitions are injected inline on that exact launch
command and never written to disk.

## Removal

1. Unload and remove the launchd daemon:
   ```bash
   launchctl unload ~/Library/LaunchAgents/com.deus.connectors.cliproxy-oauth.plist
   rm ~/Library/LaunchAgents/com.deus.connectors.cliproxy-oauth.plist
   ```
2. Remove the real local config:
   ```bash
   rm ~/.config/deus/connectors/cliproxy/config.local.yaml
   ```
3. Revert the `~/.claude.json` `customApiKeyResponses.approved` entry added
   in Phase 8 (remove that one key, keep the rest of the array intact).
4. Confirm: `python3 scripts/connectors_cli.py status cliproxy-oauth` now
   reports "not configured".
